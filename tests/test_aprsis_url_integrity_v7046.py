from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest import mock

from tools.emergencias_guardia.emergencias.formatters import compact_messages, google_maps_url
from tools.emergencias_guardia.emergencias.models import Event


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "source" / "meshtastic_to_aprs.py"


def _event() -> Event:
    """Crea una emergencia representativa con URL geográfica real."""
    return Event(
        event_id="v7046-url-test",
        source="test",
        source_event_id="v7046-url-test",
        category="road_closed",
        severity="high",
        status="active",
        title="Corte de tráfico en la CV-128 por incidencia grave",
        description="Corte total de tráfico con desvío alternativo habilitado.",
        road="CV-128",
        kilometre=21.5,
        municipality="Catí",
        province="Castellón",
        latitude=40.47123,
        longitude=0.02234,
    )


def _load_gateway():
    """Carga el gateway aislando aprslib para probar solo la lógica APRS-IS."""
    sys.modules.setdefault("aprslib", mock.MagicMock())
    spec = importlib.util.spec_from_file_location("meshnet_aprs_v7046_test", GATEWAY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compact_message_keeps_complete_map_url_when_it_fits() -> None:
    """Una URL incluida debe conservarse completa, nunca parcialmente."""
    event = _event()
    expected = google_maps_url(event)
    message = compact_messages([event], max_bytes=240, prefix="NUEVA · EMERG")[0]
    assert expected
    assert expected in message


def test_compact_message_drops_map_url_before_byte_truncation() -> None:
    """Si la URL no cabe, se elimina entera antes de recortar el resto."""
    event = _event()
    expected = google_maps_url(event)
    message = compact_messages([event], max_bytes=90, prefix="NUEVA · EMERG")[0]
    assert len(message.encode("utf-8")) <= 90
    assert expected not in message
    assert "https://" not in message
    assert "maps.google" not in message
    assert "https://maps." not in message
    assert "CV-128" in message


def test_aprsis_long_diagnostic_is_disabled_by_default() -> None:
    """La nueva prueba larga no genera tráfico sin autorización explícita."""
    gateway = _load_gateway()
    gateway.APRSIS_LONG_TEST_ENABLED = 0
    with mock.patch.object(gateway, "_aprsis_send_line_safe", new=mock.AsyncMock()) as send:
        result = asyncio.run(gateway.send_aprsis_long_test("PRUEBA larga"))
    assert result["sent"] is False
    assert result["reason"] == "disabled"
    send.assert_not_awaited()


def test_aprsis_long_diagnostic_preserves_text_and_url_over_67_chars() -> None:
    """El diagnóstico APRS-IS no aplica el límite de 67 caracteres del bulletin."""
    gateway = _load_gateway()
    gateway.APRSIS_LONG_TEST_ENABLED = 1
    gateway.APRSIS_LONG_TEST_MAX_CHARS = 400
    gateway.APRSIS_USER = "EB2EAS-11"
    gateway.APRSIS_PASSCODE = "12345"
    text = (
        "PRUEBA APRSIS TEXTO LARGO 001234567890 002345678901 003456789012 "
        "URL https://maps.google.com/?q=41.6488,-0.8891 FIN"
    )
    assert len(text) > 67
    with mock.patch.object(
        gateway, "_aprsis_send_line_safe", new=mock.AsyncMock(return_value=True)
    ) as send:
        result = asyncio.run(gateway.send_aprsis_long_test(text))
    assert result["sent"] is True
    line = send.await_args.args[0]
    assert text in line
    assert "https://maps.google.com/?q=41.6488,-0.8891" in line
    assert line.startswith("EB2EAS-11>APRS,TCPIP*:>")
