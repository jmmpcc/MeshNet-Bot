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


def _plain_html(value: str) -> str:
    return clean_text(html.unescape(re.sub(r"<[^>]+>", " ", value or "")))


def _iso(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, OverflowError):
        return value


def _is_hydrological_warning(text: str) -> bool:
    folded = fold_text(text)
    warning_terms = (
        "crecida", "crecidas", "avenida", "inundacion", "desbordamiento",
        "barranco", "cauce", "hidrolog", "saih", "vigilancia",
    )
    return any(term in folded for term in warning_terms)


def _severity(text: str) -> str:
    folded = fold_text(text)
    if any(term in folded for term in ("rojo", "extraordin", "desbordamiento", "evacu")):
        return "critical"
    if any(term in folded for term in ("naranja", "importante", "crecida importante", "riesgo alto")):
        return "high"
    return "medium"


def _province(text: str) -> str:
    folded = fold_text(text)
    for province in (
        "Zaragoza", "Huesca", "Teruel", "Navarra", "La Rioja", "Burgos", "Cantabria",
        "Lleida", "Tarragona", "Castellón", "Soria", "Álava",
    ):
        if fold_text(province) in folded:
            return province
    return ""


class CheRssSource(HttpSource):
    """Filtra el RSS oficial de CHE y conserva solo comunicaciones hidrológicas.

    La CHE publica en su canal RSS notas de prensa de naturaleza diversa. Este
    conector no convierte cada nota en una emergencia: exige vocabulario
    hidrológico explícito y normaliza únicamente avisos de crecidas, cauces,
    barrancos o inundación. El texto original queda disponible como fuente.
    """

    def parse(self, body: bytes) -> list[Event]:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise SourceError("RSS CHE con DTD o entidades no admitido")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise SourceError(f"RSS CHE no válido: {exc}") from exc
        entries = [node for node in root.iter() if _local(node.tag) in {"item", "entry"}]
        events: list[Event] = []
        for index, entry in enumerate(entries):
            event = self._event(entry, index)
            if event is not None:
                events.append(event)
        return events

    def _event(self, entry: ET.Element, index: int) -> Event | None:
        title = _plain_html(_text(entry, "title"))
        description = _plain_html(_text(entry, "description", "summary", "content"))
        combined = f"{title} {description}"
        if not _is_hydrological_warning(combined):
            return None
        link = _link(entry)
        published = _iso(_text(entry, "pubDate", "published", "updated", "date"))
        source_id = _text(entry, "guid", "id") or link
        if not source_id:
            source_id = hashlib.sha256(f"{title}|{published}|{index}".encode()).hexdigest()[:20]
        return Event(
            event_id=f"{self.source_id}:{source_id}",
            source=self.source_id,
            source_event_id=source_id,
            category="flood",
            verification=self.config.get("verification", "official"),
            severity=_severity(combined),
            title=title or "Aviso hidrológico CHE / SAIH Ebro",
            description=description,
            province=_province(combined),
            started_at=published,
            updated_at=published,
            source_url=link or self.config.get("url", ""),
            metadata={"feed_profile": "che_saih", "hydrological_warning": True},
        )
