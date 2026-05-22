#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aemet_alerts.py v7.0.13
Avisos oficiales AEMET para MeshNet Bot.

Objetivo:
- Consultar avisos oficiales AEMET desde RSS/Atom/CAP.
- Filtrar por zona/provincia/comunidad configurada.
- Detectar avisos nuevos o cambios relevantes.
- Generar un texto corto para RF.
- NO enviar por RF directamente.
- El envío lo realiza broker_task.py usando el transporte ya existente:
  Meshtastic, APRS, MeshCore canal o MeshCore DM.

Uso desde broker_task.py:
    from aemet_alerts import build_aemet_alert_text_from_meta
    text = build_aemet_alert_text_from_meta(task.meta)

Campos meta esperados:
    task_type: "aemet_alert"
    zone: "Zaragoza"
    province: "Zaragoza"
    region: "Aragón"
    timezone: "Europe/Madrid"

Variables .env principales:
    AEMET_ALERTS_ENABLED=1
    AEMET_ALERTS_FEED_URL=
    AEMET_ALERTS_DEFAULT_ZONE=Zaragoza
    AEMET_ALERTS_DEFAULT_PROVINCE=Zaragoza
    AEMET_ALERTS_DEFAULT_REGION=Aragón
    AEMET_ALERTS_LEVELS=amarillo,naranja,rojo
    AEMET_ALERTS_EVENTS=
    AEMET_ALERTS_STATE_PATH=bot_data/aemet_alerts_state.json
    AEMET_ALERTS_REPEAT_HOURS=6
    AEMET_ALERTS_COOLDOWN_MIN=15
    AEMET_ALERTS_MAX_PER_RUN=2
    AEMET_ALERTS_MAX_CHARS=180
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# =============================================================================
# Configuración base
# =============================================================================

DEFAULT_AEMET_INFO_URL = "https://www.aemet.es/es/rss_info/avisos/esp"

_CACHE: Dict[str, Tuple[float, str]] = {}


@dataclass
class AemetAlert:
    """
    Aviso AEMET normalizado.

    Campos:
        alert_id:
            Identificador estable si existe. Si no existe, se calcula por contenido.
        title:
            Título o cabecera del aviso.
        summary:
            Resumen/descripción.
        link:
            URL original si existe.
        level:
            Nivel normalizado: amarillo, naranja, rojo o desconocido.
        event:
            Fenómeno: lluvia, tormentas, viento, nieve, temperaturas, costeros...
        zone:
            Zona afectada detectada.
        province:
            Provincia detectada.
        region:
            Comunidad/región detectada.
        start:
            Inicio de validez en texto ISO o vacío.
        end:
            Fin de validez en texto ISO o vacío.
        raw_text:
            Texto completo usado para filtrado/dedupe.
    """
    alert_id: str
    title: str
    summary: str
    link: str
    level: str
    event: str
    zone: str
    province: str
    region: str
    start: str
    end: str
    raw_text: str


# =============================================================================
# Helpers de entorno / IO
# =============================================================================

def _env_bool(name: str, default: str = "0") -> bool:
    v = (os.getenv(name, default) or default).strip().lower()
    return v in {"1", "true", "yes", "y", "on", "si", "sí"}


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name, str(default)) or str(default)).strip().replace(",", "."))
    except Exception:
        return float(default)


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip()


def _csv_set(raw: str) -> set[str]:
    out: set[str] = set()
    for p in str(raw or "").replace(";", ",").split(","):
        v = p.strip().lower()
        if v:
            out.add(v)
    return out


def _safe_mkdir_for_file(path: str) -> None:
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass


def _read_json(path: str) -> dict:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: str, obj: dict) -> None:
    try:
        _safe_mkdir_for_file(path)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


# =============================================================================
# Normalización de texto
# =============================================================================

def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _normalize_for_match(s: str) -> str:
    """
    Normaliza para búsquedas:
    - minúsculas
    - sin acentos
    - espacios compactados
    """
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _ascii_safe(s: str) -> str:
    """
    Convierte texto a ASCII 7-bit aproximado.
    Útil si se quiere máxima compatibilidad con APRS o clientes antiguos.
    """
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = t.encode("ascii", "ignore").decode("ascii", "ignore")
    t = "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in t)
    return _norm_spaces(t)

def _clip_text_bytes_safe(s: str, max_bytes: int) -> str:
    """
    Recorta texto por bytes UTF-8 sin romper caracteres.

    Uso:
        txt = _clip_text_bytes_safe(txt, 140)

    Parámetros:
        s:
            Texto original.
        max_bytes:
            Límite máximo en bytes UTF-8.

    Funcionalidad:
        - MeshCore trabaja de forma más sensible al tamaño real en bytes que al número
          de caracteres.
        - Evita cortar en mitad de un carácter multibyte.
        - Intenta cortar en espacio para no dejar palabras partidas.
        - No añade sufijos ni caracteres extra.
    """
    txt = _norm_spaces(s)
    if not txt:
        return ""

    max_bytes = max(40, int(max_bytes or 140))

    if len(txt.encode("utf-8", errors="ignore")) <= max_bytes:
        return txt

    lo, hi = 1, len(txt)
    best = 1

    while lo <= hi:
        mid = (lo + hi) // 2
        part = txt[:mid]
        if len(part.encode("utf-8", errors="ignore")) <= max_bytes:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    cut = best
    sp = txt.rfind(" ", 0, cut + 1)
    if sp >= max(20, int(cut * 0.65)):
        cut = sp

    return txt[:cut].strip()


