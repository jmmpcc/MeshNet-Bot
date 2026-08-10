#!/usr/bin/env python3
"""Aplicación autónoma e independiente de farmacias de guardia.

La aplicación no importa módulos ni lee archivos de configuración de MeshNet-Bot.
La relación con el broker se establece exclusivamente mediante las variables
de su propio archivo .env.


Modos principales:
  serve       API local que responde consultas del broker.
  fetch       Descarga, normaliza y guarda el listado vigente.
  preview     Muestra los mensajes de difusión sin transmitir.
  send        Actualiza y publica el listado por el broker.
  check       Actualiza y envía solamente si cambió.
  status      Muestra el estado persistido.
  doctor      Comprueba configuración, fuente y broker.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent


def _find_project_root() -> Path:
    """Localiza la raíz del proyecto para importar helpers compartidos.

    La aplicación continúa utilizando exclusivamente su propio ``.env``. Esta
    búsqueda solo habilita la reutilización del despachador APRS común situado
    en ``shared/`` y no carga configuración del proyecto principal.
    """
    for candidate in (BASE_DIR, *BASE_DIR.parents):
        if (candidate / "shared" / "app_aprs_dispatcher.py").exists():
            return candidate
    return BASE_DIR.parent


PROJECT_ROOT = _find_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.app_aprs_dispatcher import send_application_aprs
from shared.delivery_audit import audit_delivery, new_operation_id, result_from_response


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")

TZ = ZoneInfo(os.getenv("FARMACIAS_TIMEZONE", "Europe/Madrid"))
DATA_DIR = Path(os.getenv("FARMACIAS_DATA_DIR", str(BASE_DIR / "data")))
CURRENT_FILE = DATA_DIR / "current.json"
STATE_FILE = DATA_DIR / "state.json"


def env_bool(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on", "si", "sí", "y"}


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    return " ".join(text.replace("\xa0", " ").split()).strip()


def key_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", norm(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


@dataclass
class Pharmacy:
    name: str
    address: str
    phone: str
    locality: str
    area: str
    schedule: str
    valid_date: str
    source_id: str

    @property
    def identity(self) -> str:
        return self.source_id or hashlib.sha256(
            f"{key_text(self.locality)}|{key_text(self.address)}|{re.sub(r'\D','',self.phone)}".encode()
        ).hexdigest()[:16]


def first(record: dict[str, Any], *keys: str) -> Any:
    """Devuelve el primer campo no vacío de un diccionario, ignorando mayúsculas.

    Uso:
        value = first(record, "sector", "barrio")

    Parámetros:
        record:
            Diccionario del registro que se está normalizando.
        *keys:
            Nombres alternativos admitidos, ordenados por prioridad.

    La función conserva su comportamiento histórico y no recorre estructuras
    anidadas. Para los datos específicos de guardia se utiliza
    :func:`guardia_fields`, evitando búsquedas recursivas ambiguas.
    """
    lowered = {str(k).casefold(): v for k, v in record.items()}
    for key in keys:
        if key.casefold() in lowered and lowered[key.casefold()] not in (None, "", [], {}):
            return lowered[key.casefold()]
    return ""


def guardia_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Obtiene de forma segura el bloque de guardia de la fuente municipal.

    Uso:
        guardia = guardia_fields(record)
        sector = first(guardia, "sector")

    Parámetros:
        record:
            Registro individual devuelto por la API de farmacias.

    Funcionalidad:
        - Acepta ``guardia`` como diccionario directo.
        - Acepta listas de guardias y selecciona el primer elemento válido.
        - Tolera los alias ``guardias`` y ``onDuty`` por compatibilidad.
        - Devuelve siempre un diccionario y nunca altera el registro original.
    """
    raw = first(record, "guardia", "guardias", "onDuty", "onduty")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                return dict(item)
    return {}


def normalize_area(value: Any, locality: str) -> str:
    """Normaliza el sector oficial sin destruir nombres compuestos.

    Uso:
        area = normalize_area(guardia.get("sector"), locality)

    Parámetros:
        value:
            Texto de sector/barrio suministrado por la fuente.
        locality:
            Localidad usada como fallback cuando no existe sector.

    Funcionalidad:
        - Elimina el prefijo descriptivo ``Sector``.
        - Retira indicaciones de ubicación añadidas tras marcadores como
          ``-Esquina``, ``-Frente`` o ``-Junto``.
        - Conserva nombres oficiales compuestos como
          ``Avda. Cataluña-Barrio La Jota``.
        - Si no hay sector válido, conserva el fallback histórico de localidad.
    """
    area = norm(value)
    area = re.sub(r"^sector\s*[:.-]?\s*", "", area, flags=re.I).strip()
    area = re.split(
        r"\s*-\s*(?=(?:esquina|frente|junto|pr[oó]ximo|al lado|entre)\b)",
        area,
        maxsplit=1,
        flags=re.I,
    )[0].strip()
    return area or norm(locality)


def sector_from_guardia_text(value: Any) -> str:
    """Extrae el sector de un texto combinado de horario de guardia.

    Uso:
        sector = sector_from_guardia_text(
            "Abiertas ... Sector Delicias. Turno: T-17"
        )

    Parámetros:
        value:
            Texto libre que puede contener ``Sector ...`` y ``Turno``.

    Funcionalidad:
        - Soporta respuestas donde la fuente no separa sector y horario.
        - Captura el texto situado entre ``Sector`` y ``Turno``.
        - Devuelve cadena vacía cuando no existe un sector reconocible.
    """
    text = norm(value)
    if not text:
        return ""
    match = re.search(
        r"\bSector\s+(.+?)(?=(?:\.\s*)?Turno\s*:|$)",
        text,
        flags=re.I,
    )
    return norm(match.group(1)).rstrip(".") if match else ""


