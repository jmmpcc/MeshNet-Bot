from __future__ import annotations

import csv
import hashlib
import io
import math
import os
from datetime import datetime
from typing import Any

from ..config import atomic_write_json
from ..models import Event, clean_text
from ..storage import cache_paths
from .base import HttpSource, SourceError


_CONFIDENCE_LABELS = {
    "l": "low",
    "n": "nominal",
    "h": "high",
}

_CONFIDENCE_RANK = {
    "l": 10,
    "n": 20,
    "h": 30,
}

_SATELLITE_NAMES = {
    "N": "Suomi-NPP",
    "NPP": "Suomi-NPP",
    "SNPP": "Suomi-NPP",
    "N20": "NOAA-20",
    "N21": "NOAA-21",
    "A": "Aqua",
    "T": "Terra",
}


class FirmsSource(HttpSource):
    """Conector NASA FIRMS para detecciones térmicas, no incendios confirmados.

    Cómo se utiliza:
        ``source = FirmsSource("nasa_firms", source_config, app_config)``
        ``events, not_modified = source.fetch()``

    Funcionalidad:
        - Consulta la API AREA de NASA FIRMS reutilizando ``HttpSource``.
        - Oculta la MAP_KEY en la metainformación persistida de caché.
        - Normaliza confianza VIIRS ``l/n/h`` y nombre del satélite.
        - Convierte cada detección CSV en un ``Event`` interno.
        - Agrupa píxeles cercanos espacial y temporalmente antes de devolverlos
          al motor de emergencias, evitando tratar cada píxel VIIRS como un
          incendio independiente.
        - Conserva ``verification=satellite_detection`` para que el resto del
          sistema siga distinguiendo una detección térmica de un aviso oficial.
    """

    def fetch_bytes(self):
        """Descarga el CSV FIRMS usando la MAP_KEY configurada en el entorno."""
        key_env = clean_text(self.config.get("api_key_env")) or "FIRMS_MAP_KEY"
        map_key = clean_text(os.getenv(key_env))
        if not map_key:
            raise SourceError(f"falta la variable de entorno {key_env}")
        if map_key.casefold() in {"su_map_key", "your_map_key", "map_key", "demo"}:
            raise SourceError(
                f"{key_env} contiene el texto de ejemplo; sustitúyalo por la MAP_KEY real de NASA FIRMS"
            )
        template = clean_text(self.config.get("url_template"))
        if not template:
            raise SourceError("url_template no configurada")
        bbox = self.config.get("bbox", [-9.4, 35.8, 4.4, 43.9])
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise SourceError("bbox debe contener oeste,sur,este,norte")
        values = {
            "map_key": map_key,
            "source": self.config.get("dataset", "VIIRS_SNPP_NRT"),
            "bbox": ",".join(str(value) for value in bbox),
            "days": max(1, min(10, int(self.config.get("days", 1)))),
        }
        original = self.config.get("url")
        self.config["url"] = template.format(**values)
        try:
            result = super().fetch_bytes()
            # HttpSource conserva la URL para validar la caché. En FIRMS esa URL
            # incluye la credencial, por lo que se sustituye antes de persistirla.
            result.metadata["url"] = template.format(**(values | {"map_key": "***"}))
            _, metadata_path = cache_paths(self.source_id)
            atomic_write_json(metadata_path, result.metadata)
            return result
        finally:
            if original is None:
                self.config.pop("url", None)
            else:
                self.config["url"] = original

    def parse(self, body: bytes) -> list[Event]:
        """Parsea el CSV y devuelve detecciones FIRMS agrupadas.

        Parámetros:
            body: contenido CSV recibido de la API FIRMS.

        Retorna:
            Lista de eventos. Con ``cluster_enabled=true`` —valor por defecto—
            cada evento representa un grupo espacial/temporal de píxeles de una
            misma zona, no una fila CSV individual.

        Compatibilidad:
            ``cluster_enabled=false`` conserva el comportamiento histórico de
            una fila CSV = un ``Event`` y permite diagnóstico directo.
        """
        try:
            rows = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
            detections = [self._event(row) for row in rows]
        except (UnicodeDecodeError, csv.Error) as exc:
            raise SourceError(f"CSV FIRMS no válido: {exc}") from exc

        events = [event for event in detections if event is not None]
        if not events:
            return []

        if not _config_bool(self.config.get("cluster_enabled", True), default=True):
            return events

        return self._cluster_events(events)

    def _event(self, row: dict[str, Any]) -> Event | None:
        """Normaliza una fila FIRMS individual antes de la agrupación.

        La función mantiene toda la información necesaria para poder calcular
        posteriormente el centro del grupo, FRP máximo/total, confianza máxima
        y satélites participantes.
        """
        lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
        if lat is None or lon is None:
            return None

        date_text = clean_text(row.get("acq_date"))
        time_text = clean_text(row.get("acq_time")).zfill(4)
        acquired = f"{date_text}T{time_text[:2]}:{time_text[2:4]}:00Z"

        satellite_code = clean_text(row.get("satellite")).upper()
        satellite_name = _satellite_name(satellite_code)
        basis = f"{lat:.5f}|{lon:.5f}|{acquired}|{satellite_code}"
        source_id = hashlib.sha256(basis.encode()).hexdigest()[:20]

        frp = _float(row.get("frp"))
        confidence_code = _confidence_code(row.get("confidence"))
        confidence_label = _CONFIDENCE_LABELS.get(confidence_code, "unknown")

        # Compatibilidad con la lógica histórica: toda detección válida sigue
        # siendo al menos ``medium``; únicamente se corrige que VIIRS ``h``
        # también sea reconocida como confianza alta.
        severity = (
            "high"
            if (frp is not None and frp >= 100.0) or confidence_code == "h"
            else "medium"
        )

        description = f"Foco térmico FIRMS; confianza {confidence_label}"
        if frp is not None:
            description += f"; FRP {frp:g} MW"

        return Event(
            event_id=f"{self.source_id}:{source_id}",
            source=self.source_id,
            source_event_id=source_id,
            category="wildfire",
            verification="satellite_detection",
            severity=severity,
            title="Detección térmica por satélite",
            description=description,
            latitude=lat,
            longitude=lon,
            started_at=acquired,
            updated_at=acquired,
            source_url="https://firms.modaps.eosdis.nasa.gov/map/",
            metadata={
                "frp_mw": frp,
                "confidence": confidence_code,
                "confidence_label": confidence_label,
                "satellite": satellite_name,
                "satellite_code": satellite_code,
                "instrument": clean_text(row.get("instrument")),
                "daynight": clean_text(row.get("daynight")).upper(),
                "detection_count": 1,
            },
        )

    def _cluster_events(self, events: list[Event]) -> list[Event]:
        """Agrupa detecciones térmicas próximas en espacio y tiempo.

        Parámetros de configuración de la fuente:
            cluster_radius_km:
                Distancia máxima entre píxeles conectados. Por defecto 5 km.
            cluster_time_minutes:
                Diferencia temporal máxima entre píxeles conectados. Por defecto
                90 minutos, suficiente para una pasada/órbitas próximas sin unir
                indiscriminadamente observaciones de distintos momentos del día.

        Algoritmo:
            Se calculan componentes conexas: dos detecciones pertenecen al mismo
            grupo cuando están dentro de ambos umbrales; la relación se propaga
            entre vecinos para representar frentes térmicos continuos.
        """
        radius_km = _bounded_float(self.config.get("cluster_radius_km", 5.0), 0.25, 50.0, 5.0)
        time_minutes = _bounded_float(
            self.config.get("cluster_time_minutes", 90.0), 1.0, 720.0, 90.0
        )
        time_seconds = time_minutes * 60.0

        timestamps = [_event_timestamp(event) for event in events]
        assigned = [False] * len(events)
        clusters: list[list[Event]] = []

        for start in range(len(events)):
            if assigned[start]:
                continue
            assigned[start] = True
            queue = [start]
            member_indexes: list[int] = []

            while queue:
                current = queue.pop()
                member_indexes.append(current)
                current_event = events[current]
                current_ts = timestamps[current]

                for candidate in range(len(events)):
                    if assigned[candidate]:
                        continue
                    candidate_event = events[candidate]
                    candidate_ts = timestamps[candidate]
                    if abs(candidate_ts - current_ts) > time_seconds:
                        continue
                    if _distance_km(current_event, candidate_event) > radius_km:
                        continue
                    assigned[candidate] = True
                    queue.append(candidate)

            clusters.append([events[index] for index in member_indexes])

        grouped = [
            self._aggregate_cluster(cluster, radius_km, time_minutes)
            for cluster in clusters
        ]
        grouped.sort(key=lambda event: (event.started_at or "", event.event_id))
        return grouped

    def _aggregate_cluster(
        self,
        members: list[Event],
        radius_km: float,
        time_minutes: float,
    ) -> Event:
        """Convierte un conjunto de píxeles FIRMS en un único evento térmico."""
        ordered = sorted(
            members,
            key=lambda event: (
                event.started_at or "",
                float(event.latitude or 0.0),
                float(event.longitude or 0.0),
                event.source_event_id,
            ),
        )

        weights: list[float] = []
        for event in ordered:
            frp = _float(event.metadata.get("frp_mw"))
            weights.append(max(0.1, frp if frp is not None and frp > 0 else 1.0))
        weight_total = sum(weights) or float(len(ordered))
        latitude = sum(float(event.latitude) * weight for event, weight in zip(ordered, weights)) / weight_total
        longitude = sum(float(event.longitude) * weight for event, weight in zip(ordered, weights)) / weight_total

        frp_values = [
            value
            for event in ordered
            if (value := _float(event.metadata.get("frp_mw"))) is not None
        ]
        frp_max = max(frp_values) if frp_values else None
        frp_total = sum(frp_values) if frp_values else None

        confidence_codes = [
            _confidence_code(event.metadata.get("confidence"))
            for event in ordered
        ]
        confidence_code = max(
            confidence_codes,
            key=lambda value: _CONFIDENCE_RANK.get(value, 0),
            default="",
        )
        confidence_label = _CONFIDENCE_LABELS.get(confidence_code, "unknown")

        satellites = sorted({
            clean_text(event.metadata.get("satellite"))
            for event in ordered
            if clean_text(event.metadata.get("satellite"))
        })
        satellite_codes = sorted({
            clean_text(event.metadata.get("satellite_code"))
            for event in ordered
            if clean_text(event.metadata.get("satellite_code"))
        })

        severity = "high" if any(event.severity == "high" for event in ordered) else "medium"
        started_at = min((event.started_at for event in ordered if event.started_at), default="")
        updated_at = max((event.updated_at for event in ordered if event.updated_at), default=started_at)

        # El identificador se basa en la primera detección ordenada de la pasada.
        # FIRMS devuelve nuevamente esa detección en consultas sucesivas del mismo
        # periodo, por lo que el ID permanece estable mientras el grupo no cambie
        # de observación base.
        anchor = ordered[0].source_event_id
        cluster_basis = f"{anchor}|{radius_km:.2f}|{time_minutes:.1f}"
        cluster_id = hashlib.sha256(cluster_basis.encode()).hexdigest()[:20]

        detection_count = len(ordered)
        description = f"FIRMS: {detection_count} detección"
        if detection_count != 1:
            description += "es"
        description += f" térmica{'s' if detection_count != 1 else ''} agrupada{'s' if detection_count != 1 else ''}"
        description += f"; confianza {confidence_label}"
        if frp_max is not None:
            description += f"; FRP máx {frp_max:g} MW"
        if frp_total is not None and detection_count > 1:
            description += f"; FRP total {frp_total:g} MW"

        return Event(
            event_id=f"{self.source_id}:{cluster_id}",
            source=self.source_id,
            source_event_id=cluster_id,
            category="wildfire",
            verification="satellite_detection",
            severity=severity,
            title="Detección térmica satelital agrupada",
            description=description,
            latitude=latitude,
            longitude=longitude,
            started_at=started_at,
            updated_at=updated_at,
            source_url="https://firms.modaps.eosdis.nasa.gov/map/",
            metadata={
                "detection_count": detection_count,
                "frp_mw": frp_max,
                "frp_max_mw": frp_max,
                "frp_total_mw": frp_total,
                "confidence": confidence_code,
                "confidence_label": confidence_label,
                "satellite": ", ".join(satellites),
                "satellites": satellites,
                "satellite_codes": satellite_codes,
                "cluster_radius_km": radius_km,
                "cluster_time_minutes": time_minutes,
                "clustered": detection_count > 1,
            },
        )


