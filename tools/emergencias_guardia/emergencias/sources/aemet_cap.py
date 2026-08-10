from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from ..models import Event, clean_text, fold_text
from .base import HttpSource, SourceError


TERMINAL_MSG_TYPES = {"cancel", "cancelled", "expired"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _direct_text(element: ET.Element | None, name: str) -> str:
    if element is None:
        return ""
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
    if value == "extreme":
        return "critical"
    if value == "severe":
        return "high"
    if value == "moderate":
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
    """Consume AEMET OpenData y normaliza los mensajes CAP 1.2 a ``Event``.

    AEMET OpenData responde primero con JSON y una URL temporal ``datos``. Este
    conector resuelve ese segundo salto de forma explícita, manteniendo la API
    key fuera de la configuración persistente y aplicando los mismos límites de
    tamaño y timeout que el resto de fuentes.
    """

    def fetch(self) -> tuple[list[Event], bool]:
        api_key_env = clean_text(self.config.get("api_key_env", "AEMET_API_KEY"))
        api_key = clean_text(os.getenv(api_key_env, ""))
        if not api_key:
            raise SourceError(f"{api_key_env} no configurada")
        endpoint = clean_text(self.config.get("url"))
        if not endpoint.startswith(("https://", "http://")):
            raise SourceError("URL AEMET no configurada")
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}{urllib.parse.urlencode({'api_key': api_key})}"
        metadata = self._download_json(url)
        data_url = clean_text(metadata.get("datos"))
        if not data_url.startswith(("https://", "http://")):
            description = clean_text(metadata.get("descripcion"))
            raise SourceError(description or "AEMET OpenData no devolvió URL de datos CAP")
        body = self._download_bytes(data_url)
        return self.parse(body), False

    def _download_json(self, url: str) -> dict[str, object]:
        raw = self._download_bytes(url)
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"respuesta AEMET OpenData no válida: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceError("respuesta AEMET OpenData inesperada")
        return payload

    def _download_bytes(self, url: str) -> bytes:
        timeout = float(self.app_config["fetch"]["timeout_seconds"])
        maximum = int(self.app_config["fetch"]["max_response_bytes"])
        headers = {
            "User-Agent": self.app_config["fetch"]["user_agent"],
            "Accept-Encoding": "identity",
        }
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > maximum:
                    raise SourceError("respuesta AEMET mayor que el límite configurado")
                body = response.read(maximum + 1)
                if len(body) > maximum:
                    raise SourceError("respuesta AEMET mayor que el límite configurado")
                return body
        except urllib.error.HTTPError as exc:
            raise SourceError(f"AEMET HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise SourceError(str(exc)) from exc

    def parse(self, body: bytes) -> list[Event]:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise SourceError("XML CAP con DTD o entidades no admitido")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise SourceError(f"CAP no válido: {exc}") from exc
        alerts = [node for node in root.iter() if _local(node.tag) == "alert"]
        if _local(root.tag) == "alert" and not alerts:
            alerts = [root]
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
        area_desc = _direct_text(area, "areaDesc")
        parameters = _parameter_map(info)
        province = clean_text(parameters.get("province") or parameters.get("Provincia") or "")
        autonomous_region = clean_text(parameters.get("autonomous_region") or parameters.get("Comunidad autónoma") or "")
        description_text = " ".join(part for part in (description, instruction) if part)
        return Event(
            event_id=f"{self.source_id}:{identifier}", source=self.source_id,
            source_event_id=identifier, category=_category(event_name, headline, description_text),
            status=status, verification=self.config.get("verification", "official"), severity=severity,
            title=headline or event_name or "Aviso meteorológico AEMET", description=description_text,
            province=province, autonomous_region=autonomous_region,
            started_at=onset, updated_at=sent or onset, expected_end=expires,
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
