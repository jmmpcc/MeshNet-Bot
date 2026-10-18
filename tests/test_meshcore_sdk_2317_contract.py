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



class MeshCore2317AdvertisementTests(unittest.IsolatedAsyncioTestCase):
    """Regresión RX_LOG_DATA para anuncios truncados y anuncios válidos.

    La versión 2.2.31 lanzaba IndexError al extraer adv_flags
    desde un paquete de anuncio que terminaba tras la firma.
    Estas pruebas ejecutan el parser real, sin I/O de radio.
    """

    @staticmethod
    def _rx_payload(advertisement: bytes) -> bytes:
        """Construye un paquete Advert (tipo 4), ruta directa sin saltos."""
        return bytes.fromhex("1100") + advertisement

    @staticmethod
    def _advertisement() -> bytes:
        """Devuelve un anuncio sintético válido de longitud mínima 101 bytes."""
        return (
            bytes(range(32))
            + (123456789).to_bytes(4, "little")
            + bytes(range(64))
            + bytes([0])
        )

    async def test_truncated_advertisement_does_not_raise(self) -> None:
        """Si falta adv_flags, el parser no propaga IndexError ni inventa flags."""
        from meshcore.meshcore_parser import MeshcorePacketParser

        parser = MeshcorePacketParser()
        incomplete = self._advertisement()[:-1]
        self.assertEqual(len(incomplete), 100)
        decoded = await parser.parsePacketPayload(self._rx_payload(incomplete), {})
        self.assertEqual(decoded["payload_type"], 4)
        self.assertNotIn("adv_flags", decoded)

    async def test_valid_advertisement_after_truncated_one(self) -> None:
        """La trama válida sigue conservando clave, fecha, firma y flags."""
        from meshcore.meshcore_parser import MeshcorePacketParser

        parser = MeshcorePacketParser()
        await parser.parsePacketPayload(
            self._rx_payload(self._advertisement()[:-1]), {}
        )

        complete = self._advertisement()
        self.assertEqual(len(complete), 101)
        decoded = await parser.parsePacketPayload(self._rx_payload(complete), {})
        self.assertEqual(decoded["payload_type"], 4)
        self.assertEqual(decoded["adv_key"], bytes(range(32)).hex())
        self.assertEqual(decoded["adv_timestamp"], 123456789)
        self.assertEqual(decoded["signature"], bytes(range(64)).hex())
        self.assertEqual(decoded["adv_flags"], 0)
        self.assertEqual(decoded["adv_type"], 0)


if __name__ == "__main__":
    unittest.main()
