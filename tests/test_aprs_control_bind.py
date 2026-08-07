from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "source" / "meshtastic_to_aprs.py"


def load_gateway(env: dict[str, str]):
    """Carga el gateway con un entorno aislado para validar su configuración UDP."""
    sys.modules.setdefault("aprslib", mock.MagicMock())
    spec = importlib.util.spec_from_file_location(
        f"meshnet_aprs_bind_test_{id(env)}", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with mock.patch.dict(os.environ, env, clear=False):
        spec.loader.exec_module(module)
    return module


class AprsControlBindTests(unittest.TestCase):
    def test_bind_defaults_to_historical_client_host(self):
        """Sin APRS_CTRL_BIND se conserva el comportamiento anterior."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APRS_CTRL_BIND", None)
            gateway = load_gateway({"APRS_CTRL_HOST": "127.0.0.1"})
        self.assertEqual(gateway.CONTROL_UDP_HOST, "127.0.0.1")
        self.assertEqual(gateway.CONTROL_UDP_BIND, "127.0.0.1")

    def test_bind_can_listen_on_namespace_without_changing_clients(self):
        """El listener puede usar 0.0.0.0 y los clientes seguir en loopback."""
        gateway = load_gateway({
            "APRS_CTRL_HOST": "127.0.0.1",
            "APRS_CTRL_BIND": "0.0.0.0",
            "APRS_CTRL_PORT": "9464",
        })
        self.assertEqual(gateway.CONTROL_UDP_HOST, "127.0.0.1")
        self.assertEqual(gateway.CONTROL_UDP_BIND, "0.0.0.0")
        self.assertEqual(gateway.CONTROL_UDP_PORT, 9464)

    def test_control_payload_builder_is_single_source_for_preview_and_tx(self):
        """El helper devuelve exactamente los mismos chunks usados por RF."""
        gateway = load_gateway({
            "APRS_STATUS_MAX": "67",
            "APRS_MSG_MAX": "67",
        })
        dest, clean, payloads, header = gateway._build_control_aprs_payloads(
            "broadcast",
            "EMERG " + ("X" * 120),
        )
        self.assertEqual(dest, "broadcast")
        self.assertEqual(header, "APRS")
        self.assertEqual(payloads, gateway.build_aprs_status_chunks(clean))
        self.assertGreaterEqual(len(payloads), 2)
        self.assertTrue(all(len(part) <= 67 for part in payloads))

    def test_control_payload_builder_directed_uses_message_chunking(self):
        """Los destinos dirigidos conservan el troceado APRS de mensajes."""
        gateway = load_gateway({
            "APRS_STATUS_MAX": "67",
            "APRS_MSG_MAX": "67",
        })
        dest, clean, payloads, header = gateway._build_control_aprs_payloads(
            "EA2ABC-7",
            "PRUEBA " + ("Y" * 100),
        )
        self.assertEqual(dest, "EA2ABC-7")
        self.assertEqual(header, "EA2ABC-7")
        self.assertEqual(payloads, gateway.build_aprs_message_chunks(dest, clean))
        self.assertGreaterEqual(len(payloads), 2)


if __name__ == "__main__":
    unittest.main()