def _confidence_code(value: Any) -> str:
    """Normaliza confianza FIRMS VIIRS a ``l``, ``n`` o ``h``."""
    text = clean_text(value).casefold()
    aliases = {
        "l": "l",
        "low": "l",
        "n": "n",
        "nominal": "n",
        "normal": "n",
        "h": "h",
        "high": "h",
    }
    return aliases.get(text, text[:1] if text[:1] in _CONFIDENCE_LABELS else "")


def _satellite_name(value: Any) -> str:
    """Convierte el código FIRMS en un nombre comprensible sin inventar datos."""
    code = clean_text(value).upper()
    return _SATELLITE_NAMES.get(code, code)


def _event_timestamp(event: Event) -> float:
    """Convierte la fecha ISO del evento a segundos para agrupación temporal."""
    try:
        return datetime.fromisoformat(str(event.started_at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _distance_km(left: Event, right: Event) -> float:
    """Distancia Haversine entre dos eventos FIRMS con coordenadas válidas."""
    lat1 = math.radians(float(left.latitude))
    lon1 = math.radians(float(left.longitude))
    lat2 = math.radians(float(right.latitude))
    lon2 = math.radians(float(right.longitude))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _bounded_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    """Convierte un ajuste numérico y lo limita a un rango operativo seguro."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _config_bool(value: Any, *, default: bool) -> bool:
    """Interpreta booleanos procedentes de JSON/configuración textual."""
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


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
