from __future__ import annotations

from datetime import datetime

from .models import Event


CATEGORY_LABELS = {
    "wildfire": "INCENDIO", "urban_fire": "INCENDIO URBANO",
    "industrial_fire": "INCENDIO INDUSTRIAL", "traffic_collision": "COLISIÓN",
    "road_closed": "CORTE DE TRÁFICO", "lane_closed": "CARRIL CERRADO",
    "traffic_obstruction": "AFECCIÓN VIAL", "flood": "INUNDACIÓN",
    "storm": "TORMENTA", "snow": "NIEVE", "strong_wind": "VIENTO",
    "extreme_temperature": "TEMPERATURA EXTREMA", "chemical": "RIESGO QUÍMICO",
    "power_outage": "CORTE ELÉCTRICO", "water_outage": "CORTE DE AGUA",
    "gas_outage": "CORTE DE GAS", "public_safety": "SEGURIDAD",
    "civil_protection": "PROTECCIÓN CIVIL", "other": "INCIDENCIA",
}

SEVERITY_LABELS = {
    "low": "BAJA", "medium": "MEDIA", "high": "ALTA", "critical": "CRÍTICA",
}

SOURCE_LABELS = {
    "municipal_json": "Ayto. Zaragoza",
    "dgt_datex": "DGT",
    "aemet": "AEMET",
    "nasa_firms": "NASA FIRMS",
    "copernicus_effis": "EFFIS",
    "ran": "Protección Civil",
    "infoar": "INFOAR",
}


def format_event(event: Event) -> list[str]:
    label = CATEGORY_LABELS.get(event.category, "INCIDENCIA")
    severity = SEVERITY_LABELS.get(event.severity, event.severity.upper())
    lines = [f"{severity} · {label}", event.title]
    location = " ".join(part for part in (
        event.road, f"km {event.kilometre:g}" if event.kilometre is not None else "",
    ) if part)
    place = ", ".join(part for part in (event.municipality, event.province) if part)
    if location:
        lines.append(location)
    if place:
        lines.append(place)
    if event.description and event.description != event.title:
        lines.append(event.description)
    if event.verification == "satellite_detection":
        lines.append("Detección térmica no confirmada oficialmente.")
    lines.append(f"Fuente: {source_label(event.source)}")
    return lines


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source.replace("_", " ").strip().title())


def short_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d/%m")
    except ValueError:
        return value[:10]


def compact_messages(
    events: list[Event],
    max_bytes: int = 140,
    prefix: str = "EMERG",
) -> list[str]:
    """Genera un mensaje autocontenido por evento, nunca una parte huérfana."""
    if not events:
        return [f"{prefix}\nSin emergencias activas."]
    total = len(events)
    return [
        _compact_event(
            event,
            prefix if total == 1 else f"{prefix} [{index}/{total}]",
            max_bytes,
        )
        for index, event in enumerate(events, 1)
    ]


def byte_chunks(events: list[Event], max_bytes: int = 140) -> list[str]:
    """Compatibilidad: las consultas DM usan el formato compacto."""
    return compact_messages(events, max_bytes=max_bytes, prefix="EMERG")


def _compact_event(event: Event, header: str, max_bytes: int) -> str:
    severity = SEVERITY_LABELS.get(event.severity, event.severity.upper())
    category = CATEGORY_LABELS.get(event.category, "INCIDENCIA")
    place = _compact_place(event)
    detail = _sentence(event.description, 72)
    end = short_date(event.expected_end)
    source = source_label(event.source)

    required = [header, f"{severity} · {category}", _trim_text(place or event.title, 55)]
    footer = " · ".join(part for part in (
        f"Hasta {end}" if end else "",
        source,
    ) if part)
    optional = []
    if place and event.title and event.title.casefold() not in place.casefold():
        optional.append(_trim_text(event.title, 52))
    if detail and detail.casefold() != event.title.casefold():
        optional.append(detail)

    lines = required + optional + ([footer] if footer else [])
    while len("\n".join(lines).encode("utf-8")) > max_bytes and optional:
        optional.pop()
        lines = required + optional + ([footer] if footer else [])
    message = "\n".join(lines)
    if len(message.encode("utf-8")) <= max_bytes:
        return message

    fixed = "\n".join(required[:2] + ([footer] if footer else []))
    allowance = max(12, max_bytes - len(fixed.encode("utf-8")) - 1)
    required[2] = _trim_utf8(required[2], allowance)
    message = "\n".join(required + ([footer] if footer else []))
    return _trim_utf8(message, max_bytes)


def _compact_place(event: Event) -> str:
    road = event.road
    if road and event.kilometre is not None:
        road = f"{road} km {event.kilometre:g}"
    place = " · ".join(part for part in (road, event.municipality) if part)
    if not place and event.province:
        place = event.province
    return place


def _sentence(value: str, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    for separator in (". ", "; ", "\n"):
        if separator in text:
            text = text.split(separator, 1)[0].rstrip(".")
            break
    return _trim_text(text, maximum)


def _trim_text(text: str, maximum: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= maximum:
        return text
    return text[:max(1, maximum - 1)].rstrip(" ,.;:-") + "…"


def _split_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}".strip()
        if current and len(candidate.encode("utf-8")) > limit:
            chunks.append(current)
            current = ""
        while len(line.encode("utf-8")) > limit:
            piece = _trim_utf8(line, limit)
            chunks.append(piece)
            line = line[len(piece):].lstrip()
        current = f"{current}\n{line}".strip()
    if current:
        chunks.append(current)
    return chunks


def _trim_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip()
