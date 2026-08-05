import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "shared" / "app_aprs_dispatcher.py"
spec = importlib.util.spec_from_file_location("app_aprs_dispatcher_test", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules["app_aprs_dispatcher_test"] = module
spec.loader.exec_module(module)


class AppAprsDispatcherTests(unittest.TestCase):
    def test_disabled_global_switch_skips_without_socket(self):
        with mock.patch.dict(os.environ, {
            "APPS_APRS_ENABLED": "0",
            "APPS_APRS_ALLOWED_SOURCES": "farmacias",
        }, clear=False), mock.patch.object(module.socket, "socket") as socket_mock:
            result = module.send_application_aprs(source="farmacias", text="Prueba")
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        socket_mock.assert_not_called()

    def test_source_must_be_in_allowlist(self):
        with mock.patch.dict(os.environ, {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias",
        }, clear=False), mock.patch.object(module.socket, "socket") as socket_mock:
            result = module.send_application_aprs(source="farmacias", text="Prueba")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["error"], "source_not_allowed")
        socket_mock.assert_not_called()

    def test_chunk_limit_blocks_oversized_message(self):
        with mock.patch.dict(os.environ, {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "farmacias",
            "APRS_MAX_LEN": "30",
            "APPS_APRS_MAX_CHUNKS": "1",
        }, clear=False), mock.patch.object(module.socket, "socket") as socket_mock:
            result = module.send_application_aprs(source="farmacias", text="X" * 80)
        self.assertFalse(result["ok"])
        self.assertIn("message_requires_", result["error"])
        socket_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
