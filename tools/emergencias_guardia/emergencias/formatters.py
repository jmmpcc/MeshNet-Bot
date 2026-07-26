from __future__ import annotations

from .models import Event


CATEGORY_LABELS = {
    "wildfire": "INCENDIO", "urban_fire": "INCENDIO URBANO",
    "industrial_fire": "INCENDIO INDUSTRIAL", "traffic_collision": "COLISIÓN",
    "road_closed": "CARRETERA CORTADA", "lane_closed": "CARRIL CERRADO",
    "traffic_obstruction": "OBSTÁCULO", "flood": "INUNDACIÓN",
    "storm": "TORMENTA", "snow": "NIEVE", "strong_wind": "VIENTO",
    "extreme_temperature": "TEMPERATURA EXTREMA", "chemical": "RIESGO QUÍMICO",
    "power_outage": "CORTE ELÉCTRICO", "water_outage": "CORTE DE AGUA",
    "gas_outage": "CORTE DE GAS", "public_safety": "SEGURIDAD",
    "civil_protection": "PROTECCIÓN CIVIL", "other": "INCIDENCIA",
}


def format_event(event: Event) -> list[str]:
    label = CATEGORY_LABELS.get(event.category, "INCIDENCIA")
    lines = [f"{event.severity.upper()} · {label}", event.title]
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
    lines.append(f"Fuente: {event.source}")
    return lines


def byte_chunks(events: list[Event], max_bytes: int = 140) -> list[str]:
    blocks = ["\n".join(format_event(event)) for event in events]
    if not blocks:
        return ["EMERG\nSin emergencias activas."]
    parts: list[str] = []
    for block in blocks:
        parts.extend(_split_text(block, max(40, max_bytes - 15)))
    if len(parts) == 1:
        return [f"EMERG\n{parts[0]}"]
    result = []
    for index, part in enumerate(parts, 1):
        header = f"EMERG [{index}/{len(parts)}]\n"
        result.append(header + _trim_utf8(part, max_bytes - len(header.encode("utf-8"))))
    return result


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
