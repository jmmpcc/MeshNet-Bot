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


if __name__ == "__main__":
    unittest.main()
