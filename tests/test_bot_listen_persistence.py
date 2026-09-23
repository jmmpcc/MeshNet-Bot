from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "source" / "telegram_listen_state.py"
BOT_PATH = ROOT / "source" / "Telegram_Bot_Broker.py"


def load_module():
    spec = importlib.util.spec_from_file_location("telegram_listen_state_tested", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TelegramListenStateTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "telegram_listen_state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_empty_state(self):
        self.assertEqual(
            self.module.load_state(self.path),
            {"version": 1, "listeners": {}},
        )

    def test_persists_all_channels_as_null(self):
        state = self.module.set_listener(123, enabled=True, channel=None, path=self.path)
        self.assertEqual(
            state["listeners"]["123"],
            {"enabled": True, "channel": None},
        )
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["listeners"]["123"]["channel"], None)

    def test_persists_specific_channel(self):
        self.module.set_listener(123, enabled=True, channel=4, path=self.path)
        loaded = self.module.load_state(self.path)
        self.assertEqual(
            loaded["listeners"]["123"],
            {"enabled": True, "channel": 4},
        )

    def test_stop_is_persisted_explicitly(self):
        self.module.set_listener(123, enabled=True, channel=None, path=self.path)
        self.module.set_listener(123, enabled=False, channel=None, path=self.path)
        loaded = self.module.load_state(self.path)
        self.assertEqual(
            loaded["listeners"]["123"],
            {"enabled": False, "channel": None},
        )

    def test_updating_one_chat_preserves_other_chats(self):
        self.module.set_listener(123, enabled=True, channel=None, path=self.path)
        self.module.set_listener(456, enabled=True, channel=2, path=self.path)
        self.module.set_listener(123, enabled=False, channel=None, path=self.path)
        loaded = self.module.load_state(self.path)
        self.assertEqual(loaded["listeners"]["456"], {"enabled": True, "channel": 2})

    def test_corrupt_json_degrades_to_empty_state(self):
        self.path.write_text("{not-json", encoding="utf-8")
        self.assertEqual(
            self.module.load_state(self.path),
            {"version": 1, "listeners": {}},
        )

    def test_malformed_listener_is_ignored(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "listeners": {
                        "123": {"enabled": True, "channel": "all"},
                        "456": {"enabled": True, "channel": 3},
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = self.module.load_state(self.path)
        self.assertNotIn("123", loaded["listeners"])
        self.assertEqual(loaded["listeners"]["456"], {"enabled": True, "channel": 3})

    def test_write_uses_atomic_replace(self):
        real_replace = os.replace
        calls = []

        def recording_replace(src, dst):
            calls.append((Path(src), Path(dst)))
            return real_replace(src, dst)

        with mock.patch.object(self.module.os, "replace", side_effect=recording_replace):
            self.module.set_listener(123, enabled=True, channel=None, path=self.path)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], self.path)
        self.assertTrue(calls[0][0].name.endswith(".json.tmp"))


class TelegramListenIntegrationStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BOT_PATH.read_text(encoding="utf-8")

    def _function_source(self, name: str, next_name: str) -> str:
        start = self.source.index(f"async def {name}(")
        end = self.source.index(f"async def {next_name}(", start)
        return self.source[start:end]

    def test_escuchar_persists_enabled_preference(self):
        body = self._function_source("escuchar_cmd", "refrescar_nodos_cmd")
        self.assertIn("_persist_listener_preference(", body)
        self.assertIn("enabled=True", body)
        self.assertIn("channel=listen_chan", body)

    def test_parar_persists_disabled_before_runtime_cancellation(self):
        body = self._function_source("parar_escucha_cmd", "escuchar_cmd")
        persist_at = body.index("_persist_listener_preference(")
        cancel_at = body.index('task = context.chat_data.pop("listen_task", None)')
        self.assertLess(persist_at, cancel_at)
        self.assertIn("enabled=False", body)

    def test_post_startup_restores_persisted_listeners(self):
        start = self.source.index("async def post_startup(")
        end = self.source.index("\ndef main()", start)
        body = self.source[start:end]
        self.assertIn("await _restore_persisted_listeners(app)", body)

    def test_restore_rebuilds_runtime_not_serialized_runtime_objects(self):
        start = self.source.index("async def _restore_persisted_listeners(")
        end = self.source.index("\ndef build_application()", start)
        body = self.source[start:end]
        self.assertIn('app.bot_data["listen_active_count"] = 0', body)
        self.assertIn("app.chat_data[chat_id]", body)
        self.assertIn("SimpleNamespace(", body)
        self.assertIn("app.create_task(", body)
        self.assertNotIn("listen_writer", body)


if __name__ == "__main__":
    unittest.main()
