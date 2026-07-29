from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from ..models import Event, clean_text, fold_text
from .base import HttpSource, SourceError


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _text(element: ET.Element, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for child in element.iter():
        if _local(child.tag) in wanted and child.text and child.text.strip():
            return clean_text(child.text)
    return ""


def _link(element: ET.Element) -> str:
    for child in element.iter():
        if _local(child.tag) == "link":
            return clean_text(child.attrib.get("href") or child.text)
    return ""


def _number(value: str) -> float | None:
    try:
        return float(value.replace(",", "."))
    except (TypeError, ValueError):
        return None


def _iso_date(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, OverflowError):
        return value


def _plain_html(value: str) -> str:
    return clean_text(html.unescape(re.sub(r"<[^>]+>", " ", value or "")))


class RssSource(HttpSource):
    """Normaliza RSS/Atom/GeoRSS; el perfil ``ign_earthquakes`` añade magnitud."""

    def parse(self, body: bytes) -> list[Event]:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise SourceError("XML con DTD o entidades no admitido")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise SourceError(f"RSS/Atom no válido: {exc}") from exc
        entries = [node for node in root.iter() if _local(node.tag) in {"item", "entry"}]
        events = [self._event(entry, index) for index, entry in enumerate(entries)]
        return [event for event in events if event is not None]

    def _event(self, entry: ET.Element, index: int) -> Event | None:
        title = _plain_html(_text(entry, "title"))
        description = _plain_html(_text(entry, "description", "summary", "content"))
        link = _link(entry)
        source_id = _text(entry, "guid", "id") or link
        published = _iso_date(_text(entry, "pubDate", "published", "updated", "date"))
        lat = _number(_text(entry, "lat"))
        lon = _number(_text(entry, "long", "lon"))
        point = _text(entry, "point")
        if point and (lat is None or lon is None):
            parts = point.replace(",", " ").split()
            if len(parts) >= 2:
                lat, lon = _number(parts[0]), _number(parts[1])
        if not source_id:
            source_id = hashlib.sha256(f"{title}|{published}|{index}".encode()).hexdigest()[:20]

        category = clean_text(self.config.get("category", "other"))
        severity = clean_text(self.config.get("severity", "medium"))
        metadata: dict[str, object] = {"feed_profile": self.config.get("profile", "generic")}
        if self.config.get("profile") == "ign_earthquakes":
            category = "earthquake"
            magnitude = self._magnitude(f"{title} {description}")
            if magnitude is not None:
                metadata["magnitude"] = magnitude
                severity = self._earthquake_severity(magnitude)
            title = title or "Terremoto registrado"
        return Event(
            event_id=f"{self.source_id}:{source_id}", source=self.source_id,
            source_event_id=source_id, category=category,
            verification=self.config.get("verification", "official"), severity=severity,
            title=title or "Aviso oficial", description=description,
            province=self.config.get("default_province", ""), latitude=lat, longitude=lon,
            started_at=published, updated_at=published, source_url=link or self.config.get("url", ""),
            metadata=metadata,
        )

    @staticmethod
    def _magnitude(text: str) -> float | None:
        match = re.search(r"(?:magnitud|magnitude|\bmbLg\b|\bM)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", text, re.I)
        return _number(match.group(1)) if match else None

    @staticmethod
    def _earthquake_severity(magnitude: float) -> str:
        if magnitude >= 6:
            return "critical"
        if magnitude >= 5:
            return "high"
        if magnitude >= 3.5:
            return "medium"
        return "low"
