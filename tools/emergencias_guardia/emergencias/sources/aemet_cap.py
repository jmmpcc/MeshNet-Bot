from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

from ..models import Event, clean_text, fold_text
from .base import HttpSource, SourceError


TERMINAL_MSG_TYPES = {"cancel", "cancelled", "expired"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _direct_text(element: ET.Element, name: str) -> str:
    wanted = name.casefold()
    for child in list(element):
        if _local(child.tag) == wanted and child.text:
            return clean_text(child.text)
    return ""


def _first(element: ET.Element, name: str) -> ET.Element | None:
    wanted = name.casefold()
    for child in element.iter():
        if _local(child.tag) == wanted:
            return child
    return None


def _iso(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def _severity(cap_severity: str) -> str:
    value = fold_text(cap_severity)
    if value in {"extreme"}:
        return "critical"
    if value in {"severe"}:
        return "high"
    if value in {"moderate"}:
        return "medium"
    return "low"


def _category(event: str, headline: str, description: str) -> str:
    text = fold_text(" ".join((event, headline, description)))
    if any(word in text for word in ("torment", "thunder", "rayo")):
        return "storm"
    if any(word in text for word in ("nev", "snow", "alud")):
        return "snow"
    if any(word in text for word in ("viento", "wind", "galerna")):
        return "strong_wind"
    if any(word in text for word in ("temperatura", "calor", "frio", "heat", "cold")):
        return "extreme_temperature"
    if any(word in text for word in ("lluvia", "precipit", "inund", "flood", "deshielo")):
        return "flood"
    return "storm"


def _parameter_map(info: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for parameter in info.iter():
        if _local(parameter.tag) != "parameter":
            continue
        name = _direct_text(parameter, "valueName")
        value = _direct_text(parameter, "value")
        if name:
            result[name] = value
    return result


class AemetCapSource(HttpSource):
    """Normaliza mensajes CAP 1.2 de AEMET sin alterar el modelo Event existente.

    La fuente puede recibir un CAP individual o un XML que contenga varios
    elementos ``alert``. Los avisos se identifican por el ``identifier`` CAP,
    se clasifican por fenómeno y conservan severidad, vigencia, área y
    parámetros originales en ``metadata``.
    """

    def parse(self, body: bytes) -> list[Event]:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise SourceError("XML CAP con DTD o entidades no admitido")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise SourceError(f"CAP no válido: {exc}") from exc

        alerts = [node for node in root.iter() if _local(node.tag) == "alert"]
        if _local(root.tag) == "alert" and root not in alerts:
            alerts.insert(0, root)
        return [event for alert in alerts if (event := self._event(alert)) is not None]

    def _event(self, alert: ET.Element) -> Event | None:
        identifier = _direct_text(alert, "identifier")
        if not identifier:
            return None
        sent = _iso(_direct_text(alert, "sent"))
        msg_type = _direct_text(alert, "msgType")
        status = "resolved" if fold_text(msg_type) in TERMINAL_MSG_TYPES else "active"
        info = self._select_info(alert)
        if info is None:
            return None

        event_name = _direct_text(info, "event")
        headline = _direct_text(info, "headline")
        description = _direct_text(info, "description")
        instruction = _direct_text(info, "instruction")
        severity = _severity(_direct_text(info, "severity"))
        onset = _iso(_direct_text(info, "onset") or _direct_text(info, "effective") or sent)
        expires = _iso(_direct_text(info, "expires"))
        area = _first(info, "area")
        area_desc = _direct_text(area, "areaDesc") if area is not None else ""
        parameters = _parameter_map(info)

        province = clean_text(parameters.get("province") or parameters.get("Provincia") or "")
        autonomous_region = clean_text(
            parameters.get("autonomous_region") or parameters.get("Comunidad autónoma") or ""
        )
        description_text = " ".join(part for part in (description, instruction) if part)
        return Event(
            event_id=f"{self.source_id}:{identifier}",
            source=self.source_id,
            source_event_id=identifier,
            category=_category(event_name, headline, description_text),
            status=status,
            verification=self.config.get("verification", "official"),
            severity=severity,
            title=headline or event_name or "Aviso meteorológico AEMET",
            description=description_text,
            province=province,
            autonomous_region=autonomous_region,
            started_at=onset,
            updated_at=sent or onset,
            expected_end=expires,
            source_url=self.config.get("source_url", "https://www.aemet.es/es/eltiempo/prediccion/avisos"),
            metadata={
                "cap_msg_type": msg_type,
                "cap_status": _direct_text(alert, "status"),
                "cap_scope": _direct_text(alert, "scope"),
                "cap_event": event_name,
                "cap_urgency": _direct_text(info, "urgency"),
                "cap_certainty": _direct_text(info, "certainty"),
                "cap_area": area_desc,
                "cap_parameters": parameters,
            },
        )

    @staticmethod
    def _select_info(alert: ET.Element) -> ET.Element | None:
        infos = [child for child in list(alert) if _local(child.tag) == "info"]
        if not infos:
            return None
        for info in infos:
            language = fold_text(_direct_text(info, "language"))
            if language.startswith("es") or not language:
                return info
        return infos[0]