def _aemet_meshcore_safe_text(msg: str) -> str:
    """
    Genera una variante segura para TX MeshCore de avisos AEMET.

    Uso:
        msg = _aemet_meshcore_safe_text(msg)

    Funcionalidad:
        - Convierte a ASCII si AEMET_ALERTS_MESHCORE_ASCII_SAFE=1.
        - Elimina secuencias poco útiles o problemáticas como '-?'.
        - Normaliza espacios.
        - Recorta por bytes, no por caracteres.
        - Mantiene el mensaje informativo pero reduce riesgo de no_event_received
          por payload largo o caracteres delicados.
    """
    txt = _norm_spaces(msg)

    # Limpieza específica de finales/intervalos incompletos observados:
    # "11:31-?" -> "11:31"
    txt = re.sub(r"(\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})-\?", r"\1", txt)

    # Evita finales cortados tipo "23-" si el resumen fue recortado.
    txt = re.sub(r"\s+\d{1,2}-$", "", txt).strip()

    # Sustituye expresiones largas frecuentes.
    replacements = {
        "temperaturas": "temp.",
        "temperatura máxima": "temp. max.",
        "temperatura minima": "temp. min.",
        "temperatura mínima": "temp. min.",
        "Aviso de": "Aviso:",
        "nivel amarillo": "amarillo",
        "nivel naranja": "naranja",
        "nivel rojo": "rojo",
        "Vigente": "Vig.",
    }
    for a, b in replacements.items():
        txt = txt.replace(a, b)

    if _env_bool("AEMET_ALERTS_MESHCORE_ASCII_SAFE", "1"):
        txt = _ascii_safe(txt)

    max_bytes = _env_int("AEMET_ALERTS_MESHCORE_MAX_BYTES", 135)
    return _clip_text_bytes_safe(txt, max_bytes)


def _clip_text(s: str, max_chars: int) -> str:
    t = _norm_spaces(s)
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    if max_chars < 20:
        return t[:max_chars].strip()
    return t[: max_chars - 1].rstrip() + "…"


# =============================================================================
# Descarga HTTP con caché
# =============================================================================

def _http_get_text(url: str, timeout_sec: float) -> str:
    """
    Descarga texto por HTTP/HTTPS con user-agent propio.

    Versión 24/7:
    - No usa resp.read() sin límite.
    - Lee por bloques con límite máximo.
    - Evita bloqueos si el servidor tarda en cerrar la conexión.
    """
    url = (url or "").strip()
    if not url:
        raise RuntimeError("AEMET feed URL vacío")

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MeshNetBot-AEMET-Alerts/7.0.13",
            "Accept": "application/xml,text/xml,application/rss+xml,application/atom+xml,text/html;q=0.8,*/*;q=0.5",
            "Connection": "close",
        },
    )

    max_bytes = max(8192, _env_int("AEMET_ALERTS_MAX_DOWNLOAD_BYTES", 262144))
    timeout_sec = max(2.0, float(timeout_sec or 10.0))

    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        try:
            resp.fp.raw._sock.settimeout(timeout_sec)
        except Exception:
            pass

        chunks = []
        total = 0

        while total < max_bytes:
            try:
                chunk = resp.read(min(16384, max_bytes - total))
            except TimeoutError:
                break
            except Exception:
                break

            if not chunk:
                break

            chunks.append(chunk)
            total += len(chunk)

        raw = b"".join(chunks)

        charset = "utf-8"
        ctype = resp.headers.get("Content-Type", "") if resp.headers else ""
        m = re.search(r"charset=([^;\s]+)", ctype, flags=re.I)
        if m:
            charset = m.group(1).strip()

        try:
            return raw.decode(charset, errors="replace")
        except Exception:
            return raw.decode("utf-8", errors="replace")

def _fetch_cached(url: str) -> str:
    """
    Descarga con caché en memoria.

    Evita varias descargas si existen varias tareas AEMET configuradas.
    """
    timeout_sec = _env_float("AEMET_ALERTS_TIMEOUT_SEC", 10.0)
    cache_sec = _env_int("AEMET_ALERTS_CACHE_SEC", 300)
    now = time.time()

    key = url.strip()
    if cache_sec > 0:
        old = _CACHE.get(key)
        if old and (now - old[0]) <= cache_sec:
            return old[1]

    text = _http_get_text(key, timeout_sec)
    _CACHE[key] = (now, text)

    dump_path = _env_str("AEMET_ALERTS_LAST_FEED_DUMP", "")
    if dump_path:
        try:
            _safe_mkdir_for_file(dump_path)
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    return text


# =============================================================================
# Descubrimiento básico de feeds si AEMET_ALERTS_FEED_URL está vacío
# =============================================================================

