from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

from ..models import Event, clean_text
from ..storage import load_current
from .firms import FirmsSource


_TERMINAL_STATUSES = {"resolved", "cancelled", "expired", "closed"}
_CONFIDENCE_RANK = {"l": 10, "n": 20, "h": 30}


class FirmsTrackedSource(FirmsSource):
    """NASA FIRMS con continuidad de foco entre pasadas satelitales.

    Uso:
        Se registra como implementación del tipo ``firms`` en ``SOURCE_TYPES``.
        El motor la instancia igual que al conector FIRMS histórico.

    Configuración opcional de ``sources.nasa_firms``:
        incident_tracking_enabled: activa esta capa; por defecto true.
        incident_radius_km: radio para correlacionar pasadas; por defecto 8 km.
        incident_max_gap_hours: separación temporal máxima; por defecto 24 h.
        growth_frp_ratio: aumento relativo mínimo de FRP; por defecto 25 %.
        growth_frp_min_mw: aumento absoluto mínimo de FRP; por defecto 5 MW.
        growth_extent_ratio: aumento relativo mínimo de extensión; por defecto 20 %.
        growth_extent_min_km: aumento absoluto mínimo de extensión; por defecto 0,5 km.

    Funcionalidad:
        Reutiliza íntegramente ``FirmsSource`` para descarga, normalización y
        agrupación de píxeles. Sobre los clusters resultantes mantiene un
        identificador persistente por foco y clasifica cada nueva pasada como
        inicio, aumento o seguimiento estable. No cambia nunca
        ``verification=satellite_detection``.
    """

    def parse(self, body: bytes) -> list[Event]:
        """Parsea el CSV FIRMS y correlaciona sus clusters con focos activos.

        Parámetros:
            body: CSV original descargado desde NASA FIRMS.

        Retorna:
            Un evento por foco observado en la consulta actual. Los clusters de
            pasadas posteriores que correspondan al mismo foco conservan el
            ``event_id`` anterior para reutilizar la deduplicación existente.
        """
        events = super().parse(body)
        if not events:
            return []
        if not _config_bool(self.config.get("incident_tracking_enabled", True), True):
            return events
        return self._track_incidents(events, load_current())

    def _aggregate_cluster(
        self,
        members: list[Event],
        radius_km: float,
        time_minutes: float,
    ) -> Event:
        """Añade la extensión real al cluster producido por ``FirmsSource``.

        La agrupación histórica no se altera. Una vez creado el evento se
        calcula el diámetro máximo Haversine entre sus detecciones y se guarda
        en ``metadata['cluster_extent_km']``.
        """
        event = super()._aggregate_cluster(members, radius_km, time_minutes)
        event.metadata["cluster_extent_km"] = _cluster_extent_km(members)
        return event

    def _track_incidents(
        self,
        events: list[Event],
        current: dict[str, Event],
    ) -> list[Event]:
        """Relaciona clusters nuevos con focos FIRMS activos previos.

        Los clusters se procesan cronológicamente. Como candidatos se utilizan
        los focos activos persistidos y los ya evolucionados durante esta misma
        consulta. La asociación exige simultáneamente proximidad espacial y
        temporal y selecciona el candidato más cercano.
        """
        radius_km = _bounded_float(
            self.config.get("incident_radius_km", 8.0), 0.5, 50.0, 8.0
        )
        max_gap_hours = _bounded_float(
            self.config.get("incident_max_gap_hours", 24.0), 1.0, 168.0, 24.0
        )
        max_gap_seconds = max_gap_hours * 3600.0

        tracked = {
            event.event_id: event
            for event in current.values()
            if _is_active_firms_event(event, self.source_id)
        }
        observed_ids: set[str] = set()

        for event in sorted(events, key=lambda item: (_event_timestamp(item), item.event_id)):
            previous = _best_incident_match(
                event,
                tracked.values(),
                radius_km=radius_km,
                max_gap_seconds=max_gap_seconds,
            )
            evolved = (
                self._initial_incident(event)
                if previous is None
                else self._evolve_incident(previous, event)
            )
            tracked[evolved.event_id] = evolved
            observed_ids.add(evolved.event_id)

        result = [tracked[event_id] for event_id in observed_ids]
        result.sort(key=lambda event: (event.started_at or "", event.event_id))
        return result

    def _initial_incident(self, event: Event) -> Event:
        """Prepara la primera observación para emitir alerta temprana.

        Mantiene el evento FIRMS como detección satelital no confirmada y añade
        máximos iniciales de detecciones, FRP y extensión para las comparaciones
        de pasadas posteriores.
        """
        count = _metadata_int(event, "detection_count")
        frp_total = _event_frp_total(event)
        extent = _metadata_float(event, "cluster_extent_km") or 0.0
        detected_at = event.updated_at or event.started_at

        event.title = "Inicio de posible foco de incendio satelital"
        event.description = _phase_description("Inicio", count, frp_total, extent)
        event.metadata.update({
            "firms_phase": "initial",
            "growth_reasons": [],
            "incident_first_detection_at": detected_at,
            "incident_last_detection_at": detected_at,
            "incident_last_latitude": event.latitude,
            "incident_last_longitude": event.longitude,
            "incident_passes": 1,
            "previous_detection_count": 0,
            "previous_frp_total_mw": None,
            "previous_extent_km": 0.0,
            "incident_peak_detection_count": count,
            "incident_peak_frp_total_mw": frp_total,
            "incident_peak_extent_km": extent,
            "latest_detection_count": count,
            "latest_frp_total_mw": frp_total,
            "latest_extent_km": extent,
        })
        _refresh_event_hashes(event)
        return event

    def _evolve_incident(self, previous: Event, observed: Event) -> Event:
        """Evalúa una nueva pasada mediante detecciones, FRP y extensión.

        Si hay crecimiento significativo se conserva el ``event_id`` y se
        genera un contenido nuevo, por lo que el notificador existente lo verá
        como ``updated``. Si no hay crecimiento se preservan los campos que
        participan en ``Event.content_hash`` y el ``raw_hash`` anterior; solo se
        actualizan metadatos internos y no se emite una alerta repetida.
        """
        current_count = _metadata_int(observed, "detection_count")
        current_frp = _event_frp_total(observed)
        current_extent = _metadata_float(observed, "cluster_extent_km") or 0.0

        peak_count = max(
            _metadata_int(previous, "incident_peak_detection_count"),
            _metadata_int(previous, "detection_count"),
        )
        peak_frp = _first_float(
            previous.metadata.get("incident_peak_frp_total_mw"),
            previous.metadata.get("frp_total_mw"),
            previous.metadata.get("frp_mw"),
        )
        peak_extent = _first_float(
            previous.metadata.get("incident_peak_extent_km"),
            previous.metadata.get("cluster_extent_km"),
        ) or 0.0

        reasons: list[str] = []
        if current_count > peak_count:
            reasons.append("detections")

        if _significant_growth(
            current_frp,
            peak_frp,
            _bounded_float(self.config.get("growth_frp_ratio", 0.25), 0.0, 5.0, 0.25),
            _bounded_float(self.config.get("growth_frp_min_mw", 5.0), 0.0, 10000.0, 5.0),
        ):
            reasons.append("frp")

        if _significant_growth(
            current_extent,
            peak_extent,
            _bounded_float(self.config.get("growth_extent_ratio", 0.20), 0.0, 5.0, 0.20),
            _bounded_float(self.config.get("growth_extent_min_km", 0.5), 0.0, 50.0, 0.5),
        ):
            reasons.append("extent")

        previous_confidence = clean_text(previous.metadata.get("confidence")).casefold()
        current_confidence = clean_text(observed.metadata.get("confidence")).casefold()
        if _CONFIDENCE_RANK.get(current_confidence, 0) > _CONFIDENCE_RANK.get(previous_confidence, 0):
            reasons.append("confidence")

        detected_at = observed.updated_at or observed.started_at
        old_detected_at = clean_text(previous.metadata.get("incident_last_detection_at"))
        pass_increment = 1 if detected_at and detected_at != old_detected_at else 0
        passes = max(1, _metadata_int(previous, "incident_passes")) + pass_increment

        merged_metadata = dict(previous.metadata)
        merged_metadata.update(observed.metadata)
        merged_metadata.update({
            "firms_phase": "growth" if reasons else "stable",
            "growth_reasons": reasons,
            "incident_first_detection_at": clean_text(
                previous.metadata.get("incident_first_detection_at")
            ) or previous.started_at,
            "incident_last_detection_at": detected_at,
            "incident_last_latitude": observed.latitude,
            "incident_last_longitude": observed.longitude,
            "incident_passes": passes,
            "previous_detection_count": peak_count,
            "previous_frp_total_mw": peak_frp,
            "previous_extent_km": peak_extent,
            "incident_peak_detection_count": max(peak_count, current_count),
            "incident_peak_frp_total_mw": _max_optional(peak_frp, current_frp),
            "incident_peak_extent_km": max(peak_extent, current_extent),
            "latest_detection_count": current_count,
            "latest_frp_total_mw": current_frp,
            "latest_extent_km": current_extent,
        })

        observed.event_id = previous.event_id
        observed.source_event_id = previous.source_event_id
        observed.started_at = previous.started_at or observed.started_at
        observed.metadata = merged_metadata

        if reasons:
            observed.title = "Aumento del foco de incendio satelital"
            observed.description = _phase_description(
                "Aumento", current_count, current_frp, current_extent
            )
            _refresh_event_hashes(observed)
            return observed

        _preserve_notification_content(previous, observed)
        return observed


