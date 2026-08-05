from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.voice_rf_gateway.config import VoiceRfConfig
from tools.voice_rf_gateway.service import VoiceRfApplication
from tools.voice_rf_gateway.text_normalizer import (
    compose_emergency_voice_text,
    normalize_voice_text,
)
from tools.voice_rf_gateway.tts import SynthesisResult


class VoiceRfGatewayTests(unittest.TestCase):
    def make_config(self, directory: str, enabled: bool = False) -> VoiceRfConfig:
        return VoiceRfConfig(
            service_enabled=enabled,
            bind="127.0.0.1",
            port=8790,
            tts_engine="piper",
            fallback_engine="espeak-ng",
            piper_bin="/missing/piper",
            piper_model="",
            espeak_bin="/missing/espeak-ng",
            espeak_voice="es",
            espeak_speed=145,
            output_dir=Path(directory),
            keep_audio=False,
            max_text_chars=700,
            max_audio_seconds=40.0,
            transmit_enabled=False,
        )

    def test_text_normalizer_removes_urls_and_expands_km(self):
        text = normalize_voice_text("A-124 cortada km 18 https://example.test")
        self.assertIn("A 124", text)
        self.assertIn("kilómetro 18", text)
        self.assertNotIn("http", text)

    def test_test_message_is_unambiguously_marked(self):
        text = compose_emergency_voice_text("Incendio simulado", is_test=True)
        self.assertIn("Prueba técnica", text)
        self.assertIn("No existe una emergencia real", text)

    def test_disabled_service_never_calls_tts(self):
        with tempfile.TemporaryDirectory() as directory:
            app = VoiceRfApplication(self.make_config(directory, enabled=False))
            with patch.object(app.synthesizer, "synthesize") as mocked:
                result = app.dispatch({"event_id": "T1", "text": "Prueba"})
            mocked.assert_not_called()
            self.assertEqual(result["reason"], "disabled")
            self.assertFalse(result["sent"])

    def test_enabled_service_generates_but_never_transmits(self):
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "voice.wav"
            wav.write_bytes(b"RIFF" + b"0" * 80)
            app = VoiceRfApplication(self.make_config(directory, enabled=True))
            fake = SynthesisResult(True, "espeak-ng", str(wav), 2.5, reason="generated")
            with patch.object(app.synthesizer, "synthesize", return_value=fake):
                result = app.dispatch({"event_id": "T2", "text": "Prueba", "is_test": True})
            self.assertTrue(result["generated"])
            self.assertFalse(result["sent"])
            self.assertEqual(result["transmit_reason"], "not_implemented_safety_lock")
            self.assertFalse(wav.exists())

    def test_health_reports_rf_safety_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            app = VoiceRfApplication(self.make_config(directory, enabled=False))
            result = app.health()
            self.assertFalse(result["transmit_enabled"])
            self.assertEqual(result["transmit_reason"], "not_implemented_safety_lock")


if __name__ == "__main__":
    unittest.main()
