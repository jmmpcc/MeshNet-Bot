from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.emergencias_guardia.emergencias.emergency_dispatcher import (
    dispatch_secondary_outputs,
)
from tools.emergencias_guardia.emergencias.models import Event
from tools.emergencias_guardia.emergencias.rf_manager import RfManager


def make_event(severity: str = "high") -> Event:
    return Event(
        event_id="TEST-001",
        source="test",
        source_event_id="1",
        title="Prueba",
        description="Prueba técnica",
        category="wildfire",
        severity=severity,
        status="active",
        verification="official",
        started_at="2026-08-05T10:00:00+00:00",
        updated_at="2026-08-05T10:00:00+00:00",
        province="Zaragoza",
        municipality="Zuera",
        raw_hash="abc",
        metadata={},
    )



class EmergencyDispatcherTests(unittest.TestCase):
    def test_all_secondary_outputs_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            result = dispatch_secondary_outputs(make_event(), "EMERG PRUEBA")
        self.assertEqual(result["aprs_rf"]["reason"], "disabled")
        self.assertEqual(result["aprsis_bulletin"]["reason"], "disabled")
        self.assertEqual(result["voice_rf"]["reason"], "disabled")

    def test_voice_service_disabled_never_transmits(self):
        env = {
            "EMERGENCIAS_VOICE_RF_ENABLED": "1",
            "EMERGENCIAS_VOICE_RF_AUTOMATIC": "1",
            "EMERGENCIAS_VOICE_RF_MIN_LEVEL": "high",
            "VOICE_RF_SERVICE_ENABLED": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(make_event(), "EMERG PRUEBA")
        self.assertFalse(result["voice_rf"]["sent"])
        self.assertEqual(result["voice_rf"]["reason"], "service_disabled")

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher.socket.socket")
    def test_aprs_rf_high_uses_gateway_preview_then_existing_udp_gateway(self, socket_factory):
        """El dispatcher consulta partes reales y después transmite sin cambiar el path."""
        client = socket_factory.return_value.__enter__.return_value
        client.recvfrom.side_effect = [
            (b'{"ok": true, "preview": true, "dest": "broadcast", "parts": 1}', ("127.0.0.1", 9464)),
            (b'{"ok": true, "dest": "broadcast", "parts": 1, "sent": 1, "rf": true}', ("127.0.0.1", 9464)),
        ]
        env = {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias,farmacias",
            "EMERGENCIAS_APRS_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_MIN_LEVEL": "high",
            "APRS_CTRL_HOST": "127.0.0.1",
            "APRS_CTRL_PORT": "9464",
            "APRS_EMERG_DEST": "broadcast",
            "APRS_BOT_PATH": "none",
        }
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(make_event("high"), "EMERG PRUEBA")
        self.assertTrue(result["aprs_rf"]["ok"])
        self.assertEqual(result["aprs_rf"]["sent"], 1)
        self.assertEqual(result["aprs_rf"]["original_parts"], 1)
        self.assertFalse(result["aprs_rf"]["compacted"])
        self.assertTrue(result["aprs_rf"]["aprs_formatted"])
        self.assertIn("EMERG INCENDIO", result["aprs_rf"]["aprs_text"])
        payloads = [call.args[0].decode("utf-8") for call in client.sendto.call_args_list]
        self.assertIn('"mode": "aprs_preview"', payloads[0])
        self.assertIn('"mode": "aprs"', payloads[1])
        self.assertIn('"origin": "app_emergencias"', payloads[1])
        self.assertIn('"path": ""', payloads[1])

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher.socket.socket")
    def test_aprs_rf_medium_is_not_sent(self, socket_factory):
        env = {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias",
            "EMERGENCIAS_APRS_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_MIN_LEVEL": "high",
        }
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(make_event("medium"), "EMERG PRUEBA")
        self.assertEqual(result["aprs_rf"]["reason"], "severity_below_threshold")
        socket_factory.assert_not_called()

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher.socket.socket")
    def test_aprs_rf_accepts_three_real_gateway_parts(self, socket_factory):
        """Emergencias permite tres tramas RF aunque el límite global de apps siga en dos."""
        client = socket_factory.return_value.__enter__.return_value
        client.recvfrom.side_effect = [
            (b'{"ok": true, "preview": true, "dest": "broadcast", "parts": 3}', ("127.0.0.1", 9464)),
            (b'{"ok": true, "dest": "broadcast", "parts": 3, "sent": 3, "rf": true}', ("127.0.0.1", 9464)),
        ]
        env = {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias",
            "APPS_APRS_MAX_CHUNKS": "2",
            "EMERGENCIAS_APRS_RF_MAX_CHUNKS": "3",
            "EMERGENCIAS_APRS_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_MIN_LEVEL": "high",
        }
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(make_event("high"), "X" * 130)
        self.assertTrue(result["aprs_rf"]["ok"])
        self.assertEqual(result["aprs_rf"]["sent"], 3)
        self.assertEqual(result["aprs_rf"]["original_parts"], 3)
        self.assertEqual(result["aprs_rf"]["max_chunks"], 3)
        self.assertFalse(result["aprs_rf"]["compacted"])
        self.assertIn("EMERG INCENDIO", result["aprs_rf"]["aprs_text"])

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher.socket.socket")
    def test_aprs_rf_compacts_only_when_gateway_reports_too_many_parts(self, socket_factory):
        """Un texto de más de tres partes se compacta y se vuelve a validar antes de RF."""
        client = socket_factory.return_value.__enter__.return_value
        client.recvfrom.side_effect = [
            (b'{"ok": true, "preview": true, "dest": "broadcast", "parts": 4}', ("127.0.0.1", 9464)),
            (b'{"ok": true, "preview": true, "dest": "broadcast", "parts": 2}', ("127.0.0.1", 9464)),
            (b'{"ok": true, "dest": "broadcast", "parts": 2, "sent": 2, "rf": true}', ("127.0.0.1", 9464)),
        ]
        env = {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias",
            "EMERGENCIAS_APRS_RF_MAX_CHUNKS": "3",
            "EMERGENCIAS_APRS_RF_COMPACT_MAX_BYTES": "100",
            "EMERGENCIAS_APRS_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_MIN_LEVEL": "high",
        }
        original = "FINALIZADA · EMERG\n" + ("detalle muy largo " * 20)
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(make_event("high"), original)
        self.assertTrue(result["aprs_rf"]["ok"])
        self.assertTrue(result["aprs_rf"]["compacted"])
        self.assertEqual(result["aprs_rf"]["original_parts"], 4)
        self.assertEqual(result["aprs_rf"]["rf_parts"], 2)
        payloads = [call.args[0].decode("utf-8") for call in client.sendto.call_args_list]
        self.assertEqual(len(payloads), 3)
        self.assertIn("EMERG INCENDIO", payloads[1])
        self.assertIn('"mode": "aprs"', payloads[2])

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher.socket.socket")
    def test_aprs_rf_never_transmits_if_compact_text_still_exceeds_limit(self, socket_factory):
        """El límite sigue siendo una barrera dura si ni el resumen cabe en tres partes."""
        client = socket_factory.return_value.__enter__.return_value
        client.recvfrom.side_effect = [
            (b'{"ok": true, "preview": true, "dest": "broadcast", "parts": 5}', ("127.0.0.1", 9464)),
            (b'{"ok": true, "preview": true, "dest": "broadcast", "parts": 4}', ("127.0.0.1", 9464)),
        ]
        env = {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias",
            "EMERGENCIAS_APRS_RF_MAX_CHUNKS": "3",
            "EMERGENCIAS_APRS_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_MIN_LEVEL": "high",
        }
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(make_event("high"), "X" * 500)
        self.assertFalse(result["aprs_rf"]["ok"])
        self.assertEqual(result["aprs_rf"]["reason"], "chunk_limit_exceeded")
        self.assertEqual(result["aprs_rf"]["original_parts"], 5)
        self.assertEqual(result["aprs_rf"]["compact_parts"], 4)
        self.assertEqual(len(client.sendto.call_args_list), 2)

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher.socket.socket")
    def test_aprsis_uses_single_line_human_readable_emergency_type(self, socket_factory):
        client = socket_factory.return_value.__enter__.return_value
        client.recvfrom.return_value = (
            b'{"ok": true, "sent": true, "bulletin": "BLN3"}',
            ("127.0.0.1", 9464),
        )
        env = {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias",
            "EMERGENCIAS_APRS_ENABLED": "1",
            "APRSIS_PUSH_ENABLED": "1",
            "APRSIS_EMERGENCY_BULLETIN_ENABLED": "1",
            "APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL": "high",
        }
        event = make_event("high")
        event.road = "A-23"
        event.kilometre = 312
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(event, "mensaje mesh deliberadamente largo " * 8)
        self.assertTrue(result["aprsis_bulletin"]["sent"])
        payload = client.sendto.call_args.args[0].decode("utf-8")
        self.assertIn('"text": "EMERG INCENDIO | A-23 km 312 | Zuera | Prueba"', payload)

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher._send_aprs_rf")
    def test_aprs_rf_failure_is_isolated(self, mocked):
        mocked.side_effect = TimeoutError("sin respuesta")
        with patch.dict(os.environ, {}, clear=True):
            result = dispatch_secondary_outputs(make_event(), "EMERG PRUEBA")
        self.assertEqual(result["aprs_rf"]["reason"], "request_failed")
        self.assertIn("TimeoutError", result["aprs_rf"]["error"])

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher._send_aprsis_bulletin")
    def test_aprsis_failure_is_isolated(self, mocked):
        mocked.side_effect = TimeoutError("sin respuesta")
        with patch.dict(os.environ, {}, clear=True):
            result = dispatch_secondary_outputs(make_event(), "EMERG PRUEBA")
        self.assertEqual(result["aprsis_bulletin"]["reason"], "request_failed")
        self.assertIn("TimeoutError", result["aprsis_bulletin"]["error"])

    def test_rf_manager_exclusive_lock_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = RfManager(str(Path(directory) / "rf.lock"))
            with manager.acquire(owner="test", priority=10) as lease:
                self.assertEqual(lease.owner, "test")
                state = Path(str(manager.lock_path) + ".json")
                self.assertTrue(state.exists())
            self.assertTrue(manager.state_path.exists())


if __name__ == "__main__":
    unittest.main()
