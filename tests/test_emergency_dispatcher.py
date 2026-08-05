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
