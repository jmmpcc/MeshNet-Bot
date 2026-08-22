import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
sys.path.insert(0, str(SOURCE))


class _Caps:
    meshtastic_enabled = True
    meshcore_enabled = True


sys.modules.setdefault(
    "radio_profile",
    SimpleNamespace(resolve_radio_profile=lambda **kwargs: _Caps()),
)

import beacon_bot


class BeaconChannelRangeTests(unittest.TestCase):
    """Valida que cada transporte conserve su rango real de canales."""

    def test_meshtastic_accepts_channel_7(self):
        parsed = beacon_bot._validate_definition(
            ["10", "2", "mt7", "7", "texto"],
            transport="meshtastic",
        )
        self.assertEqual(parsed, (10, 2, "mt7", 7, False, "texto"))

    def test_meshtastic_rejects_channel_8(self):
        parsed = beacon_bot._validate_definition(
            ["10", "2", "mt8", "8", "texto"],
            transport="meshtastic",
        )
        self.assertIsInstance(parsed, str)
        self.assertIn("0 y 7", parsed)

    def test_meshcore_accepts_channel_40(self):
        parsed = beacon_bot._validate_definition(
            ["10", "2", "mc40", "40", "texto"],
            transport="meshcore",
        )
        self.assertEqual(parsed, (10, 2, "mc40", 40, False, "texto"))

    def test_meshcore_rejects_channel_41(self):
        parsed = beacon_bot._validate_definition(
            ["10", "2", "mc41", "41", "texto"],
            transport="meshcore",
        )
        self.assertIsInstance(parsed, str)
        self.assertIn("0 y 40", parsed)

    def test_legacy_helper_default_remains_meshtastic_0_7(self):
        """Una llamada antigua sin transport mantiene exactamente el límite 0..7."""
        parsed = beacon_bot._validate_definition(
            ["10", "2", "legacy", "8", "texto"]
        )
        self.assertIsInstance(parsed, str)
        self.assertIn("0 y 7", parsed)

    def test_contextual_help_shows_both_ranges(self):
        original = beacon_bot._available_transports
        try:
            beacon_bot._available_transports = lambda: {"meshtastic", "meshcore"}
            text = beacon_bot.contextual_help()
        finally:
            beacon_bot._available_transports = original
        self.assertIn("<canal 0-7>", text)
        self.assertIn("<canal 0-40>", text)


if __name__ == "__main__":
    unittest.main()