def _phase_description(
    phase: str,
    count: int,
    frp_total: float | None,
    extent_km: float,
) -> str:
    """Genera el detalle legible del inicio/aumento sin afirmar confirmación."""
    parts = [f"{phase} de posible foco de incendio detectado por NASA FIRMS"]
    if count > 0:
        parts.append(
            f"{count} detección{'es' if count != 1 else ''} "
            f"térmica{'s' if count != 1 else ''}"
        )
    if frp_total is not None:
        parts.append(f"FRP total {frp_total:g} MW")
    if extent_km > 0:
        parts.append(f"extensión observada {extent_km:.1f} km")
    return "; ".join(parts)


def _preserve_notification_content(previous: Event, observed: Event) -> None:
    """Conserva el contenido deduplicable cuando el foco sigue estable.

    ``updated_at`` y los metadatos de seguimiento permanecen con los valores de
    la pasada más reciente, pero todos los campos usados por ``content_hash`` y
    el ``raw_hash`` quedan iguales al último estado significativo.
    """
    observed.status = previous.status
    observed.verification = previous.verification
    observed.severity = previous.severity
    observed.title = previous.title
    observed.description = previous.description
    observed.road = previous.road
    observed.kilometre = previous.kilometre
    observed.municipality = previous.municipality
    observed.province = previous.province
    observed.autonomous_region = previous.autonomous_region
    observed.latitude = previous.latitude
    observed.longitude = previous.longitude
    observed.expected_end = previous.expected_end
    observed.fingerprint = previous.fingerprint
    observed.raw_hash = previous.raw_hash


