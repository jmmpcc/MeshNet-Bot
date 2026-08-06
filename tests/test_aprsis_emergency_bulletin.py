from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "source" / "meshtastic_to_aprs.py"


def load_gateway():
    sys.modules.setdefault("aprslib", mock.MagicMock())
    spec = importlib.util.spec_from_file_location("meshnet_aprs_phase2a_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AprsIsEmergencyBulletinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = load_gateway()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "bulletins.json"
        self.gateway.APRSIS_PUSH_ENABLED = 1
        self.gateway.APRSIS_EMERGENCY_BULLETIN_ENABLED = 1
        self.gateway.APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL = "high"
        self.gateway.APRSIS_EMERGENCY_BULLETIN_MIN_INTERVAL_SEC = 0
        self.gateway.APRSIS_EMERGENCY_BULLETIN_DEDUP_SEC = 1800
        self.gateway.APRSIS_EMERGENCY_BULLETIN_GROUP = ""
        self.gateway.APRSIS_AEMET_BULLETIN_GROUP = "AEMET"
        self.gateway.APRSIS_FARMACIAS_BULLETIN_GROUP = "FARMA"
        self.gateway.APRSIS_NEWS_BULLETIN_GROUP = "NEWS"
        self.gateway.APRSIS_SYSTEM_BULLETIN_GROUP = "MESH"
        self.gateway.APRSIS_TEST_BULLETIN_GROUP = "TEST"
        self.gateway.APRSIS_EMERGENCY_BULLETIN_STATE_PATH = self.state_path
        self.gateway.APRSIS_USER = "EB2EAS-11"
        self.gateway.APRSIS_PASSCODE = "12345"

    def tearDown(self):
        self.temp.cleanup()

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_disabled_switch_never_sends(self):
        self.gateway.APRSIS_EMERGENCY_BULLETIN_ENABLED = 0
        with mock.patch.object(self.gateway, "_aprsis_send_line_safe", new=mock.AsyncMock()) as send:
            result = self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-1", text="EMERG ZAR: prueba", severity="high", status="active",
            ))
        self.assertEqual(result["reason"], "bulletin_disabled")
        send.assert_not_awaited()

    def test_low_severity_never_sends(self):
        with mock.patch.object(self.gateway, "_aprsis_send_line_safe", new=mock.AsyncMock()) as send:
            result = self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-1", text="Aviso menor", severity="medium", status="active",
            ))
        self.assertEqual(result["reason"], "severity_below_threshold")
        send.assert_not_awaited()

    def test_high_event_uses_public_bln_without_message_id(self):
        with mock.patch.object(
            self.gateway, "_aprsis_send_line_safe", new=mock.AsyncMock(return_value=True)
        ) as send:
            result = self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-1", text="EMERG ZAR: A-2 cortada", severity="high", status="active",
            ))
        self.assertTrue(result["sent"])
        self.assertEqual(result["bulletin"], "BLN0")
        line = send.await_args.args[0]
        self.assertIn("::BLN0     :", line)
        self.assertNotRegex(line, r"\{\d{2}\}$")
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["events"]["evt-1"]["bulletin"], "BLN0")


    def test_reserved_group_catalog(self):
        expected = {
            "emergencias": "",
            "aemet": "AEMET",
            "farmacias": "FARMA",
            "news": "NEWS",
            "meshnet": "MESH",
            "test": "TEST",
        }
        for source, group in expected.items():
            with self.subTest(source=source):
                self.assertEqual(self.gateway._aprsis_bulletin_group_for(source), group)

    def test_unknown_group_source_is_safe_and_empty(self):
        self.assertEqual(self.gateway._aprsis_bulletin_group_for("desconocida"), "")

    def test_grouped_bulletin_uses_emerg_group(self):
        self.gateway.APRSIS_EMERGENCY_BULLETIN_GROUP = "EMERG"
        with mock.patch.object(
            self.gateway, "_aprsis_send_line_safe", new=mock.AsyncMock(return_value=True)
        ) as send:
            result = self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-group", text="EMERG ZAR: incendio", severity="high", status="active",
            ))
        self.assertEqual(result["bulletin"], "BLN0EMERG")
        self.assertIn("::BLN0EMERG:", send.await_args.args[0])

    def test_group_is_sanitized_and_limited_to_five_chars(self):
        self.gateway.APRSIS_EMERGENCY_BULLETIN_GROUP = "emergencias-2026"
        self.assertEqual(self.gateway._normalize_aprsis_bulletin_group(
            self.gateway.APRSIS_EMERGENCY_BULLETIN_GROUP
        ), "EMERG")

    def test_enabling_group_migrates_existing_slot_without_rate_limit(self):
        self.state_path.write_text(json.dumps({
            "version": 1,
            "events": {
                "evt-1": {
                    "bulletin": "BLN4",
                    "digest": "old",
                    "last_sent": 9999999999,
                    "closed": False,
                }
            },
        }), encoding="utf-8")
        self.gateway.APRSIS_EMERGENCY_BULLETIN_GROUP = "EMERG"
        self.gateway.APRSIS_EMERGENCY_BULLETIN_MIN_INTERVAL_SEC = 300
        with mock.patch.object(
            self.gateway, "_aprsis_send_line_safe", new=mock.AsyncMock(return_value=True)
        ) as send:
            result = self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-1", text="EMERG ZAR: incendio", severity="high", status="active",
            ))
        self.assertTrue(result["sent"])
        self.assertEqual(result["bulletin"], "BLN4EMERG")
        self.assertIn("::BLN4EMERG:", send.await_args.args[0])

    def test_duplicate_is_suppressed(self):
        with mock.patch.object(
            self.gateway, "_aprsis_send_line_safe", new=mock.AsyncMock(return_value=True)
        ) as send:
            first = self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-1", text="EMERG ZAR: incendio", severity="high", status="active",
            ))
            second = self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-1", text="EMERG ZAR: incendio", severity="high", status="active",
            ))
        self.assertTrue(first["sent"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(send.await_count, 1)

    def test_resolved_event_keeps_slot_and_adds_fin(self):
        with mock.patch.object(
            self.gateway, "_aprsis_send_line_safe", new=mock.AsyncMock(return_value=True)
        ) as send:
            self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-1", text="EMERG ZAR: incendio activo", severity="high", status="active",
            ))
            result = self.run_async(self.gateway.send_aprsis_emergency_bulletin(
                event_id="evt-1", text="EMERG ZAR: incendio controlado", severity="high", status="resolved",
            ))
        self.assertEqual(result["bulletin"], "BLN0")
        self.assertIn(":FIN EMERG ZAR", send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