def _discover_feed_urls_from_info_page(html_text: str, base_url: str) -> list[str]:
    """
    Intenta descubrir enlaces RSS/Atom/CAP desde una página informativa AEMET.

    Es deliberadamente tolerante:
    - busca href="..."
    - prioriza enlaces que parezcan RSS/Atom/XML/CAP
    - convierte URLs relativas en absolutas
    """
    out: list[str] = []
    seen: set[str] = set()

    for m in re.finditer(r"""href=["']([^"']+)["']""", html_text or "", flags=re.I):
        href = html.unescape(m.group(1).strip())
        if not href:
            continue
        abs_url = urllib.parse.urljoin(base_url, href)
        low = abs_url.lower()

        looks_xml = any(x in low for x in ("rss", "atom", "cap", ".xml", "avisos"))
        if not looks_xml:
            continue

        if abs_url not in seen:
            seen.add(abs_url)
            out.append(abs_url)

    # Primero XML/RSS/Atom/CAP explícitos.
    out.sort(key=lambda u: (0 if re.search(r"(rss|atom|cap|\.xml)", u, flags=re.I) else 1, len(u)))
    return out


def _configured_feed_urls() -> list[str]:
    """
    Devuelve las URLs fuente.

    Reglas:
    1) Si AEMET_ALERTS_FEED_URL está definido, se usa tal cual.
       Admite varias URLs separadas por coma o punto y coma.
    2) Si está vacío, se usa la página oficial de RSS/Atom de avisos de España
       y se intenta descubrir feed XML/CAP desde ahí.
    """
    raw = _env_str("AEMET_ALERTS_FEED_URL", "")
    if raw:
        urls = [u.strip() for u in raw.replace(";", ",").split(",") if u.strip()]
        return urls

    # Fallback: página oficial informativa.
    # Si no se encuentra feed dentro, se intentará parsear la propia página,
    # pero normalmente será mejor definir AEMET_ALERTS_FEED_URL cuando tengamos
    # el canal exacto de comunidad/provincia.
    try:
        page = _fetch_cached(DEFAULT_AEMET_INFO_URL)
        urls = _discover_feed_urls_from_info_page(page, DEFAULT_AEMET_INFO_URL)
        if urls:
            return urls[:5]
    except Exception:
        pass

    return [DEFAULT_AEMET_INFO_URL]


# =============================================================================
# Parseo XML / RSS / Atom / CAP
# =============================================================================

def _xml_root(text: str) -> Optional[ET.Element]:
    try:
        return ET.fromstring(text.encode("utf-8"))
    except Exception:
        try:
            return ET.fromstring(text)
        except Exception:
            return None


def _tag_name(el: ET.Element) -> str:
    t = el.tag or ""
    if "}" in t:
        return t.rsplit("}", 1)[1].lower()
    return t.lower()


def _child_text(el: ET.Element, *names: str) -> str:
    wanted = {n.lower() for n in names}
    for c in list(el):
        if _tag_name(c) in wanted:
            return _strip_html(c.text or "")
    return ""


def _findall_by_local(root: ET.Element, local_name: str) -> list[ET.Element]:
    target = local_name.lower()
    return [e for e in root.iter() if _tag_name(e) == target]


def _atom_link(el: ET.Element) -> str:
    for c in list(el):
        if _tag_name(c) == "link":
            href = c.attrib.get("href") or c.text or ""
            if href:
                return href.strip()
    return ""


def _parse_dt_any(s: str) -> str:
    """
    Convierte fechas comunes a ISO si puede.
    Si no puede, devuelve el texto original recortado.
    """
    raw = _norm_spaces(s)
    if not raw:
        return ""

    # ISO directo
    try:
        ss = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        pass

    # RFC 2822 / pubDate
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
    except Exception:
        pass

    return raw[:80]


def _level_from_text(text: str, severity: str = "") -> str:
    """
    Detecta nivel Meteoalerta.

    AEMET suele usar amarillo/naranja/rojo.
    CAP puede usar severity en inglés:
      - Extreme -> rojo
      - Severe  -> naranja
      - Moderate -> amarillo
    """
    t = _normalize_for_match(text)
    sev = _normalize_for_match(severity)

    if "rojo" in t or sev == "extreme":
        return "rojo"
    if "naranja" in t or sev == "severe":
        return "naranja"
    if "amarillo" in t or sev == "moderate":
        return "amarillo"

    # Fallback: palabras de severidad.
    if "extremo" in t or "riesgo extremo" in t:
        return "rojo"
    if "importante" in t or "riesgo importante" in t:
        return "naranja"
    if "riesgo" in t:
        return "amarillo"

    return "desconocido"


def _event_from_text(text: str) -> str:
    """
    Detecta fenómeno meteorológico principal.
    """
    t = _normalize_for_match(text)

    checks = [
        ("tormentas", ("tormenta", "tormentas", "granizo")),
        ("lluvia", ("lluvia", "precipitacion", "precipitaciones", "chubasco", "chubascos")),
        ("viento", ("viento", "racha", "rachas")),
        ("nieve", ("nieve", "nevadas", "nevada")),
        ("temperaturas", ("temperatura", "calor", "frio", "helada", "heladas")),
        ("costeros", ("costeros", "costero", "oleaje", "maritimo", "maritima")),
        ("polvo", ("polvo", "calima")),
        ("niebla", ("niebla", "visibilidad")),
        ("aludes", ("alud", "aludes", "avalancha")),
    ]

    for name, needles in checks:
        if any(n in t for n in needles):
            return name

    return "fenómeno adverso"


