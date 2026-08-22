import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    """Valida límites por transporte y que el canal validado llegue intacto al broker."""

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

    def test_meshcore_channel_40_reaches_existing_broker_route_unchanged(self):
        """El nuevo límite no altera MESHCORE_SEND: channel_idx=40 llega intacto."""
        spec = beacon_bot.BeaconSpec("meshcore", 10, 2, "mc40", 40, "texto")
        with patch.object(beacon_bot, "_broker_rpc", return_value={"ok": True}) as rpc:
            result = beacon_bot._send_beacon_sync(spec)

        self.assertTrue(result["ok"])
        cmd, params = rpc.call_args.args
        self.assertEqual(cmd, "MESHCORE_SEND")
        self.assertEqual(params["channel_idx"], 40)
        self.assertEqual(params["text"], "texto")
        self.assertEqual(params["max_retries"], 0)

    def test_meshtastic_channel_7_keeps_existing_send_text_route(self):
        """La separación de límites no modifica la ruta SEND_TEXT de Meshtastic."""
        spec = beacon_bot.BeaconSpec("meshtastic", 10, 2, "mt7", 7, "texto")
        with patch.object(beacon_bot, "_broker_rpc", return_value={"ok": True}) as rpc:
            result = beacon_bot._send_beacon_sync(spec)

        self.assertTrue(result["ok"])
        cmd, params = rpc.call_args.args
        self.assertEqual(cmd, "SEND_TEXT")
        self.assertEqual(params["ch"], 7)
        self.assertEqual(params["text"], "texto")
        self.assertFalse(params["require_ack"])

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
