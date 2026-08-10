from __future__ import annotations

from datetime import datetime
import math
import unicodedata

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
    "civil_protection": "PROTECCIÓN CIVIL", "earthquake": "TERREMOTO",
    "tsunami": "TSUNAMI", "volcanic": "VOLCÁN", "landslide": "DESLIZAMIENTO",
    "other": "INCIDENCIA",
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
    "ign_earthquakes": "IGN",
}


# Etiquetas APRS deliberadamente breves y sin ambigüedad. El objetivo no es
# codificar la emergencia con siglas opacas, sino garantizar que el tipo de
# incidente aparezca completo al principio de una trama APRS de 67 caracteres.
APRS_CATEGORY_LABELS = {
    "wildfire": "INCENDIO",
    "urban_fire": "INCENDIO URB",
    "industrial_fire": "INCENDIO IND",
    "traffic_collision": "COLISION",
    "road_closed": "CORTE VIA",
    "lane_closed": "CARRIL CERRADO",
    "traffic_obstruction": "AFECCION VIAL",
    "flood": "INUNDACION",
    "storm": "TORMENTA",
    "snow": "NIEVE",
    "strong_wind": "VIENTO",
    "extreme_temperature": "TEMP EXTREMA",
    "chemical": "RIESGO QUIMICO",
    "power_outage": "CORTE ELECTRICO",
    "water_outage": "CORTE AGUA",
    "gas_outage": "CORTE GAS",
    "public_safety": "SEGURIDAD",
    "civil_protection": "PROT CIVIL",
    "earthquake": "TERREMOTO",
    "tsunami": "TSUNAMI",
    "volcanic": "VOLCAN",
    "landslide": "DESLIZAMIENTO",
    "other": "INCIDENCIA",
}

APRS_TERMINAL_STATUSES = {"resolved", "cancelled", "expired", "closed"}


def aprs_emergency_text(event: Event, max_chars: int = 67) -> str:
    """Construye el texto APRS compacto de una emergencia.

    Uso:
      ``text = aprs_emergency_text(event, max_chars=67)``

    Parámetros:
      event: evento normalizado de ``emergencias_guardia``.
      max_chars: longitud máxima del cuerpo APRS. El valor recomendado y por
        defecto es 67, límite del campo de texto de un mensaje APRS clásico.

    Funcionalidad:
      - Conserva SIEMPRE al comienzo el estado operativo y el tipo de emergencia.
      - Usa ``FIN`` para estados terminales y ``CRIT`` para severidad crítica;
        el resto de emergencias publicables utiliza ``EMERG``.
      - Prioriza carretera/km, municipio y provincia sobre detalles narrativos.
      - Solo añade el título cuando aporta información distinta del tipo/lugar.
      - Convierte a ASCII seguro para APRS y nunca corta el tipo de emergencia.
      - Intenta producir una única trama APRS, reduciendo airtime RF y evitando
        que APRS-IS trunque precisamente la parte que identifica la incidencia.
    """
    limit = max(24, int(max_chars or 67))
    status = str(event.status or "active").strip().lower()
    if status in APRS_TERMINAL_STATUSES:
        prefix = "FIN"
    elif str(event.severity or "").strip().lower() == "critical":
        prefix = "CRIT"
    else:
        prefix = "EMERG"

    category = APRS_CATEGORY_LABELS.get(event.category, "INCIDENCIA")
    mandatory = _aprs_ascii_text(f"{prefix} {category}")
    if len(mandatory) >= limit:
        return mandatory[:limit].rstrip()

    road = _aprs_ascii_text(event.road)
    if road and event.kilometre is not None:
        road = f"{road} km {event.kilometre:g}"
    municipality = _aprs_ascii_text(event.municipality)
    province = _aprs_ascii_text(event.province)

    # Orden de prioridad: ubicación operativa primero. La provincia solo se
    # añade si no duplica el municipio.
    candidates: list[str] = []
    if road:
        candidates.append(road)
    if municipality:
        candidates.append(municipality)
    elif province:
        candidates.append(province)

    title = _aprs_ascii_text(event.title)
    normalized_category = category.casefold()
    normalized_location = " ".join(candidates).casefold()
    if (
        title
        and title.casefold() not in {"incidencia", normalized_category}
        and title.casefold() not in normalized_location
        and normalized_category not in title.casefold()
    ):
        candidates.append(title)

    result = mandatory
    for candidate in candidates:
        candidate = " ".join(candidate.split())
        if not candidate:
            continue
        separator = " | "
        remaining = limit - len(result) - len(separator)
        if remaining <= 0:
            break
        if len(candidate) <= remaining:
            result += separator + candidate
            continue
        # Si es el primer dato de ubicación, usamos el espacio restante en vez
        # de descartarlo por completo. El tipo ya está asegurado en ``mandatory``.
        if result == mandatory and remaining >= 6:
            result += separator + candidate[:remaining].rstrip(" ,.;:-")
        break
    return result[:limit].rstrip(" ,.;:-")


def _aprs_ascii_text(value: str) -> str:
    """Normaliza texto a ASCII imprimible y compacta espacios para APRS."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii", "ignore")
    text = "".join(ch if 32 <= ord(ch) <= 126 and ch not in "|~{" else " " for ch in text)
    return " ".join(text.split())


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
    map_url = google_maps_url(event)
    if map_url:
        lines.append(f"Mapa: {map_url}")
    lines.append(f"Fuente: {source_label(event.source)}")
    return lines


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source.replace("_", " ").strip().title())


def google_maps_url(event: Event) -> str:
    """Devuelve un enlace compacto solo para coordenadas geográficas válidas."""
    if event.latitude is None or event.longitude is None:
        return ""
    latitude = float(event.latitude)
    longitude = float(event.longitude)
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return ""
    return (
        "https://maps.google.com/?q="
        f"{_coordinate(latitude)},{_coordinate(longitude)}"
    )


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
    map_url = google_maps_url(event)

    required = [header, f"{severity} · {category}", _trim_text(place or event.title, 55)]
    if map_url:
        required.append(map_url)
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

    fixed_lines = required[:2] + ([map_url] if map_url else []) + ([footer] if footer else [])
    fixed = "\n".join(fixed_lines)
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


def _coordinate(value: float) -> str:
    return f"{value:.5f}".rstrip("0").rstrip(".")