def _detect_zone_from_text(text: str, fallback: str = "") -> str:
    """
    Devuelve una zona corta, intentando no crear textos enormes.
    """
    raw = _norm_spaces(text)
    fb = _norm_spaces(fallback)
    if fb:
        return fb

    # Patrones frecuentes.
    for pat in (
        r"zona(?:s)?\s+(?:de\s+)?aviso[:\s]+([^.;,\n]{3,80})",
        r"área(?:s)?[:\s]+([^.;,\n]{3,80})",
        r"area(?:s)?[:\s]+([^.;,\n]{3,80})",
        r"provincia(?:s)?[:\s]+([^.;,\n]{3,80})",
    ):
        m = re.search(pat, raw, flags=re.I)
        if m:
            return _norm_spaces(m.group(1))[:80]

    return ""


def _make_alert_hash(a: AemetAlert) -> str:
    """
    Genera hash estable para deduplicación.
    Incluye nivel, fenómeno, zona e intervalo para reenviar si cambia el aviso.
    """
    base = "\n".join([
        a.alert_id or "",
        a.title or "",
        a.level or "",
        a.event or "",
        a.zone or "",
        a.province or "",
        a.region or "",
        a.start or "",
        a.end or "",
        a.link or "",
    ])
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def _parse_cap_alert(root: ET.Element, source_link: str = "") -> list[AemetAlert]:
    """
    Parsea CAP 1.x.

    Estructura habitual:
      alert
        identifier
        sent
        info
          event
          severity
          urgency
          headline
          description
          instruction
          onset/effective/expires
          area/areaDesc
    """
    alerts: list[AemetAlert] = []

    # Puede venir un único <alert> como raíz o varios embebidos.
    cap_roots = [root] if _tag_name(root) == "alert" else _findall_by_local(root, "alert")

    for ar in cap_roots:
        identifier = _child_text(ar, "identifier", "id", "guid")
        sent = _child_text(ar, "sent", "updated", "published")

        infos = [e for e in list(ar) if _tag_name(e) == "info"]
        if not infos:
            infos = [ar]

        for info in infos:
            event = _child_text(info, "event") or ""
            severity = _child_text(info, "severity")
            headline = _child_text(info, "headline", "title")
            desc = _child_text(info, "description", "summary")
            instruction = _child_text(info, "instruction")
            onset = _child_text(info, "onset", "effective") or _child_text(info, "effective") or sent
            expires = _child_text(info, "expires")

            area_descs = []
            for area in [e for e in list(info) if _tag_name(e) == "area"]:
                area_desc = _child_text(area, "areadesc", "areaDesc")
                if area_desc:
                    area_descs.append(area_desc)

            area_text = "; ".join(area_descs)
            full = _norm_spaces(" ".join([headline, event, desc, instruction, area_text, severity]))

            level = _level_from_text(full, severity)
            event_norm = _event_from_text(event or full)
            title = headline or f"AEMET {level} {event_norm}".strip()
            summary = desc or instruction or event or ""

            zone = _detect_zone_from_text(area_text or full)

            alert = AemetAlert(
                alert_id=identifier or hashlib.sha1(full.encode("utf-8", errors="ignore")).hexdigest(),
                title=title,
                summary=summary,
                link=source_link,
                level=level,
                event=event_norm,
                zone=zone,
                province="",
                region="",
                start=_parse_dt_any(onset),
                end=_parse_dt_any(expires),
                raw_text=full,
            )
            alerts.append(alert)

    return alerts


def _parse_rss_atom_alerts(root: ET.Element, source_url: str) -> list[AemetAlert]:
    """
    Parsea RSS/Atom de forma tolerante.

    Además, si un item trae link a CAP/XML y AEMET_ALERTS_FETCH_CAP_DETAILS=1,
    intenta descargar ese detalle para obtener campos más finos.
    """
    out: list[AemetAlert] = []
    fetch_cap_details = _env_bool("AEMET_ALERTS_FETCH_CAP_DETAILS", "1")

    # RSS items
    rss_items = _findall_by_local(root, "item")
    for item in rss_items:
        title = _child_text(item, "title")
        desc = _child_text(item, "description", "summary")
        link = _child_text(item, "link")
        guid = _child_text(item, "guid", "id")
        pub = _child_text(item, "pubDate", "updated", "published")

        # Intento de CAP enlazado.
        if fetch_cap_details and link and re.search(r"(cap|xml)", link, flags=re.I):
            try:
                cap_text = _fetch_cached(link)
                cap_root = _xml_root(cap_text)
                if cap_root is not None:
                    cap_alerts = _parse_cap_alert(cap_root, source_link=link)
                    if cap_alerts:
                        out.extend(cap_alerts)
                        continue
            except Exception:
                pass

        full = _norm_spaces(" ".join([title, desc, link]))
        level = _level_from_text(full)
        event = _event_from_text(full)

        out.append(AemetAlert(
            alert_id=guid or link or hashlib.sha1(full.encode("utf-8", errors="ignore")).hexdigest(),
            title=title or f"AEMET {level} {event}",
            summary=desc,
            link=link or source_url,
            level=level,
            event=event,
            zone=_detect_zone_from_text(full),
            province="",
            region="",
            start=_parse_dt_any(pub),
            end="",
            raw_text=full,
        ))

    # Atom entries
    atom_entries = _findall_by_local(root, "entry")
    for entry in atom_entries:
        title = _child_text(entry, "title")
        desc = _child_text(entry, "summary", "content")
        link = _atom_link(entry)
        guid = _child_text(entry, "id")
        updated = _child_text(entry, "updated", "published")

        if fetch_cap_details and link and re.search(r"(cap|xml)", link, flags=re.I):
            try:
                cap_text = _fetch_cached(link)
                cap_root = _xml_root(cap_text)
                if cap_root is not None:
                    cap_alerts = _parse_cap_alert(cap_root, source_link=link)
                    if cap_alerts:
                        out.extend(cap_alerts)
                        continue
            except Exception:
                pass

        full = _norm_spaces(" ".join([title, desc, link]))
        level = _level_from_text(full)
        event = _event_from_text(full)

        out.append(AemetAlert(
            alert_id=guid or link or hashlib.sha1(full.encode("utf-8", errors="ignore")).hexdigest(),
            title=title or f"AEMET {level} {event}",
            summary=desc,
            link=link or source_url,
            level=level,
            event=event,
            zone=_detect_zone_from_text(full),
            province="",
            region="",
            start=_parse_dt_any(updated),
            end="",
            raw_text=full,
        ))

    return out


