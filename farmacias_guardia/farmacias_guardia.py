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
    lowered = {str(k).casefold(): v for k, v in record.items()}
    for key in keys:
        if key.casefold() in lowered and lowered[key.casefold()] not in (None, "", [], {}):
            return lowered[key.casefold()]
    return ""


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
    name = norm(first(record, "title", "nombre", "name", "farmacia", "titular"))
    address = norm(first(record, "streetAddress", "direccion", "domicilio", "address", "calle"))
    phone = norm(first(record, "telephone", "telefono", "phone"))
    locality = norm(first(record, "addressLocality", "localidad", "poblacion", "municipio", "city")) or "Zaragoza"
    area = norm(first(record, "sector", "barrio", "distrito", "area")) or locality
    schedule = norm(first(record, "horarioGuardia", "horario", "schedule", "descripcion")) or "Guardia"
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
    pharmacies = load_pharmacies()
    words = norm(text).split()[1:]
    max_bytes = int(os.getenv("FARMACIAS_MESHCORE_MAX_BYTES" if network == "meshcore" else "FARMACIAS_MESHTASTIC_MAX_BYTES", "170"))
    localities = sorted({p.locality for p in pharmacies}, key=key_text)
    if not words or key_text(" ".join(words)) in {"ayuda", "localidades", "pueblos"}:
        lines = ["Localidades: " + " · ".join(localities), "Uso: farma <localidad>"]
        return byte_chunks(lines, "FARMA", max_bytes)
    query = key_text(" ".join(words))
    locality = next((x for x in localities if key_text(x) == query), None)
    area = None
    if not locality:
        matches = [x for x in localities if query in key_text(x) or key_text(x) in query]
        if len(matches) == 1:
            locality = matches[0]
    if not locality and words and key_text(words[0]) in {"zaragoza", "zgz"}:
        locality = "Zaragoza"
        area_query = key_text(" ".join(words[1:]))
        areas = sorted({p.area for p in pharmacies if key_text(p.locality) == "zaragoza"}, key=key_text)
        if not area_query:
            return byte_chunks(["Barrios: " + " · ".join(areas), "Uso: farma zaragoza <barrio>"], "FARMA ZARAGOZA", max_bytes)
        area = next((x for x in areas if key_text(x) == area_query), None)
    if not locality:
        return byte_chunks(["Localidad no disponible.", "Usa: farma"], "FARMA", max_bytes)
    lines = grouped_lines(pharmacies, locality, area)
    if not lines:
        return [f"No consta guardia para {area or locality}."]
    date_text = datetime.now(TZ).strftime("%d/%m")
    return byte_chunks(lines, f"GUARDIA {(area or locality).upper()} {date_text}", max_bytes)


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
    return os.getenv("RADIO_PROFILE", "meshtastic_a_meshcore_embedded_b").strip().lower()


def broadcast_target() -> tuple[str, int]:
    configured = os.getenv("FARMACIAS_BROADCAST_TRANSPORT", "auto").strip().lower()
    profile = radio_profile()
    if configured == "auto":
        if profile == "meshcore_only":
            configured = "meshcore"
        elif profile == "meshtastic_a_meshcore_embedded_b":
            configured = "meshtastic"
        else:
            configured = os.getenv("FARMACIAS_MIXED_PROFILE_BROADCAST", "meshcore").strip().lower()
    channel_var = "FARMACIAS_MESHCORE_CHANNEL" if configured == "meshcore" else "FARMACIAS_MESHTASTIC_CHANNEL"
    return configured, int(os.getenv(channel_var, "-1"))


def broadcast_messages(network: str) -> list[str]:
    pharmacies = load_pharmacies()
    max_bytes = int(os.getenv("FARMACIAS_MESHCORE_MAX_BYTES" if network == "meshcore" else "FARMACIAS_MESHTASTIC_MAX_BYTES", "170"))
    lines = grouped_lines(pharmacies)
    return byte_chunks(lines, f"FARMACIAS GUARDIA {datetime.now(TZ).strftime('%d/%m')}", max_bytes)


def send_current(force: bool = False, only_if_changed: bool = False) -> dict[str, Any]:
    current = load_current()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if only_if_changed and current.get("hash") == state.get("last_sent_hash") and not force:
        return {"sent": False, "reason": "unchanged"}
    network, channel = broadcast_target()
    if channel < 0:
        raise RuntimeError(f"canal FARMACIAS no configurado para {network}")
    messages = broadcast_messages(network)
    delay = max(0, int(os.getenv("FARMACIAS_INTER_MESSAGE_DELAY_SECONDS", "8")))
    results = []
    for index, message in enumerate(messages):
        if network == "meshcore":
            response = broker_request("MESHCORE_SEND", {"kind": "chan", "channel_idx": channel, "text": message})
        else:
            response = broker_request("SEND_TEXT", {"ch": channel, "dest": None, "ack": 0, "origin": "farmacias", "no_bridge": True, "text": message})
        if not response.get("ok"):
            raise RuntimeError(f"broker rechazó fragmento {index + 1}: {response}")
        results.append(response)
        if index + 1 < len(messages) and delay:
            time.sleep(delay)
    state.update({
        "last_sent_hash": current.get("hash"), "last_sent_at": datetime.now(TZ).isoformat(),
        "last_network": network, "last_channel": channel, "last_messages": len(messages),
        "last_status": "broker_accepted",
    })
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"sent": True, "network": network, "channel": channel, "messages": len(messages), "results": results}


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
    send_p = sub.add_parser("send"); send_p.add_argument("--force", action="store_true")
    check_p = sub.add_parser("check"); check_p.add_argument("--send", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor")
    args = parser.parse_args()

    if args.command == "serve":
        host = os.getenv("FARMACIAS_API_HOST", "127.0.0.1")
        port = int(os.getenv("FARMACIAS_API_PORT", "8788"))
        server = ThreadingHTTPServer((host, port), Handler)
        print(f"API farmacias escuchando en http://{host}:{port}", flush=True)
        server.serve_forever()
    elif args.command == "fetch":
        payload = save_current(fetch()); print(json.dumps({"ok": True, "records": len(payload["pharmacies"]), "hash": payload["hash"]}, ensure_ascii=False))
    elif args.command == "preview":
        network, channel = broadcast_target()
        for msg in broadcast_messages(network):
            print(f"--- {len(msg.encode('utf-8'))} bytes ---\n{msg}")
        print(f"Destino: {network} canal {channel}")
    elif args.command == "send":
        save_current(fetch()); print(json.dumps(send_current(force=args.force), ensure_ascii=False))
    elif args.command == "check":
        before = load_current().get("hash") if CURRENT_FILE.exists() else None
        after = save_current(fetch()).get("hash")
        changed = before != after
        result = {"changed": changed}
        if args.send and changed:
            result["send"] = send_current(only_if_changed=True)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps({"current": load_current() if CURRENT_FILE.exists() else None, "state": json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None}, ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        checks = {"data_dir": str(DATA_DIR), "current_exists": CURRENT_FILE.exists(), "profile": radio_profile(), "target": broadcast_target()}
        try:
            checks["broker"] = broker_request("BROKER_STATUS", {})
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
