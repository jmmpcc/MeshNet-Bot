from __future__ import annotations

import hashlib
import json
from typing import Any

from ..models import Event, clean_text
from .base import HttpSource, SourceError


DEFAULT_MAPPING = {
    "id": "id", "title": "title", "description": "description",
    "category": "category", "status": "status", "severity": "severity",
    "road": "road", "kilometre": "kilometre", "municipality": "municipality",
    "province": "province", "latitude": "latitude", "longitude": "longitude",
    "started_at": "started_at", "updated_at": "updated_at",
    "expected_end": "expected_end", "source_url": "source_url",
}


def nested_get(item: Any, path: str, default: Any = "") -> Any:
    current = item
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part, default)
        else:
            return default
    return current


class JsonSource(HttpSource):
    def parse(self, body: bytes) -> list[Event]:
        payload = self.decode_json(body)
        path = str(self.config.get("records_path", "")).strip()
        records = nested_get(payload, path, []) if path else payload
        if isinstance(records, dict):
            for candidate in ("items", "features", "results", "records"):
                if isinstance(records.get(candidate), list):
                    records = records[candidate]
                    break
        if not isinstance(records, list):
            raise SourceError("la ruta de registros JSON no contiene una lista")
        mapping = DEFAULT_MAPPING | self.config.get("mapping", {})
        events: list[Event] = []
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                continue
            properties = item.get("properties", item)
            geometry = item.get("geometry") if isinstance(item.get("geometry"), dict) else {}
            coordinates = geometry.get("coordinates", []) if geometry.get("type") == "Point" else []
            raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
            source_event_id = clean_text(nested_get(properties, mapping["id"]))
            if not source_event_id:
                source_event_id = hashlib.sha256(raw.encode()).hexdigest()[:20]
            latitude = nested_get(properties, mapping["latitude"], None)
            longitude = nested_get(properties, mapping["longitude"], None)
            if len(coordinates) >= 2:
                longitude, latitude = coordinates[0], coordinates[1]
            events.append(Event(
                event_id=f"{self.source_id}:{source_event_id}",
                source=self.source_id, source_event_id=source_event_id,
                category=clean_text(nested_get(properties, mapping["category"])) or "other",
                status=clean_text(nested_get(properties, mapping["status"])) or "active",
                verification=self.config.get("verification", "official"),
                severity=clean_text(nested_get(properties, mapping["severity"])) or "medium",
                title=clean_text(nested_get(properties, mapping["title"])) or "Incidencia municipal",
                description=nested_get(properties, mapping["description"]),
                road=nested_get(properties, mapping["road"]),
                kilometre=_number(nested_get(properties, mapping["kilometre"], None)),
                municipality=nested_get(properties, mapping["municipality"]),
                province=nested_get(properties, mapping["province"]) or self.config.get("default_province", ""),
                latitude=_number(latitude), longitude=_number(longitude),
                started_at=clean_text(nested_get(properties, mapping["started_at"])),
                updated_at=clean_text(nested_get(properties, mapping["updated_at"])),
                expected_end=clean_text(nested_get(properties, mapping["expected_end"])),
                source_url=clean_text(nested_get(properties, mapping["source_url"])) or self.config.get("url", ""),
                metadata={"raw_index": index},
            ))
        return events


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