def _best_incident_match(
    observed: Event,
    candidates: Iterable[Event],
    *,
    radius_km: float,
    max_gap_seconds: float,
) -> Event | None:
    """Selecciona el foco compatible más cercano en espacio y tiempo."""
    observed_ts = _event_timestamp(observed)
    best_event: Event | None = None
    best_key: tuple[float, float] | None = None

    for candidate in candidates:
        if not _is_active_firms_event(candidate, observed.source):
            continue
        candidate_ts = _incident_last_timestamp(candidate)
        gap = observed_ts - candidate_ts
        if observed_ts and candidate_ts and (gap < 0 or gap > max_gap_seconds):
            continue

        coordinates = _incident_last_coordinates(candidate)
        if coordinates is None or observed.latitude is None or observed.longitude is None:
            continue
        distance = _distance_km(
            float(observed.latitude),
            float(observed.longitude),
            coordinates[0],
            coordinates[1],
        )
        if distance > radius_km:
            continue

        key = (distance, abs(gap))
        if best_key is None or key < best_key:
            best_key = key
            best_event = candidate

    return best_event


def _is_active_firms_event(event: Event, source_id: str) -> bool:
    """Indica si un evento puede actuar como foco FIRMS persistente."""
    return (
        event.source == source_id
        and event.category == "wildfire"
        and str(event.status or "active").strip().casefold() not in _TERMINAL_STATUSES
        and event.latitude is not None
        and event.longitude is not None
    )


