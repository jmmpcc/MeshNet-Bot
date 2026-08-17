import asyncio
import os
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


sys.modules.setdefault("radio_profile", SimpleNamespace(resolve_radio_profile=lambda **kwargs: _Caps()))

import beacon_bot


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class BeaconBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        beacon_bot._ACTIVE_BEACONS.clear()
        os.environ["ADMIN_IDS"] = "123"

    async def asyncTearDown(self):
        for spec in list(beacon_bot._ACTIVE_BEACONS.values()):
            if spec.task and not spec.task.done():
                spec.task.cancel()
        await asyncio.sleep(0)
        beacon_bot._ACTIVE_BEACONS.clear()

    def _update(self, uid=123):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=uid),
            effective_message=_Message(),
        )

    def test_validate_definition_legacy_is_unnumbered(self):
        parsed = beacon_bot._validate_definition(
            ["10", "3", "guardia", "2", "Texto", "de", "prueba"]
        )
        self.assertEqual(parsed, (10, 3, "guardia", 2, False, "Texto de prueba"))

    def test_validate_definition_numbered(self):
        parsed = beacon_bot._validate_definition(
            ["10", "3", "guardia", "2", "num", "Texto", "de", "prueba"]
        )
        self.assertEqual(parsed, (10, 3, "guardia", 2, True, "Texto de prueba"))

    def test_validate_definition_explicit_nonum(self):
        parsed = beacon_bot._validate_definition(
            ["10", "3", "guardia", "2", "nonum", "Texto"]
        )
        self.assertEqual(parsed, (10, 3, "guardia", 2, False, "Texto"))

    def test_validate_rejects_bad_values(self):
        self.assertIsInstance(beacon_bot._validate_definition(["0", "3", "x", "2", "txt"]), str)
        self.assertIsInstance(beacon_bot._validate_definition(["10", "0", "x", "2", "txt"]), str)
        self.assertIsInstance(beacon_bot._validate_definition(["10", "3", "nombre con espacios", "2", "txt"]), str)
        self.assertIsInstance(beacon_bot._validate_definition(["10", "3", "x", "8", "txt"]), str)
        self.assertIsInstance(beacon_bot._validate_definition(["10", "3", "x", "2", "num"]), str)
        self.assertIsInstance(beacon_bot._validate_definition(["10", "3", "x", "2", "nonum"]), str)

    def test_send_meshtastic_uses_existing_broker_route_without_number(self):
        spec = beacon_bot.BeaconSpec("meshtastic", 10, 1, "mt", 3, "hola")
        with patch.object(beacon_bot, "_broker_rpc", return_value={"ok": True}) as rpc:
            result = beacon_bot._send_beacon_sync(spec)
        self.assertTrue(result["ok"])
        cmd, params = rpc.call_args.args
        self.assertEqual(cmd, "SEND_TEXT")
        self.assertEqual(params["ch"], 3)
        self.assertEqual(params["text"], "hola")
        self.assertFalse(params["require_ack"])

    def test_send_meshcore_numbered_uses_existing_broker_route(self):
        spec = beacon_bot.BeaconSpec("meshcore", 10, 1, "mc", 4, "hola mc", numbered=True)
        with patch.object(beacon_bot, "_broker_rpc", return_value={"ok": True}) as rpc:
            result = beacon_bot._send_beacon_sync(spec)
        self.assertTrue(result["ok"])
        cmd, params = rpc.call_args.args
        self.assertEqual(cmd, "MESHCORE_SEND")
        self.assertEqual(params["channel_idx"], 4)
        self.assertEqual(params["text"], "1. hola mc")

    async def test_numbering_advances_only_after_successful_broker_ack(self):
        spec = beacon_bot.BeaconSpec("meshtastic", 10, 1, "mt", 1, "aviso", numbered=True)
        sent = []

        def fake_rpc(cmd, params, timeout=8.0):
            sent.append(params["text"])
            return {"ok": len(sent) != 2}

        with patch.object(beacon_bot, "_broker_rpc", side_effect=fake_rpc):
            await beacon_bot._send_beacon(spec)
            await beacon_bot._send_beacon(spec)
            await beacon_bot._send_beacon(spec)

        self.assertEqual(sent, ["1. aviso", "2. aviso", "2. aviso"])
        self.assertEqual(spec.sequence, 2)

    async def test_start_and_stop_by_name(self):
        update = self._update()
        context = SimpleNamespace(args=["60", "1", "prueba", "1", "num", "texto"])
        with patch.object(beacon_bot, "_available_transports", return_value={"meshcore"}), \
             patch.object(beacon_bot, "_send_beacon", new=lambda spec: asyncio.sleep(0, result={"ok": True})):
            await beacon_bot.baliza_mc_cmd(update, context)
            self.assertIn(("meshcore", "prueba"), beacon_bot._ACTIVE_BEACONS)
            self.assertTrue(beacon_bot._ACTIVE_BEACONS[("meshcore", "prueba")].numbered)
            await beacon_bot.parar_baliza_mc_cmd(update, SimpleNamespace(args=["prueba"]))
            self.assertNotIn(("meshcore", "prueba"), beacon_bot._ACTIVE_BEACONS)
        self.assertIn("activada", update.effective_message.replies[0])
        self.assertIn("Numeración: SI", update.effective_message.replies[0])
        self.assertIn("detenida", update.effective_message.replies[-1])

    async def test_duplicate_name_is_rejected(self):
        update = self._update()
        context = SimpleNamespace(args=["60", "1", "dup", "1", "texto"])
        with patch.object(beacon_bot, "_available_transports", return_value={"meshcore"}), \
             patch.object(beacon_bot, "_send_beacon", new=lambda spec: asyncio.sleep(0, result={"ok": True})):
            await beacon_bot.baliza_mc_cmd(update, context)
            await beacon_bot.baliza_mc_cmd(update, context)
        self.assertTrue(any("Ya existe" in x for x in update.effective_message.replies))

    async def test_non_admin_cannot_create(self):
        update = self._update(uid=999)
        await beacon_bot.baliza_mc_cmd(update, SimpleNamespace(args=["10", "1", "x", "0", "txt"]))
        self.assertEqual(beacon_bot._ACTIVE_BEACONS, {})
        self.assertIn("administradores", update.effective_message.replies[-1])

    def test_contextual_help_respects_profile_and_shows_number_mode(self):
        with patch.object(beacon_bot, "_available_transports", return_value={"meshcore"}):
            text = beacon_bot.contextual_help()
        self.assertIn("/baliza_mc", text)
        self.assertIn("[num|nonum]", text)
        self.assertNotIn("/baliza <", text)


if __name__ == "__main__":
    unittest.main()