def parse_aemet_alerts_from_text(text: str, source_url: str = "") -> list[AemetAlert]:
    """
    Parsea una respuesta AEMET:
    - CAP XML
    - RSS XML
    - Atom XML

    Seguridad 24/7:
    - Por defecto NO recorre páginas HTML índice.
    - Las páginas /rss_info/avisos/... de AEMET pueden ser páginas HTML de enlaces,
      no feeds XML directos.
    - Evita bloqueos por descubrimiento recursivo de enlaces.
    """
    text = text or ""
    root = _xml_root(text)

    if root is not None:
        local = _tag_name(root)

        if local == "alert" or _findall_by_local(root, "info"):
            cap = _parse_cap_alert(root, source_link=source_url)
            if cap:
                return cap

        alerts = _parse_rss_atom_alerts(root, source_url)
        if alerts:
            return alerts

        return []

    # HTML desactivado por defecto para producción 24/7.
    if not _env_bool("AEMET_ALERTS_PARSE_HTML_LINKS", "0"):
        return []

    alerts: list[AemetAlert] = []
    max_links = max(0, _env_int("AEMET_ALERTS_HTML_MAX_LINKS", 2))

    for url in _discover_feed_urls_from_info_page(text, source_url or DEFAULT_AEMET_INFO_URL)[:max_links]:
        try:
            sub = _fetch_cached(url)
            alerts.extend(parse_aemet_alerts_from_text(sub, source_url=url))
        except Exception:
            continue

    return alerts

def fetch_aemet_alerts() -> list[AemetAlert]:
    """
    Descarga y parsea avisos desde las URLs configuradas.
    """
    alerts: list[AemetAlert] = []
    for url in _configured_feed_urls():
        try:
            text = _fetch_cached(url)
            alerts.extend(parse_aemet_alerts_from_text(text, source_url=url))
        except Exception as e:
            if _env_int("AEMET_ALERTS_DEBUG", 0) >= 1:
                print(f"[aemet_alerts] Error leyendo {url}: {type(e).__name__}: {e}", flush=True)
            continue

    # Eliminar entradas informativas de AEMET que indican ausencia de avisos.
    # AEMET publica en los RSS de zona entradas tipo:
    #   "Estado completo de avisos para ... No hay avisos para ..."
    # No son alertas operativas y no deben pasar a la fase de filtrado/envío.
    operative_alerts: list[AemetAlert] = []
    for a in alerts:
        raw = _normalize_for_match(" ".join([
            a.title or "",
            a.summary or "",
            a.raw_text or "",
        ]))

        if "no hay avisos" in raw:
            continue

        operative_alerts.append(a)

    # Dedup por hash de alerta en la misma ejecución.
    unique: dict[str, AemetAlert] = {}
    for a in operative_alerts:
        h = _make_alert_hash(a)
        unique[h] = a

    return list(unique.values())


# =============================================================================
# Filtrado por zona/nivel/fenómeno
# =============================================================================

def _alert_matches_area(a: AemetAlert, zone: str, province: str, region: str) -> bool:
    """
    Filtra por zona/provincia/región.

    Si no hay zona configurada, admite todos.
    Si el feed no trae área clara, se busca en título/resumen/raw.
    """
    needles = [_normalize_for_match(x) for x in (zone, province, region) if _normalize_for_match(x)]
    if not needles:
        return True

    hay = _normalize_for_match(" ".join([
        a.title,
        a.summary,
        a.zone,
        a.province,
        a.region,
        a.raw_text,
    ]))

    return any(n in hay for n in needles)


def _alert_matches_level(a: AemetAlert) -> bool:
    allowed = _csv_set(_env_str("AEMET_ALERTS_LEVELS", "amarillo,naranja,rojo"))
    if not allowed:
        return True
    return (a.level or "desconocido").lower() in allowed


