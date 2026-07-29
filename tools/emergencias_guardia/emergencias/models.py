from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


VALID_CATEGORIES = {
    "wildfire", "urban_fire", "industrial_fire", "traffic_collision",
    "road_closed", "lane_closed", "traffic_obstruction", "flood", "storm",
    "snow", "strong_wind", "extreme_temperature", "chemical",
    "power_outage", "water_outage", "gas_outage", "public_safety",
    "civil_protection", "earthquake", "tsunami", "volcanic", "landslide", "other",
}
VALID_SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_RANK = {name: index for index, name in enumerate(VALID_SEVERITIES)}
VALID_VERIFICATIONS = {
    "official", "confirmed_multi_source", "satellite_detection", "unverified",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def fold_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


@dataclass(slots=True)
class Event:
    event_id: str
    source: str
    source_event_id: str
    category: str
    status: str = "active"
    verification: str = "official"
    severity: str = "medium"
    title: str = ""
    description: str = ""
    road: str = ""
    kilometre: float | None = None
    municipality: str = ""
    province: str = ""
    autonomous_region: str = ""
    latitude: float | None = None
    longitude: float | None = None
    started_at: str = ""
    updated_at: str = ""
    expected_end: str = ""
    source_url: str = ""
    first_seen: str = ""
    last_seen: str = ""
    fingerprint: str = ""
    raw_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in VALID_CATEGORIES:
            self.category = "other"
        if self.severity not in VALID_SEVERITIES:
            self.severity = "medium"
        if self.verification not in VALID_VERIFICATIONS:
            self.verification = "unverified"
        self.title = clean_text(self.title) or "Incidencia"
        self.description = clean_text(self.description)
        self.road = clean_text(self.road).upper()
        self.municipality = clean_text(self.municipality)
        self.province = clean_text(self.province)
        self.autonomous_region = clean_text(self.autonomous_region)
        if not self.event_id:
            self.event_id = f"{self.source}:{self.source_event_id}"
        if not self.fingerprint:
            self.fingerprint = self.identity_fingerprint()
        if not self.raw_hash:
            self.raw_hash = self.content_hash()

    def identity_fingerprint(self) -> str:
        identity = {
            "source": fold_text(self.source),
            "source_event_id": clean_text(self.source_event_id),
            "category": self.category,
            "road": fold_text(self.road),
            "kilometre": round(self.kilometre, 1) if self.kilometre is not None else None,
            "municipality": fold_text(self.municipality),
            "latitude": round(self.latitude, 3) if self.latitude is not None else None,
            "longitude": round(self.longitude, 3) if self.longitude is not None else None,
            "started_at": self.started_at[:16],
        }
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def content_hash(self) -> str:
        content = {
            "category": self.category, "status": self.status,
            "verification": self.verification, "severity": self.severity,
            "title": clean_text(self.title), "description": clean_text(self.description),
            "road": self.road, "kilometre": self.kilometre,
            "municipality": self.municipality, "province": self.province,
            "latitude": self.latitude, "longitude": self.longitude,
            "expected_end": self.expected_end,
        }
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})
