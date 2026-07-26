from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from typing import Iterable

from ..models import Event, clean_text, fold_text
from .base import HttpSource, SourceError


CATEGORY_PATTERNS = (
    ("traffic_collision", ("accident", "collision", "accidente", "colision")),
    ("road_closed", ("roadclosed", "road closed", "carretera cortada", "closure")),
    ("lane_closed", ("laneclosed", "lane closed", "carril cerrado")),
    ("traffic_obstruction", ("obstruction", "obstacle", "vehicleobstruction")),
    ("wildfire", ("forestfire", "wildfire", "incendio forestal")),
    ("snow", ("snow", "nieve")), ("flood", ("flood", "inund")),
)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def descendants(element: ET.Element, names: Iterable[str]) -> list[ET.Element]:
    wanted = {name.casefold() for name in names}
    return [node for node in element.iter() if local_name(node.tag).casefold() in wanted]


def first_text(element: ET.Element, *names: str) -> str:
    for node in descendants(element, names):
        if node.text and node.text.strip():
            return clean_text(node.text)
    return ""


def first_float(element: ET.Element, *names: str) -> float | None:
    value = first_text(element, *names)
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


class Datex2Source(HttpSource):
    def parse(self, body: bytes) -> list[Event]:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise SourceError("XML con DTD o entidades no admitido")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise SourceError(f"XML DATEX II no válido: {exc}") from exc
        containers = descendants(root, ("situation",)) or [root]
        records: list[Event] = []
        for container in containers:
            for index, record in enumerate(descendants(container, ("situationRecord",))):
                records.append(self._event(record, index))
        return records

    def _event(self, record: ET.Element, index: int) -> Event:
        source_id = clean_text(record.attrib.get("id") or record.attrib.get("versionedReference"))
        title = first_text(record, "value", "comment", "situationRecordCreationReference")
        record_type = local_name(next(iter(record), record).tag)
        combined = fold_text(f"{record_type} {title} {ET.tostring(record, encoding='unicode')[:4000]}")
        category = "other"
        for candidate, needles in CATEGORY_PATTERNS:
            if any(needle in combined for needle in needles):
                category = candidate
                break
        road = first_text(record, "roadNumber", "roadName")
        kilometre = first_float(record, "kilometrePoint", "kilometerPoint")
        started = first_text(record, "overallStartTime", "situationRecordCreationTime")
        if not source_id:
            basis = f"{record_type}|{road}|{kilometre}|{started}|{index}"
            source_id = hashlib.sha256(basis.encode()).hexdigest()[:20]
        severity_raw = fold_text(first_text(record, "severity", "impactOnTraffic"))
        severity = "high" if any(word in severity_raw for word in ("high", "highest", "severe")) else "medium"
        return Event(
            event_id=f"{self.source_id}:{source_id}", source=self.source_id,
            source_event_id=source_id, category=category, verification="official",
            severity=severity, title=title or _title_for(category),
            description=first_text(record, "value", "comment"), road=road,
            kilometre=kilometre, municipality=first_text(record, "town", "municipality"),
            province=first_text(record, "administrativeArea") or self.config.get("default_province", ""),
            latitude=first_float(record, "latitude"), longitude=first_float(record, "longitude"),
            started_at=started, updated_at=first_text(record, "situationRecordVersionTime"),
            expected_end=first_text(record, "overallEndTime"),
            source_url=self.config.get("url", ""),
            metadata={"datex_record_type": record_type},
        )


def _title_for(category: str) -> str:
    return {
        "traffic_collision": "Colisión de tráfico", "road_closed": "Carretera cortada",
        "lane_closed": "Carril cerrado", "traffic_obstruction": "Obstáculo en la vía",
        "wildfire": "Incendio próximo a la vía", "snow": "Nieve en la vía",
        "flood": "Inundación en la vía",
    }.get(category, "Incidencia de tráfico")