def schedule_without_sector(value: Any) -> str:
    """Elimina del horario los metadatos ``Sector`` y ``Turno``.

    Mantiene únicamente el horario legible para evitar repetir el sector en la
    salida y en el hash persistido. Si no se detecta el marcador, conserva el
    texto original.
    """
    text = norm(value)
    if not text:
        return ""
    cleaned = re.split(r"\bSector\s+", text, maxsplit=1, flags=re.I)[0]
    return norm(cleaned).rstrip(".")


def records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("result", "results", "items", "features", "rows", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            out = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                if key == "features" and isinstance(item.get("properties"), dict):
                    rec = dict(item["properties"])
                    geometry = item.get("geometry")
                    if geometry:
                        rec["geometry"] = geometry
                    out.append(rec)
                else:
                    out.append(item)
            return out
        if isinstance(value, dict):
            nested = records_from_payload(value)
            if nested:
                return nested
    return []


def parse_html_fallback(text: str, valid_date: str) -> list[Pharmacy]:
    blocks = re.split(r"(?=De Guardia\s+Farmacia|<[^>]+>\s*De Guardia\s+Farmacia)", text, flags=re.I)
    out: list[Pharmacy] = []
    for block in blocks:
        plain = norm(re.sub(r"<[^>]+>", " ", block))
        if "de guardia" not in plain.casefold():
            continue
        name = re.search(r"De Guardia\s+Farmacia\s+(.+?)(?=Tel[eé]fono:|$)", plain, re.I)
        phone = re.search(r"Tel[eé]fono:\s*([0-9 +\-]+)", plain, re.I)
        address = re.search(r"Tel[eé]fono:\s*[0-9 +\-]+\s+(.+?)(?=Horario de guardia:|Sector|Turno:|$)", plain, re.I)
        sector = re.search(r"Sector\s+(.+?)(?=Turno:|$)", plain, re.I)
        schedule = re.search(r"Horario de guardia:\s*(.+?)(?=Sector|Turno:|$)", plain, re.I)
        if address:
            out.append(Pharmacy(
                name=norm(name.group(1) if name else "Farmacia"),
                address=norm(address.group(1)),
                phone=norm(phone.group(1) if phone else ""),
                locality="Zaragoza",
                area=norm(sector.group(1) if sector else "Zaragoza"),
                schedule=norm(schedule.group(1) if schedule else "Guardia"),
                valid_date=valid_date,
                source_id="",
            ))
    return out


def normalize_record(record: dict[str, Any], valid_date: str) -> Pharmacy | None:
    """Normaliza un registro de la API municipal al modelo interno Pharmacy.

    Uso:
        pharmacy = normalize_record(record, "2026-07-24")

    Parámetros:
        record:
            Registro individual extraído de ``result``/``items`` de la fuente.
        valid_date:
            Fecha ISO a la que corresponde el listado descargado.

    Funcionalidad:
        - Mantiene los nombres alternativos ya soportados en campos raíz.
        - Lee prioritariamente ``guardia.sector`` y ``guardia.horario`` cuando
          la API municipal encapsula allí la información de guardia.
        - Normaliza el prefijo ``Sector`` y notas descriptivas de ubicación.
        - Conserva el fallback histórico a ``Zaragoza`` si falta localidad.
        - Descarta únicamente registros sin dirección, como antes.
    """
    guardia_raw = first(record, "guardia", "guardias", "onDuty", "onduty")
    guardia = guardia_fields(record)

    name = norm(first(record, "title", "nombre", "name", "farmacia", "titular"))
    address = norm(first(record, "streetAddress", "direccion", "domicilio", "address", "calle"))
    phone = norm(first(record, "telephone", "telefono", "phone"))
    locality = norm(first(record, "addressLocality", "localidad", "poblacion", "municipio", "city")) or "Zaragoza"

    schedule_raw = (
        first(guardia, "horario", "horarioGuardia", "schedule", "descripcion")
        or first(record, "horarioGuardia", "horario", "schedule", "descripcion")
        or (guardia_raw if isinstance(guardia_raw, str) else "")
    )

    area_raw = (
        first(guardia, "sector", "barrio", "distrito", "area")
        or first(record, "sector", "barrio", "distrito", "area")
        or sector_from_guardia_text(schedule_raw)
        or sector_from_guardia_text(guardia_raw if isinstance(guardia_raw, str) else "")
    )
    area = normalize_area(area_raw, locality)
    schedule = schedule_without_sector(schedule_raw) or "Guardia"

    source_id = norm(first(record, "id", "@id", "identifier", "codigo"))
    if not address:
        return None
    return Pharmacy(name or "Farmacia", address, phone, locality, area, schedule, valid_date, source_id)


def fetch() -> list[Pharmacy]:
    url = os.getenv("FARMACIAS_SOURCE_URL", "https://www.zaragoza.es/sede/servicio/farmacia.json?tipo=guardia").strip()
    timeout = max(3, int(os.getenv("FARMACIAS_REQUEST_TIMEOUT", "30")))
    req = urllib.request.Request(url, headers={"User-Agent": "MeshNet-Farmacias/1.0", "Accept": "application/json,text/html;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        content_type = str(response.headers.get("Content-Type", ""))
    text = raw.decode("utf-8", errors="replace")
    today = datetime.now(TZ).date().isoformat()
    pharmacies: list[Pharmacy] = []
    if "json" in content_type.casefold() or text.lstrip().startswith(("{", "[")):
        payload = json.loads(text)
        for record in records_from_payload(payload):
            item = normalize_record(record, today)
            if item:
                pharmacies.append(item)
    else:
        pharmacies = parse_html_fallback(text, today)
    unique: dict[str, Pharmacy] = {}
    for pharmacy in pharmacies:
        unique[pharmacy.identity] = pharmacy
    pharmacies = sorted(unique.values(), key=lambda p: (key_text(p.locality), key_text(p.area), key_text(p.address)))
    minimum = max(1, int(os.getenv("FARMACIAS_MIN_VALID_RECORDS", "1")))
    if len(pharmacies) < minimum:
        raise RuntimeError(f"fuente inválida: solo {len(pharmacies)} registros válidos")
    return pharmacies


def canonical_hash(pharmacies: Iterable[Pharmacy]) -> str:
    rows = [asdict(p) | {"identity": p.identity} for p in pharmacies]
    rows.sort(key=lambda x: x["identity"])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def added_pharmacies(previous: Iterable[Pharmacy], current: Iterable[Pharmacy]) -> list[Pharmacy]:
    """Devuelve solo identidades nuevas, conservando el orden de ``current``."""
    previous_ids = {pharmacy.identity for pharmacy in previous}
    return [pharmacy for pharmacy in current if pharmacy.identity not in previous_ids]


def save_current(pharmacies: list[Pharmacy]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(TZ).isoformat(),
        "hash": canonical_hash(pharmacies),
        "pharmacies": [asdict(p) | {"identity": p.identity} for p in pharmacies],
    }
    tmp = CURRENT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CURRENT_FILE)
    return payload


def load_current() -> dict[str, Any]:
    return json.loads(CURRENT_FILE.read_text(encoding="utf-8"))


def load_pharmacies() -> list[Pharmacy]:
    payload = load_current()
    return [Pharmacy(**{k: row.get(k, "") for k in Pharmacy.__dataclass_fields__}) for row in payload.get("pharmacies", [])]


def compact_address(value: str) -> str:
    replacements = [
        (r"\bAvenida\b|\bAvda\.?\b", "Av"), (r"\bPaseo\b|\bPº\.?\b", "Pº"),
        (r"\bCalle\b", "C/"), (r"\bPlaza\b|\bPza\.?\b", "Pl"),
        (r"\bCarretera\b", "Ctra"), (r"\bn[uú]mero\b", ""),
    ]
    out = norm(value)
    for pattern, repl in replacements:
        out = re.sub(pattern, repl, out, flags=re.I)
    return norm(out.replace(",", ""))


def byte_chunks(lines: list[str], header: str, max_bytes: int) -> list[str]:
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        probe = "\n".join([header] + current + [line])
        if current and len(probe.encode("utf-8")) > max_bytes:
            chunks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append(current)
    total = len(chunks)
    result = []
    for index, body in enumerate(chunks, 1):
        final_header = f"{header} [{index}/{total}]"
        while len((final_header + "\n" + "\n".join(body)).encode("utf-8")) > max_bytes and len(body) > 1:
            moved = body.pop()
            chunks.insert(index, [moved])
            total += 1
        result.append(final_header + "\n" + "\n".join(body))
    if len(result) != total:
        return byte_chunks(lines, header, max_bytes)
    return result


def grouped_lines(pharmacies: list[Pharmacy], locality_filter: str | None = None, area_filter: str | None = None) -> list[str]:
    selected = []
    for p in pharmacies:
        if locality_filter and key_text(locality_filter) != key_text(p.locality):
            continue
        if area_filter and key_text(area_filter) != key_text(p.area):
            continue
        selected.append(p)
    selected.sort(key=lambda p: (key_text(p.locality), key_text(p.area), key_text(p.name), key_text(p.address)))
    lines: list[str] = []
    previous = None
    for p in selected:
        group = p.area if key_text(p.locality) == "zaragoza" else p.locality
        if group != previous:
            lines.append(group.upper())
            previous = group
        details = compact_address(p.address)
        if p.phone:
            details += f" · {p.phone}"
        lines.append(details)
    return lines


def format_query(text: str, network: str) -> list[str]:
    """Responde consultas ``farma`` usando localidad y sector oficial.

    Uso desde MeshCore/Meshtastic:
        farma
        farma zaragoza
        farma zaragoza delicias

    Funcionalidad:
        - Sin argumentos, devuelve todas las farmacias de guardia disponibles.
        - ``farma ayuda`` enumera localidades.
        - ``farma zaragoza`` enumera sectores reales distintos de Zaragoza.
        - ``farma zaragoza <sector>`` filtra por coincidencia exacta o única
          parcial, tolerando tildes y mayúsculas.
        - Para otras localidades conserva el comportamiento anterior.
    """
    pharmacies = load_pharmacies()
    normalized = norm(text)
    words = normalized.split()[1:]
    max_bytes = int(os.getenv("FARMACIAS_MESHCORE_MAX_BYTES" if network == "meshcore" else "FARMACIAS_MESHTASTIC_MAX_BYTES", "170"))
    localities = sorted({p.locality for p in pharmacies}, key=key_text)

    if not words:
        lines = grouped_lines(pharmacies, None, None)
        if not lines:
            return ["No constan farmacias de guardia."]
        date_text = datetime.now(TZ).strftime("%d/%m")
        result = byte_chunks(lines, f"GUARDIA {date_text}", max_bytes)
        print(
            f"[farmacias-api] format command={normalized!r} network={network} "
            f"pharmacies={len(pharmacies)} parts={len(result)} all=True",
            flush=True,
        )
        return result

    if key_text(" ".join(words)) in {"ayuda", "localidades", "pueblos"}:
        lines = ["Localidades: " + " · ".join(localities), "Uso: farma <localidad>"]
        return byte_chunks(lines, "FARMA", max_bytes)

    # Zaragoza necesita resolución por sector antes de la coincidencia exacta
    # de localidad; de lo contrario ``farma zaragoza`` mostraría todas las
    # farmacias y nunca alcanzaría el listado de barrios/sectores.
    if key_text(words[0]) in {"zaragoza", "zgz"}:
        locality = next((x for x in localities if key_text(x) == "zaragoza"), "Zaragoza")
        areas = sorted(
            {p.area for p in pharmacies if key_text(p.locality) == "zaragoza" and key_text(p.area) != "zaragoza"},
            key=key_text,
        )
        area_query = key_text(" ".join(words[1:]))

        if not area_query:
            if not areas:
                return byte_chunks(
                    ["La fuente no ha proporcionado sectores diferenciados.", "Usa: farma zaragoza"],
                    "FARMA ZARAGOZA",
                    max_bytes,
                )
            return byte_chunks(
                ["Barrios/sectores: " + " · ".join(areas), "Uso: farma zaragoza <barrio>"],
                "FARMA ZARAGOZA",
                max_bytes,
            )

        area = next((x for x in areas if key_text(x) == area_query), None)
        if area is None:
            matches = [x for x in areas if area_query in key_text(x) or key_text(x) in area_query]
            if len(matches) == 1:
                area = matches[0]
        if area is None:
            return byte_chunks(
                ["Barrio/sector no disponible.", "Usa: farma zaragoza"],
                "FARMA ZARAGOZA",
                max_bytes,
            )

        lines = grouped_lines(pharmacies, locality, area)
        if not lines:
            return [f"No consta guardia para {area}."]
        date_text = datetime.now(TZ).strftime("%d/%m")
        return byte_chunks(lines, f"GUARDIA {area.upper()} {date_text}", max_bytes)

    query = key_text(" ".join(words))
    locality = next((x for x in localities if key_text(x) == query), None)
    if not locality:
        matches = [x for x in localities if query in key_text(x) or key_text(x) in query]
        if len(matches) == 1:
            locality = matches[0]
    if not locality:
        return byte_chunks(["Localidad no disponible.", "Usa: farma"], "FARMA", max_bytes)

    lines = grouped_lines(pharmacies, locality, None)
    if not lines:
        return [f"No consta guardia para {locality}."]
    date_text = datetime.now(TZ).strftime("%d/%m")
    return byte_chunks(lines, f"GUARDIA {locality.upper()} {date_text}", max_bytes)


def broker_request(command: str, params: dict[str, Any]) -> dict[str, Any]:
    host = os.getenv("BROKER_CTRL_HOST", "127.0.0.1")
    port = int(os.getenv("BROKER_CTRL_PORT", "8766"))
    timeout = float(os.getenv("BROKER_TIMEOUT_SECONDS", "10"))
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall((json.dumps({"cmd": command, "params": params}, ensure_ascii=False) + "\n").encode("utf-8"))
        reader = sock.makefile("rb")
        line = reader.readline()
    if not line:
        raise RuntimeError("broker sin respuesta")
    return json.loads(line.decode("utf-8", errors="replace"))


def radio_profile() -> str:
    # La aplicación es independiente del broker: si no se declara el perfil en
    # su propio .env no debemos inventar uno. El broker sigue siendo la fuente
    # de verdad y puede indicar una incompatibilidad al enviar.
    return os.getenv("RADIO_PROFILE", "auto").strip().lower().replace("-", "_")


def broadcast_targets() -> list[tuple[str, int]]:
    configured = os.getenv("FARMACIAS_BROADCAST_TRANSPORT", "auto").strip().lower()
    profile = radio_profile()
    if configured not in {"auto", "meshcore", "meshtastic", "both"}:
        raise RuntimeError(
            "FARMACIAS_BROADCAST_TRANSPORT debe ser auto, meshcore, meshtastic o both"
        )
    if configured == "auto":
        if profile == "meshcore_only":
            configured = "meshcore"
        elif profile in {"meshtastic_a_meshcore_embedded_b", "meshtastic_a_meshcore_b", "meshcore_embedded"}:
            configured = "meshtastic"
        elif profile in {"meshcore_a_meshtastic_embedded_b", "meshcore_a_meshtastic_b", "meshcore_meshtastic"}:
            configured = "meshcore"
        else:
            configured = os.getenv("FARMACIAS_MIXED_PROFILE_BROADCAST", "meshcore").strip().lower()
    # Un valor explícito antiguo puede quedar en el .env tras cambiar el perfil
    # de radio. En meshcore_only nunca debemos intentar SEND_TEXT, porque el
    # broker rechazará correctamente el adaptador Meshtastic deshabilitado.
    if profile == "meshcore_only" and configured in {"meshtastic", "both"}:
        print(
            "[farmacias] FARMACIAS_BROADCAST_TRANSPORT=meshtastic no es "
            "compatible con RADIO_PROFILE=meshcore_only; se usará MeshCore",
            file=sys.stderr,
            flush=True,
        )
        configured = "meshcore"
    networks = ("meshcore", "meshtastic") if configured == "both" else (configured,)
    return [(network, int(os.getenv(
        "FARMACIAS_MESHCORE_CHANNEL" if network == "meshcore" else "FARMACIAS_MESHTASTIC_CHANNEL", "-1"
    ))) for network in networks]


def broadcast_target() -> tuple[str, int]:
    """Compatibilidad para consumidores que esperan un único destino."""
    return broadcast_targets()[0]


def broadcast_messages(
    network: str,
    pharmacies: list[Pharmacy] | None = None,
    header: str | None = None,
) -> list[str]:
    pharmacies = load_pharmacies() if pharmacies is None else pharmacies
    max_bytes = int(os.getenv("FARMACIAS_MESHCORE_MAX_BYTES" if network == "meshcore" else "FARMACIAS_MESHTASTIC_MAX_BYTES", "170"))
    lines = grouped_lines(pharmacies)
    title = header or f"FARMACIAS GUARDIA {datetime.now(TZ).strftime('%d/%m')}"
    return byte_chunks(lines, title, max_bytes)



def _farmacias_source_label() -> str:
    """Etiqueta legible de la fuente para el journal de entregas."""
    return str(os.getenv("FARMACIAS_SOURCE_LABEL", "Ayuntamiento de Zaragoza") or "Ayuntamiento de Zaragoza").strip()


def _audit_farmacias_delivery(
    *, operation_id: str, transport: str, destination: str, message: str,
    response: dict[str, Any], channel: int | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Registra el resultado sin permitir que la auditoría altere Farmacias."""
    audit_delivery(
        app="farmacias",
        source=_farmacias_source_label(),
        operation_id=operation_id,
        transport=transport,
        destination=destination,
        channel=channel,
        message=message,
        result=result_from_response(response),
        result_detail=str(response.get("reason") or response.get("error") or ""),
        parts=response.get("chunks") if isinstance(response.get("chunks"), int) else None,
        metadata={"response": response, **(metadata or {})},
    )

def send_broadcast_message(network: str, channel: int, message: str) -> tuple[dict[str, Any], str, int]:
    """Envía un fragmento y corrige el destino usando la respuesta real del broker."""
    if network == "meshcore":
        response = broker_request("MESHCORE_SEND", {"kind": "chan", "channel_idx": channel, "text": message})
    else:
        response = broker_request("SEND_TEXT", {
            "ch": channel, "dest": None, "ack": 0, "origin": "farmacias",
            "no_bridge": True, "text": message,
        })
    if response.get("ok") or response.get("error") != "meshtastic_disabled_by_radio_profile":
        return response, network, channel

    fallback_channel = int(os.getenv("FARMACIAS_MESHCORE_CHANNEL", "-1"))
    if fallback_channel < 0:
        return response, network, channel
    print(
        "[farmacias] el broker tiene Meshtastic deshabilitado; se reintenta por MeshCore",
        file=sys.stderr,
        flush=True,
    )
    fallback = broker_request("MESHCORE_SEND", {
        "kind": "chan", "channel_idx": fallback_channel, "text": message,
    })
    return fallback, "meshcore", fallback_channel


def send_pharmacies(pharmacies: list[Pharmacy], header: str) -> dict[str, Any]:
    """Difunde exclusivamente las farmacias indicadas con una cabecera propia."""
    if not pharmacies:
        return {"sent": False, "reason": "no_new_pharmacies"}
    return _send_to_targets(pharmacies=pharmacies, header=header)


def _send_to_targets(pharmacies: list[Pharmacy] | None = None, header: str | None = None) -> dict[str, Any]:
    """Difunde a los destinos existentes y audita cada fragmento aceptado/rechazado."""
    delay = max(0, int(os.getenv("FARMACIAS_INTER_MESSAGE_DELAY_SECONDS", "8")))
    deliveries = []
    operation_id = new_operation_id("farmacias")
    for network, channel in broadcast_targets():
        if channel < 0:
            raise RuntimeError(f"canal FARMACIAS no configurado para {network}")
        messages, results = broadcast_messages(network, pharmacies, header), []
        actual_network, actual_channel = network, channel
        for index, message in enumerate(messages):
            try:
                response, actual_network, actual_channel = send_broadcast_message(network, channel, message)
            except Exception as exc:
                response = {"ok": False, "sent": False, "reason": "request_failed", "error": f"{type(exc).__name__}: {exc}"}
                _audit_farmacias_delivery(
                    operation_id=operation_id, transport=network,
                    destination=f"channel:{channel}", channel=channel,
                    message=message, response=response,
                    metadata={"fragment": index + 1, "broadcast": True},
                )
                raise
            _audit_farmacias_delivery(
                operation_id=operation_id, transport=actual_network,
                destination=f"channel:{actual_channel}", channel=actual_channel,
                message=message, response=response,
                metadata={"fragment": index + 1, "broadcast": True, "requested_network": network},
            )
            if not response.get("ok"):
                raise RuntimeError(f"broker rechazó fragmento {index + 1} ({network}): {response}")
            results.append(response)
            if index + 1 < len(messages) and delay:
                time.sleep(delay)
        deliveries.append({"network": actual_network, "channel": actual_channel, "messages": len(messages), "results": results})
    return {"sent": True, **deliveries[0], "deliveries": deliveries, "operation_id": operation_id}

def farmacias_aprs_summary(pharmacies: list[Pharmacy] | None = None) -> str:
    """Construye un resumen APRS compacto del listado vigente.

    Parámetros:
      pharmacies: listado que se acaba de difundir. Si se omite, se carga la
        instantánea actual.

    El resumen evita direcciones y teléfonos para no ocupar varias tramas. El
    detalle completo continúa disponible por la malla mediante
    ``farma <localidad>``.
    """
    items = load_pharmacies() if pharmacies is None else pharmacies
    localities = sorted(
        {norm(item.locality) for item in items if norm(item.locality)},
        key=key_text,
    )
    date_text = datetime.now(TZ).strftime("%d/%m")
    locality_text = ",".join(localities[:3]) if localities else "sin localidad"
    if len(localities) > 3:
        locality_text += f" +{len(localities) - 3}"
    return (
        f"FARMA {date_text}: {len(items)} guardias en {locality_text}. "
        "Detalle por malla: farma <localidad>"
    )


def send_farmacias_aprs(
    *,
    pharmacies: list[Pharmacy] | None = None,
    requested: bool = False,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Solicita APRS y registra su resultado sin alterar sus autorizaciones.

    Se conserva estrictamente el orden histórico: las autorizaciones se
    comprueban antes de cargar/formatear datos locales. Así, con APRS
    desactivado, la función sigue sin necesitar ``current.json`` ni llamar al
    dispatcher.
    """
    op_id = operation_id or new_operation_id("farmacias")
    text = ""
    if not env_bool("FARMACIAS_APRS_ENABLED", "0"):
        result = {"ok": True, "skipped": True, "error": "farmacias_aprs_disabled"}
    elif not requested and not env_bool("FARMACIAS_APRS_AUTOMATIC", "0"):
        result = {
            "ok": True,
            "skipped": True,
            "error": "farmacias_aprs_automatic_disabled",
        }
    else:
        text = farmacias_aprs_summary(pharmacies)
        result = send_application_aprs(
            source="farmacias",
            text=text,
            dest=os.getenv(
                "FARMACIAS_APRS_DESTINATION",
                os.getenv("APPS_APRS_DESTINATION", "broadcast"),
            ),
            origin="app_farmacias",
        )
    _audit_farmacias_delivery(
        operation_id=op_id,
        transport="aprs",
        destination=str(result.get("dest") or os.getenv("FARMACIAS_APRS_DESTINATION", "broadcast")),
        message=text,
        response=result,
        metadata={"requested": requested},
    )
    return result

def send_current(
    force: bool = False,
    only_if_changed: bool = False,
    *,
    aprs_requested: bool = False,
) -> dict[str, Any]:
    current = load_current()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if only_if_changed and current.get("hash") == state.get("last_sent_hash") and not force:
        return {"sent": False, "reason": "unchanged"}
    delivery = _send_to_targets()
    state.update({
        "last_sent_hash": current.get("hash"), "last_sent_at": datetime.now(TZ).isoformat(),
        "last_network": delivery["network"], "last_channel": delivery["channel"],
        "last_messages": delivery["messages"], "last_deliveries": delivery["deliveries"],
        "last_status": "broker_accepted",
    })
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    # APRS se solicita únicamente después de que todos los destinos Mesh hayan
    # sido aceptados. Un fallo APRS no revierte, repite ni invalida el envío
    # Mesh que ya funciona.
    delivery["aprs"] = send_farmacias_aprs(requested=aprs_requested, operation_id=delivery.get("operation_id"))
    return delivery


_MESH_EVENT_STOP = threading.Event()
_MESH_EVENT_DEDUP_LOCK = threading.Lock()
_MESH_EVENT_DEDUP: dict[str, float] = {}


def _strip_meshcore_display_prefix(text: str) -> str:
    """Elimina únicamente la cabecera visual ``[MC...]`` añadida por el broker.

    El evento JSONL MeshCore puede publicar el texto como ``[MC:... ] farma`` para
    que el bot muestre el origen. La aplicación independiente necesita evaluar el
    comando original y, por tanto, retira solo esa cabecera inicial.
    """
    value = norm(text)
    return re.sub(r"^\[MC[^\]]*\]\s*", "", value, flags=re.IGNORECASE).strip()



def _split_meshcore_display_alias(text: str) -> tuple[str, str]:
    """Separa ``ALIAS: comando`` usado en canales MeshCore, si existe."""
    value = _strip_meshcore_display_prefix(text)
    match = re.match(r"^([A-Za-z0-9_./@+\-]{3,32})\s*:\s+(.+)$", value)
    if not match:
        return "", value
    return norm(match.group(1)), norm(match.group(2))



def _meshcore_contact_prefix_for_alias_from_env(alias: str) -> str:
    """Resuelve alias desde ``MESHCORE_CONTACT_ALIASES`` si está configurado."""
    wanted = key_text(alias)
    if not wanted:
        return ""
    matches: list[str] = []
    for part in (os.getenv("MESHCORE_CONTACT_ALIASES", "") or "").split(","):
        if ":" not in part:
            continue
        prefix, name = part.split(":", 1)
        if key_text(name) == wanted and norm(prefix):
            matches.append(norm(prefix))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else ""

def _meshcore_contact_prefix_for_alias(alias: str) -> str:
    """Resuelve un alias/nombre MeshCore a prefijo DM usando el broker."""
    wanted = key_text(alias)
    if not wanted:
        return ""
    env_prefix = _meshcore_contact_prefix_for_alias_from_env(alias)
    if env_prefix:
        return env_prefix
    try:
        response = broker_request("MESHCORE_CONTACTS", {"limit": 500})
    except Exception:
        return ""
    if not response.get("ok"):
        return ""
    matches: list[str] = []
    for contact in response.get("contacts") or []:
        if not isinstance(contact, dict):
            continue
        names = [contact.get("name"), contact.get("alias"), contact.get("label")]
        if any(key_text(value) == wanted for value in names if value):
            dm_key = norm(contact.get("dm_key") or contact.get("prefix") or contact.get("contact_id") or "")
            if dm_key:
                matches.append(dm_key)
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else ""

def _event_seen_recently(source: str, text: str, rx_time: Any) -> bool:
    """Deduplica eventos JSONL durante una ventana corta para evitar respuestas dobles."""
    now = time.time()
    ttl = max(5.0, float(os.getenv("FARMACIAS_EVENT_DEDUP_SECONDS", "30")))
    key = hashlib.sha1(f"{source}|{text}|{rx_time}".encode("utf-8", errors="ignore")).hexdigest()
    with _MESH_EVENT_DEDUP_LOCK:
        for old_key, ts in list(_MESH_EVENT_DEDUP.items()):
            if now - ts > ttl:
                _MESH_EVENT_DEDUP.pop(old_key, None)
        if key in _MESH_EVENT_DEDUP:
            return True
        _MESH_EVENT_DEDUP[key] = now
    return False


def _send_meshcore_dm(contact_prefix: str, messages: list[str], *, send_all: bool = False) -> int:
    """Envía respuesta DM MeshCore y registra cada fragmento best-effort."""
    prefix = norm(contact_prefix)
    if not prefix:
        raise RuntimeError("evento MeshCore sin pubkey_prefix; no se puede responder por DM")

    delay = max(0.0, float(os.getenv("FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS", "1.0")))
    max_messages = max(1, int(os.getenv("FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE", "6")))
    selected_messages = messages if send_all else messages[:max_messages]
    operation_id = new_operation_id("farmacias-dm")
    for index, message in enumerate(selected_messages):
        try:
            response = broker_request(
                "MESHCORE_SEND",
                {"kind": "contact", "contact_prefix": prefix, "text": str(message)},
            )
        except Exception as exc:
            response = {"ok": False, "sent": False, "reason": "request_failed", "error": f"{type(exc).__name__}: {exc}"}
            _audit_farmacias_delivery(
                operation_id=operation_id, transport="meshcore",
                destination=f"contact:{prefix}", message=str(message), response=response,
                metadata={"direct_reply": True, "fragment": index + 1},
            )
            raise
        _audit_farmacias_delivery(
            operation_id=operation_id, transport="meshcore",
            destination=f"contact:{prefix}", message=str(message), response=response,
            metadata={"direct_reply": True, "fragment": index + 1},
        )
        if not response.get("ok"):
            raise RuntimeError(f"broker rechazó respuesta DM {index + 1}: {response}")
        if index + 1 < len(selected_messages) and delay:
            time.sleep(delay)
    return len(selected_messages)

def _handle_broker_event(event: dict[str, Any]) -> None:
    """Procesa un evento JSONL del broker y atiende comandos ``farma`` MeshCore.

    Solo acepta:
      - mensajes directos MeshCore; o
      - mensajes del canal indicado por ``FARMACIAS_MESHCORE_CHANNEL``.

    La respuesta se devuelve siempre por DM al ``meshcore_pubkey_prefix`` emisor.
    """
    if not isinstance(event, dict) or event.get("type") != "packet":
        return
    packet = event.get("packet") or {}
    if not isinstance(packet, dict) or not packet.get("meshcore"):
        return
    decoded = packet.get("decoded") or {}
    if not isinstance(decoded, dict) or str(decoded.get("portnum") or "").upper() != "TEXT_MESSAGE_APP":
        return

    raw_text = decoded.get("text") or ""
    display_alias, text = _split_meshcore_display_alias(raw_text)
    if not (text.casefold() == "farma" or text.casefold().startswith("farma ")):
        return

    kind = str(packet.get("meshcore_kind") or "").strip().lower()
    try:
        chan_idx = int(packet.get("meshcore_chan_idx")) if packet.get("meshcore_chan_idx") is not None else None
    except Exception:
        chan_idx = None
    allowed_channel = int(os.getenv("FARMACIAS_MESHCORE_CHANNEL", "-1"))
    is_direct = kind == "contact"
    if not is_direct and not (kind in {"chan", "channel"} and chan_idx == allowed_channel):
        return

    source = norm(packet.get("meshcore_pubkey_prefix") or "")
    if not source:
        from_id = norm(packet.get("fromId") or "")
        if from_id.lower().startswith("meshcore:"):
            candidate = from_id.split(":", 1)[1].strip()
            if candidate and candidate.casefold() != "meshcore":
                source = candidate
    if not source and kind in {"chan", "channel"}:
        source = _meshcore_contact_prefix_for_alias(display_alias)
    if not source:
        print("[farmacias-listener] comando ignorado: origen MeshCore sin pubkey_prefix/alias resoluble", flush=True)
        return

    rx_time = packet.get("rxTime") or event.get("ts") or 0
    if _event_seen_recently(source, text, rx_time):
        return

    try:
        normalized_command = norm(text)
        print(
            f"[farmacias-listener] request source={source} kind={kind} channel={chan_idx} "
            f"raw={raw_text!r} normalized={normalized_command!r} rx_time={rx_time}",
            flush=True,
        )
        messages = format_query(text, "meshcore")
        send_all = normalized_command.casefold() == "farma"
        sent_parts = _send_meshcore_dm(source, messages, send_all=send_all)
        print(
            f"[farmacias-listener] atendido source={source} kind={kind} "
            f"channel={chan_idx} command={normalized_command!r} "
            f"parts_generated={len(messages)} parts_enqueued={sent_parts}",
            flush=True,
        )
    except Exception as exc:
        print(f"[farmacias-listener] ERROR {type(exc).__name__}: {exc}", flush=True)
        try:
            _send_meshcore_dm(source, ["Servicio de farmacias no disponible temporalmente."])
        except Exception:
            pass


def broker_event_listener(stop_event: threading.Event | None = None) -> None:
    """Mantiene una conexión 24x7 al puerto JSONL del broker.

    Parámetros configurables en el `.env` independiente:
      ``BROKER_EVENT_HOST`` (127.0.0.1), ``BROKER_EVENT_PORT`` (8765),
      ``FARMACIAS_LISTENER_RECONNECT_SECONDS`` (5).
    """
    stop = stop_event or _MESH_EVENT_STOP
    host = os.getenv("BROKER_EVENT_HOST", os.getenv("BROKER_CTRL_HOST", "127.0.0.1"))
    port = int(os.getenv("BROKER_EVENT_PORT", "8765"))
    reconnect = max(1.0, float(os.getenv("FARMACIAS_LISTENER_RECONNECT_SECONDS", "5")))

    while not stop.is_set():
        try:
            print(f"[farmacias-listener] conectando a {host}:{port}", flush=True)
            with socket.create_connection((host, port), timeout=10) as sock:
                sock.settimeout(30.0)
                reader = sock.makefile("r", encoding="utf-8", errors="replace")
                print(f"[farmacias-listener] conectado a {host}:{port}", flush=True)
                while not stop.is_set():
                    try:
                        line = reader.readline()
                    except socket.timeout:
                        continue
                    if not line:
                        raise ConnectionError("broker cerró la conexión JSONL")
                    try:
                        _handle_broker_event(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            if not stop.is_set():
                print(f"[farmacias-listener] reconexión: {type(exc).__name__}: {exc}", flush=True)
                stop.wait(reconnect)


def start_broker_event_listener() -> threading.Thread | None:
    """Arranca el listener independiente en segundo plano si está habilitado."""
    if not env_bool("FARMACIAS_COMMAND_LISTENER_ENABLED", "1"):
        print("[farmacias-listener] deshabilitado por configuración", flush=True)
        return None
    thread = threading.Thread(
        target=broker_event_listener,
        name="farmacias-broker-listener",
        daemon=True,
    )
    thread.start()
    return thread


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print("[farmacias-api] " + (fmt % args), flush=True)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"ok": CURRENT_FILE.exists(), "updated_at": load_current().get("updated_at") if CURRENT_FILE.exists() else None})
        else:
            self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/query":
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            text = norm(request.get("text"))
            network = norm(request.get("network")).lower()
            if not (text.casefold() == "farma" or text.casefold().startswith("farma ")):
                self.send_json(200, {"recognized": False, "messages": []})
                return
            self.send_json(200, {"recognized": True, "messages": format_query(text, network)})
        except Exception as exc:
            self.send_json(500, {"recognized": True, "messages": ["Servicio de farmacias sin datos válidos."], "error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Farmacias de guardia para MeshNet-Bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("fetch")
    sub.add_parser("preview")
    send_p = sub.add_parser("send")
    send_p.add_argument("--force", action="store_true")
    send_p.add_argument(
        "--aprs",
        action="store_true",
        help="solicita también el resumen APRS; requiere las autorizaciones del .env",
    )
    check_p = sub.add_parser("check"); check_p.add_argument("--send", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor")
    args = parser.parse_args()

    if args.command == "serve":
        host = os.getenv("FARMACIAS_API_HOST", "127.0.0.1")
        port = int(os.getenv("FARMACIAS_API_PORT", "8788"))
        listener_thread = start_broker_event_listener()
        server = ThreadingHTTPServer((host, port), Handler)
        print(f"API farmacias escuchando en http://{host}:{port}", flush=True)
        try:
            server.serve_forever()
        finally:
            _MESH_EVENT_STOP.set()
            server.server_close()
            if listener_thread and listener_thread.is_alive():
                listener_thread.join(timeout=2.0)
    elif args.command == "fetch":
        payload = save_current(fetch()); print(json.dumps({"ok": True, "records": len(payload["pharmacies"]), "hash": payload["hash"]}, ensure_ascii=False))
    elif args.command == "preview":
        if not CURRENT_FILE.exists():
            print(json.dumps({
                "ok": False,
                "error": (
                    "No hay datos locales de farmacias. "
                    "Pulse «Actualizar datos» antes de «Ver farmacias»."
                ),
            }, ensure_ascii=False))
            return 1
        network, channel = broadcast_target()
        for msg in broadcast_messages(network):
            print(f"--- {len(msg.encode('utf-8'))} bytes ---\n{msg}")
        print(f"Destino: {network} canal {channel}")
    elif args.command == "send":
        save_current(fetch()); print(json.dumps(send_current(force=args.force, aprs_requested=args.aprs), ensure_ascii=False))
    elif args.command == "check":
        previous = load_pharmacies() if CURRENT_FILE.exists() else []
        had_previous = CURRENT_FILE.exists()
        current = fetch()
        before_hash = canonical_hash(previous) if had_previous else None
        after = canonical_hash(current)
        # Sin una instantánea previa no hay base para afirmar que un registro
        # sea una incorporación; se inicializa la copia sin difundir todo.
        added = added_pharmacies(previous, current) if had_previous else []
        result = {"changed": before_hash != after, "new_pharmacies": len(added)}
        if args.send and added:
            result["send"] = send_pharmacies(added, "NUEVAS FARMACIAS DE GUARDIA")
        save_current(current)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps({"current": load_current() if CURRENT_FILE.exists() else None, "state": json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None}, ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        checks = {
            "data_dir": str(DATA_DIR),
            "current_exists": CURRENT_FILE.exists(),
            "profile": radio_profile(),
            "targets": broadcast_targets(),
            "listener_enabled": env_bool("FARMACIAS_COMMAND_LISTENER_ENABLED", "1"),
            "broker_event": [
                os.getenv("BROKER_EVENT_HOST", os.getenv("BROKER_CTRL_HOST", "127.0.0.1")),
                int(os.getenv("BROKER_EVENT_PORT", "8765")),
            ],
        }
        try:
            status_cmd = "MESHCORE_STATUS" if radio_profile() == "meshcore_only" else "BROKER_STATUS"
            checks["broker"] = broker_request(status_cmd, {})
        except Exception as exc:
            checks["broker_error"] = str(exc)
        try:
            checks["source_records"] = len(fetch())
        except Exception as exc:
            checks["source_error"] = str(exc)
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
