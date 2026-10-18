"""Contratos offline del SDK MeshCore 2.3.17 utilizado por el broker.

Ejecución: python -m pytest -q tests/test_meshcore_sdk_2317_contract.py
No abre conexiones de radio, TCP, serie ni BLE. El transporte del SDK
se sustituye por un capturador en memoria.
"""
from __future__ import annotations

import importlib.metadata
import unittest
from types import SimpleNamespace

from meshcore import EventType, MeshCore
from meshcore.commands import CommandHandler


class _CaptureSender:
    """Doble de transporte para inspeccionar paquetes sin transmitirlos."""

    def __init__(self) -> None:
        self.packets: list[bytes] = []

    async def send(self, data: bytes | bytearray, expected_events: object) -> SimpleNamespace:
        """Almacena el comando binario y devuelve una confirmación simulada."""
        self.packets.append(bytes(data))
        return SimpleNamespace(type=EventType.MSG_SENT, payload={"ok": True})


class MeshCore2317ContractTests(unittest.IsolatedAsyncioTestCase):
    """Valida las API y la codificación TRACE que requiere MeshNet-Bot."""

    def test_version_and_discovery_api(self) -> None:
        """Evita que un cambio del resolvedor reintroduzca el SDK antiguo."""
        self.assertEqual(importlib.metadata.version("meshcore"), "2.3.17")
        for method in (
            "send_trace", "send_path_discovery", "send_path_discovery_sync",
            "_send_path_discovery_raw", "get_contacts", "get_channel", "send_msg",
        ):
            with self.subTest(method=method):
                self.assertTrue(callable(getattr(CommandHandler, method, None)))
        self.assertTrue(callable(getattr(MeshCore, "wait_for_event", None)))
        for event in ("PATH_RESPONSE", "TRACE_DATA", "RX_LOG_DATA"):
            with self.subTest(event=event):
                self.assertTrue(hasattr(EventType, event))

    async def test_trace_without_path_uses_firmware_safe_length(self) -> None:
        """Verifica el byte final requerido para superar diez bytes sin ruta."""
        capture = _CaptureSender()
        await CommandHandler.send_trace(
            capture, auth_code=0x12345678, tag=0x11223344,
            flags=0, path=b"",
        )
        self.assertEqual(
            capture.packets,
            [bytes.fromhex("2444332211785634120000")],
        )

    async def test_directed_trace_retains_binary_format(self) -> None:
        """Comprueba que el SDK no modifica rutas dirigidas existentes."""
        capture = _CaptureSender()
        await CommandHandler.send_trace(
            capture, auth_code=0x12345678, tag=0x11223344,
            flags=0, path=bytes.fromhex("aabb"),
        )
        self.assertEqual(
            capture.packets,
            [bytes.fromhex("24443322117856341200aabb")],
        )


if __name__ == "__main__":
    unittest.main()
