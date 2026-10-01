from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OptionalScopeHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = _load_module(
            "meshcore_optional_scope_test",
            ROOT / "shared" / "meshcore_optional_scope.py",
        )

    def test_empty_scope_preserves_historical_payload(self):
        params = {"kind": "chan", "channel_idx": 3, "text": "Aviso"}
        result = self.helper.add_optional_channel_scope("MESHCORE_SEND", params, "")
        self.assertIs(result, params)
        self.assertEqual(result, params)
        self.assertNotIn("scope", result)

    def test_scope_is_added_only_to_meshcore_channel_tx(self):
        params = {"kind": "chan", "channel_idx": 3, "text": "Aviso"}
        result = self.helper.add_optional_channel_scope(
            "MESHCORE_SEND", params, "#Utebo"
        )
        self.assertEqual(result["scope"], "#Utebo")
        self.assertNotIn("scope", params)

        dm = {"kind": "contact", "contact_prefix": "abc", "text": "Respuesta"}
        self.assertIs(
            self.helper.add_optional_channel_scope("MESHCORE_SEND", dm, "#Utebo"),
            dm,
        )
        self.assertNotIn("scope", dm)

        meshtastic = {"ch": 3, "text": "Aviso"}
        self.assertIs(
            self.helper.add_optional_channel_scope("SEND_TEXT", meshtastic, "#Utebo"),
            meshtastic,
        )
        self.assertNotIn("scope", meshtastic)

    def test_existing_per_tx_scope_has_priority(self):
        params = {
            "kind": "chan",
            "channel_idx": 3,
            "text": "Aviso",
            "scope": "#Zaragoza",
        }
        result = self.helper.add_optional_channel_scope(
            "MESHCORE_SEND", params, "#Utebo"
        )
        self.assertIs(result, params)
        self.assertEqual(result["scope"], "#Zaragoza")


class AutomaticApplicationScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.farmacias = _load_module(
            "farmacias_guardia_scoped_test",
            ROOT / "tools" / "farmacias_guardia" / "farmacias_guardia_scoped.py",
        )
        cls.emergencias = _load_module(
            "emergencias_guardia_scoped_test",
            ROOT / "tools" / "emergencias_guardia" / "emergencias_guardia_scoped.py",
        )

    def test_farmacias_channel_tx_receives_configured_scope_but_dm_does_not(self):
        calls = []
        original = self.farmacias._ORIGINAL_BROKER_REQUEST
        try:
            self.farmacias._ORIGINAL_BROKER_REQUEST = (
                lambda command, params: calls.append((command, dict(params))) or {"ok": True}
            )
            with mock.patch.dict(
                os.environ, {"FARMACIAS_MESHCORE_SCOPE": "#Utebo"}, clear=False
            ):
                self.farmacias._scoped_broker_request(
                    "MESHCORE_SEND",
                    {"kind": "chan", "channel_idx": 1, "text": "Farmacias"},
                )
                self.farmacias._scoped_broker_request(
                    "MESHCORE_SEND",
                    {"kind": "contact", "contact_prefix": "abc", "text": "DM"},
                )
        finally:
            self.farmacias._ORIGINAL_BROKER_REQUEST = original

        self.assertEqual(calls[0][1]["scope"], "#Utebo")
        self.assertNotIn("scope", calls[1][1])

    def test_farmacias_without_scope_preserves_channel_payload(self):
        calls = []
        original = self.farmacias._ORIGINAL_BROKER_REQUEST
        try:
            self.farmacias._ORIGINAL_BROKER_REQUEST = (
                lambda command, params: calls.append((command, dict(params))) or {"ok": True}
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FARMACIAS_MESHCORE_SCOPE", None)
                self.farmacias._scoped_broker_request(
                    "MESHCORE_SEND",
                    {"kind": "chan", "channel_idx": 1, "text": "Farmacias"},
                )
        finally:
            self.farmacias._ORIGINAL_BROKER_REQUEST = original

        self.assertEqual(
            calls[0][1],
            {"kind": "chan", "channel_idx": 1, "text": "Farmacias"},
        )

    def test_emergencias_scope_applies_only_to_meshcore_channel(self):
        calls = []
        original = self.emergencias._ORIGINAL_BROKER_REQUEST
        try:
            self.emergencias._ORIGINAL_BROKER_REQUEST = (
                lambda config, command, params: calls.append(
                    (command, dict(params))
                ) or {"ok": True}
            )
            with mock.patch.dict(
                os.environ, {"EMERGENCIAS_MESHCORE_SCOPE": "#Utebo"}, clear=False
            ):
                self.emergencias._scoped_broker_request(
                    {"notifications": {}},
                    "MESHCORE_SEND",
                    {"kind": "chan", "channel_idx": 2, "text": "DGT"},
                )
                self.emergencias._scoped_broker_request(
                    {"notifications": {}},
                    "SEND_TEXT",
                    {"ch": 2, "text": "DGT"},
                )
        finally:
            self.emergencias._ORIGINAL_BROKER_REQUEST = original

        self.assertEqual(calls[0][1]["scope"], "#Utebo")
        self.assertNotIn("scope", calls[1][1])

    def test_systemd_and_control_panel_use_scoped_launchers_for_transmission(self):
        daily = (
            ROOT
            / "tools"
            / "farmacias_guardia"
            / "systemd"
            / "meshnet-farmacias-daily.service"
        ).read_text(encoding="utf-8")
        check = (
            ROOT
            / "tools"
            / "farmacias_guardia"
            / "systemd"
            / "meshnet-farmacias-check.service"
        ).read_text(encoding="utf-8")
        emergency = (
            ROOT
            / "tools"
            / "emergencias_guardia"
            / "systemd"
            / "meshnet-emergencias-check.service"
        ).read_text(encoding="utf-8")
        manifest = (
            ROOT
            / "tools"
            / "ControlPanel"
            / "manifests"
            / "farmacias_guardia.json"
        ).read_text(encoding="utf-8")

        self.assertIn("farmacias_guardia_scoped.py send", daily)
        self.assertIn("farmacias_guardia_scoped.py check --send", check)
        self.assertIn(
            "emergencias_guardia_scoped.py check --notify-changes", emergency
        )
        self.assertIn("farmacias_guardia_scoped.py\",\"send\",\"--force", manifest)


if __name__ == "__main__":
    unittest.main()
