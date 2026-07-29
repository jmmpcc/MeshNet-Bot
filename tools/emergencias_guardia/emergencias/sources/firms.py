from __future__ import annotations

import csv
import hashlib
import io
import os
from typing import Any

from ..config import atomic_write_json
from ..models import Event, clean_text
from ..storage import cache_paths
from .base import HttpSource, SourceError


class FirmsSource(HttpSource):
    """Conector de detecciones térmicas NASA FIRMS (no incendios confirmados)."""

    def fetch_bytes(self):
        key_env = clean_text(self.config.get("api_key_env")) or "FIRMS_MAP_KEY"
        map_key = clean_text(os.getenv(key_env))
        if not map_key:
            raise SourceError(f"falta la variable de entorno {key_env}")
        template = clean_text(self.config.get("url_template"))
        if not template:
            raise SourceError("url_template no configurada")
        bbox = self.config.get("bbox", [-9.4, 35.8, 4.4, 43.9])
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise SourceError("bbox debe contener oeste,sur,este,norte")
        values = {
            "map_key": map_key, "source": self.config.get("dataset", "VIIRS_SNPP_NRT"),
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
        try:
            rows = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
            events = [self._event(row) for row in rows]
        except (UnicodeDecodeError, csv.Error) as exc:
            raise SourceError(f"CSV FIRMS no válido: {exc}") from exc
        return [event for event in events if event is not None]

    def _event(self, row: dict[str, Any]) -> Event | None:
        lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
        if lat is None or lon is None:
            return None
        acquired = f"{clean_text(row.get('acq_date'))}T{clean_text(row.get('acq_time')).zfill(4)[:2]}:{clean_text(row.get('acq_time')).zfill(4)[2:]}:00Z"
        basis = f"{lat:.4f}|{lon:.4f}|{acquired}|{row.get('satellite', '')}"
        source_id = hashlib.sha256(basis.encode()).hexdigest()[:20]
        frp = _float(row.get("frp"))
        confidence = clean_text(row.get("confidence"))
        severity = "high" if (frp is not None and frp >= 100) or confidence.casefold() == "high" else "medium"
        return Event(
            event_id=f"{self.source_id}:{source_id}", source=self.source_id,
            source_event_id=source_id, category="wildfire", verification="satellite_detection",
            severity=severity, title="Detección térmica por satélite",
            description=f"Foco térmico FIRMS; confianza {confidence or 'sin indicar'}; FRP {frp:g} MW" if frp is not None else f"Foco térmico FIRMS; confianza {confidence or 'sin indicar'}",
            latitude=lat, longitude=lon, started_at=acquired,
            updated_at=acquired, source_url="https://firms.modaps.eosdis.nasa.gov/map/",
            metadata={"frp_mw": frp, "confidence": confidence, "satellite": clean_text(row.get("satellite"))},
        )


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