def _alert_matches_event(a: AemetAlert) -> bool:
    allowed = _csv_set(_env_str("AEMET_ALERTS_EVENTS", ""))
    if not allowed:
        return True
    ev = _normalize_for_match(a.event)
    raw = _normalize_for_match(a.raw_text)
    return any(x in ev or x in raw for x in allowed)


def filter_aemet_alerts(alerts: list[AemetAlert], meta: dict) -> list[AemetAlert]:
    """
    Aplica filtros por zona, nivel y fenómeno.
    """
    zone = str(meta.get("zone") or meta.get("city") or meta.get("location") or _env_str("AEMET_ALERTS_DEFAULT_ZONE", "")).strip()
    province = str(meta.get("province") or _env_str("AEMET_ALERTS_DEFAULT_PROVINCE", "")).strip()
    region = str(meta.get("region") or _env_str("AEMET_ALERTS_DEFAULT_REGION", "")).strip()

    out: list[AemetAlert] = []
    for a in alerts:
        if not _alert_matches_level(a):
            continue
        if not _alert_matches_event(a):
            continue
        if not _alert_matches_area(a, zone, province, region):
            continue
        out.append(a)

    # Orden operativo: rojo > naranja > amarillo > desconocido
    rank = {"rojo": 0, "naranja": 1, "amarillo": 2, "desconocido": 3}
    out.sort(key=lambda x: (rank.get((x.level or "").lower(), 9), x.event, x.zone))
    return out


# =============================================================================
# Estado / anti-duplicados
# =============================================================================

def _state_scope(meta: dict) -> str:
    """
    Scope de dedupe por zona y transporte.

    Esto permite que una misma alerta se pueda emitir en dos tareas distintas
    si el usuario programa canales distintos.
    """
    zone = str(meta.get("zone") or meta.get("city") or meta.get("location") or _env_str("AEMET_ALERTS_DEFAULT_ZONE", "default")).strip()
    transport = str(meta.get("transport") or "mesh").strip().lower()

    if transport == "meshcore":
        mode = str(meta.get("meshcore_mode") or "").strip().lower()
        if mode == "dm":
            dst = f"mc_dm:{meta.get('meshcore_contact')}"
        else:
            dst = f"mc_ch:{meta.get('meshcore_channel_idx')}"
    else:
        dst = f"mesh_ch:{meta.get('channel') or meta.get('mesh_channel') or ''}"

    return _normalize_for_match(f"{zone}|{transport}|{dst}") or "default"


def _cleanup_state(state: dict) -> dict:
    """
    Elimina avisos antiguos del estado local.
    """
    retention_days = _env_int("AEMET_ALERTS_STATE_RETENTION_DAYS", 7)
    if retention_days <= 0:
        return state

    cutoff = int(time.time()) - retention_days * 86400
    scopes = state.get("scopes")
    if not isinstance(scopes, dict):
        return {"scopes": {}}

    for scope, data in list(scopes.items()):
        if not isinstance(data, dict):
            scopes.pop(scope, None)
            continue
        sent = data.get("sent")
        if not isinstance(sent, dict):
            data["sent"] = {}
            continue
        for h, rec in list(sent.items()):
            try:
                ts = int((rec or {}).get("sent_ts") or 0)
            except Exception:
                ts = 0
            if ts and ts < cutoff:
                sent.pop(h, None)

    return state


def _select_new_alerts(alerts: list[AemetAlert], meta: dict) -> list[AemetAlert]:
    """
    Aplica anti-duplicado:
    - No repite el mismo aviso hasta AEMET_ALERTS_REPEAT_HOURS.
    - Respeta cooldown global por tarea.
    - Limita a AEMET_ALERTS_MAX_PER_RUN.
    """
    state_path = _env_str("AEMET_ALERTS_STATE_PATH", os.path.join("bot_data", "aemet_alerts_state.json"))
    state = _cleanup_state(_read_json(state_path))

    scopes = state.setdefault("scopes", {})
    scope = _state_scope(meta)
    data = scopes.setdefault(scope, {"sent": {}, "last_sent_ts": 0})

    sent = data.setdefault("sent", {})
    now = int(time.time())

    cooldown_min = _env_int("AEMET_ALERTS_COOLDOWN_MIN", 15)
    last_sent_ts = int(data.get("last_sent_ts") or 0)
    if cooldown_min > 0 and last_sent_ts and (now - last_sent_ts) < cooldown_min * 60:
        return []

    repeat_hours = _env_int("AEMET_ALERTS_REPEAT_HOURS", 6)
    max_per_run = max(1, _env_int("AEMET_ALERTS_MAX_PER_RUN", 2))

    selected: list[AemetAlert] = []

    for a in alerts:
        h = _make_alert_hash(a)
        old = sent.get(h)
        if isinstance(old, dict):
            old_ts = int(old.get("sent_ts") or 0)
            if repeat_hours > 0 and old_ts and (now - old_ts) < repeat_hours * 3600:
                continue

        selected.append(a)
        if len(selected) >= max_per_run:
            break

    return selected