def _incident_last_coordinates(event: Event) -> tuple[float, float] | None:
    """Obtiene las últimas coordenadas observadas del foco."""
    lat = _to_float(event.metadata.get("incident_last_latitude"))
    lon = _to_float(event.metadata.get("incident_last_longitude"))
    lat = lat if lat is not None else _to_float(event.latitude)
    lon = lon if lon is not None else _to_float(event.longitude)
    if lat is None or lon is None:
        return None
    return lat, lon


def _incident_last_timestamp(event: Event) -> float:
    """Fecha de última detección real del foco expresada en segundos UTC."""
    value = clean_text(event.metadata.get("incident_last_detection_at"))
    return _timestamp(value or event.updated_at or event.started_at)


def _event_timestamp(event: Event) -> float:
    """Fecha de observación del cluster expresada en segundos UTC."""
    return _timestamp(event.updated_at or event.started_at)


def _timestamp(value: Any) -> float:
    """Convierte ISO-8601 a segundos UTC; devuelve 0 para valores inválidos."""
    text = clean_text(value)
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _cluster_extent_km(members: list[Event]) -> float:
    """Calcula el diámetro máximo Haversine entre detecciones del cluster."""
    maximum = 0.0
    for index, left in enumerate(members[:-1]):
        if left.latitude is None or left.longitude is None:
            continue
        for right in members[index + 1:]:
            if right.latitude is None or right.longitude is None:
                continue
            maximum = max(
                maximum,
                _distance_km(
                    float(left.latitude), float(left.longitude),
                    float(right.latitude), float(right.longitude),
                ),
            )
    return maximum


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia Haversine entre dos coordenadas WGS84."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    value = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 6371.0088 * 2 * math.atan2(
        math.sqrt(value), math.sqrt(max(0.0, 1.0 - value))
    )


def _significant_growth(
    current: float | None,
    previous: float | None,
    ratio: float,
    minimum_delta: float,
) -> bool:
    """Exige crecimiento absoluto y relativo para filtrar oscilaciones menores."""
    if current is None or previous is None or current <= previous:
        return False
    delta = current - previous
    if delta < minimum_delta:
        return False
    if previous <= 0:
        return current >= minimum_delta
    return current >= previous * (1.0 + ratio)


def _event_frp_total(event: Event) -> float | None:
    """Obtiene FRP total y usa FRP individual como respaldo."""
    return _first_float(event.metadata.get("frp_total_mw"), event.metadata.get("frp_mw"))


def _refresh_event_hashes(event: Event) -> None:
    """Recalcula identidad y contenido después de modificar el evento."""
    event.fingerprint = event.identity_fingerprint()
    event.raw_hash = event.content_hash()


def _metadata_int(event: Event, key: str) -> int:
    """Obtiene un metadato entero no negativo."""
    try:
        return max(0, int(event.metadata.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _metadata_float(event: Event, key: str) -> float | None:
    """Obtiene un metadato numérico finito."""
    return _to_float(event.metadata.get(key))


def _to_float(value: Any) -> float | None:
    """Convierte a float finito sin propagar valores defectuosos."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_float(*values: Any) -> float | None:
    """Devuelve el primer valor numérico finito disponible."""
    for value in values:
        number = _to_float(value)
        if number is not None:
            return number
    return None


def _max_optional(left: float | None, right: float | None) -> float | None:
    """Máximo de dos números opcionales conservando ``None`` si ambos faltan."""
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _bounded_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    """Convierte un ajuste y lo limita a un rango operativo seguro."""
    number = _to_float(value)
    if number is None:
        return default
    return max(minimum, min(maximum, number))


def _config_bool(value: Any, default: bool) -> bool:
    """Interpreta booleanos procedentes de JSON o texto."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on", "si", "sí"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default