def _alert_state_record(a: AemetAlert) -> dict:
    return {
        "hash": _make_alert_hash(a),
        "level": a.level,
        "event": a.event,
        "zone": a.zone,
        "start": a.start,
        "end": a.end,
        "title": a.title[:160],
    }


def mark_aemet_alerts_sent(alert_records: list[dict], meta: dict) -> None:
    """
    Confirma en el estado local que los avisos se han transmitido correctamente.

    Se llama desde broker_task.py solo después de que el transporte elegido haya
    terminado sin error. Así, un fallo de RF/MeshCore no consume el aviso ni
    bloquea el siguiente reintento por cooldown/repeat_hours.
    """
    if not alert_records:
        return

    state_path = _env_str("AEMET_ALERTS_STATE_PATH", os.path.join("bot_data", "aemet_alerts_state.json"))
    state = _cleanup_state(_read_json(state_path))
    scopes = state.setdefault("scopes", {})
    scope = _state_scope(meta or {})
    data = scopes.setdefault(scope, {"sent": {}, "last_sent_ts": 0})
    sent = data.setdefault("sent", {})
    now = int(time.time())

    changed = False
    for rec in alert_records:
        if not isinstance(rec, dict):
            continue
        h = str(rec.get("hash") or "").strip()
        if not h:
            continue
        sent[h] = {
            "sent_ts": now,
            "level": rec.get("level") or "",
            "event": rec.get("event") or "",
            "zone": rec.get("zone") or "",
            "start": rec.get("start") or "",
            "end": rec.get("end") or "",
            "title": str(rec.get("title") or "")[:160],
        }
        changed = True

    if changed:
        data["last_sent_ts"] = now
        _write_json_atomic(state_path, state)


def mark_aemet_alerts_sent_from_result(result: dict, meta: dict) -> None:
    mark_aemet_alerts_sent((result or {}).get("alerts") or [], meta or {})


# =============================================================================
# Formato de salida RF
# =============================================================================

def _tz_for_meta(meta: dict):
    tz_name = str(meta.get("timezone") or _env_str("AEMET_ALERTS_TZ", "Europe/Madrid") or "Europe/Madrid")
    if ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return timezone(timedelta(hours=2))


def _fmt_dt_short(s: str, meta: dict) -> str:
    """
    Fecha corta para RF.
    """
    raw = _norm_spaces(s)
    if not raw:
        return "?"

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(_tz_for_meta(meta))
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        pass

    # Si no es ISO, acortar texto original.
    return raw[:16]


def _aemet_short_detail(a: AemetAlert) -> str:
    """
    Construye un detalle AEMET corto y estable para RF.

    Uso interno:
        detalle = _aemet_short_detail(alerta)

    Parámetros:
        a:
            AemetAlert ya normalizado.

    Funcionalidad:
        - Evita reenviar descripciones largas de AEMET.
        - Elimina frases redundantes tipo "Aviso de ... de nivel amarillo".
        - Reduce fenómenos frecuentes a una frase corta.
        - Mantiene el mensaje apto para LoRa/MeshCore sin depender del troceo posterior.
    """
    event = (a.event or "").strip().lower()
    level = (a.level or "").strip().lower()
    raw = _strip_html(a.summary or a.title or "")
    raw_norm = _normalize_for_match(raw)

    if "temperatura" in raw_norm or event == "temperaturas":
        if "maxima" in raw_norm or "maximas" in raw_norm or "calor" in raw_norm:
            return "Temp. máxima"
        if "minima" in raw_norm or "minimas" in raw_norm or "frio" in raw_norm or "helada" in raw_norm:
            return "Temp. mínima"
        return "Temperaturas"

    if "tormenta" in raw_norm or event == "tormentas":
        return "Tormentas"

    if "lluvia" in raw_norm or "precipitacion" in raw_norm or event == "lluvia":
        return "Lluvia"

    if "viento" in raw_norm or "racha" in raw_norm or event == "viento":
        return "Viento"

    if "nieve" in raw_norm or event == "nieve":
        return "Nieve"

    if "costero" in raw_norm or "oleaje" in raw_norm or event == "costeros":
        return "Fenómeno costero"

    if "niebla" in raw_norm or event == "niebla":
        return "Niebla"

    if event and event != "fenómeno adverso":
        return event.capitalize()

    if level and level != "desconocido":
        return f"Aviso {level}"

    return "Aviso meteorológico"


def _format_one_alert(a: AemetAlert, meta: dict) -> str:
    """
    Formatea UN aviso AEMET en una sola línea corta para RF.

    Uso interno:
        msg = _format_one_alert(alerta, meta)

    Parámetros:
        a:
            Aviso AEMET normalizado.
        meta:
            Metadatos de la tarea programada.

    Funcionalidad:
        - Genera un texto compacto para Meshtastic/MeshCore/APRS.
        - Evita que Companion reciba mensajes partidos tipo (1/2), (2/2).
        - Mantiene compatibilidad con AEMET_ALERTS_TEMPLATE si se define en .env.
        - Por defecto usa una plantilla corta y no el resumen largo de AEMET.
    """
    zone = str(
        meta.get("zone")
        or meta.get("city")
        or meta.get("location")
        or a.zone
        or _env_str("AEMET_ALERTS_DEFAULT_ZONE", "zona")
    ).strip()

    nivel = (a.level or "aviso").upper()
    fenomeno = a.event or "fenómeno"
    inicio = _fmt_dt_short(a.start, meta)
    fin = _fmt_dt_short(a.end, meta)

    # Detalle RF corto. No usar directamente a.summary porque AEMET genera textos largos.
    detail = _aemet_short_detail(a)

    red_as_emergency = _env_bool("AEMET_ALERTS_RED_AS_EMERGENCY", "1")
    if red_as_emergency and (a.level or "").lower() == "rojo":
        template = _env_str(
            "AEMET_ALERTS_EMERGENCY_TEMPLATE",
            "EMERGENCIA AEMET {zona}: {detalle}. {inicio}-{fin}. Revisar nodos exteriores.",
        )
    else:
        template = _env_str(
            "AEMET_ALERTS_TEMPLATE",
            "AEMET {nivel} {zona}: {detalle}. {inicio}-{fin}.",
        )

    msg = template.format(
        nivel=nivel,
        zona=zone,
        fenomeno=fenomeno,
        inicio=inicio,
        fin=fin,
        detalle=detail,
        titulo=a.title,
    )

    if _env_bool("AEMET_ALERTS_ASCII_SAFE", "0"):
        msg = _ascii_safe(msg)

    # Límite pensado para que normalmente quepa en una sola trama MeshCore.
    max_chars = _env_int("AEMET_ALERTS_MAX_CHARS", 135)
    return _clip_text(msg, max_chars)

def build_aemet_alert_result_from_meta(meta: Dict[str, Any]) -> dict:
    """
    Construye el texto RF de avisos AEMET y devuelve los avisos pendientes.

    Uso:
        result = build_aemet_alert_result_from_meta(task.meta)

    Parámetros:
        meta:
            Diccionario persistido por broker_task.schedule_message.

    Devuelve:
        - {"text": "...", "alerts": [...]} si hay avisos nuevos.
        - {"text": "", "alerts": []} si no hay avisos nuevos o si está desactivado.

    Importante:
        Esta función NO envía RF y NO marca avisos como enviados.
    """
    meta = dict(meta or {})

    if not _env_bool("AEMET_ALERTS_ENABLED", "1"):
        return {"text": "", "alerts": []}

    alerts = fetch_aemet_alerts()
    if not alerts:
        return {"text": "", "alerts": []}

    filtered = filter_aemet_alerts(alerts, meta)
    if not filtered:
        return {"text": "", "alerts": []}

    selected = _select_new_alerts(filtered, meta)
    if not selected:
        return {"text": "", "alerts": []}

    messages = [_format_one_alert(a, meta) for a in selected]
    messages = [m for m in messages if m.strip()]
    if not messages:
        return {"text": "", "alerts": []}

    # Cada aviso en línea separada. broker_task ya trocea por bytes si hace falta.
    return {
        "text": "\n".join(messages).strip(),
        "alerts": [_alert_state_record(a) for a in selected],
    }


def build_aemet_alert_text_from_meta(meta: Dict[str, Any]) -> str:
    """
    Construye el texto RF de avisos AEMET para una tarea programada.

    Uso:
        text = build_aemet_alert_text_from_meta(task.meta)

    Parámetros:
        meta:
            Diccionario persistido por broker_task.schedule_message.

    Devuelve:
        - Texto listo para transmitir si hay aviso nuevo.
        - "" si no hay avisos nuevos o si está desactivado.

    Funcionalidad:
        - Consulta avisos oficiales AEMET.
        - Filtra por zona/provincia/región.
        - Selecciona avisos nuevos según estado persistente.
        - Devuelve SOLO UN aviso por ejecución, para evitar tramas partidas
          y mensajes confusos en Companion.
        - No envía RF directamente.
    """
    meta = dict(meta or {})

    if not _env_bool("AEMET_ALERTS_ENABLED", "1"):
        return ""

    alerts = fetch_aemet_alerts()
    if not alerts:
        return ""

    filtered = filter_aemet_alerts(alerts, meta)
    if not filtered:
        return ""

    selected = _select_new_alerts(filtered, meta)
    if not selected:
        return ""

    # Cambio quirúrgico:
    # Antes se formateaban varios avisos y se unían con "\n".
    # Eso provocaba que MeshCore/Companion mostrase (1/2), (2/2).
    # Ahora solo se transmite el aviso más relevante por ejecución.
    first = selected[0]

    msg = _format_one_alert(first, meta)
    msg = _norm_spaces(msg)

    if not msg:
        return ""

    return msg


def debug_aemet_alerts(meta: Optional[Dict[str, Any]] = None) -> dict:
    """
    Diagnóstico manual.

    Uso desde consola Python:
        import aemet_alerts
        print(aemet_alerts.debug_aemet_alerts({"zone":"Zaragoza"}))

    Devuelve:
        Resumen de URLs, avisos totales y avisos filtrados.
    """
    meta = dict(meta or {})
    urls = _configured_feed_urls()
    alerts = fetch_aemet_alerts()
    filtered = filter_aemet_alerts(alerts, meta)
    return {
        "ok": True,
        "urls": urls,
        "total": len(alerts),
        "filtered": len(filtered),
        "alerts": [asdict(a) for a in filtered[:10]],
    }
