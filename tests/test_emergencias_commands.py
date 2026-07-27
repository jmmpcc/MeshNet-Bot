import os
import sys
import unittest
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE_DIR))
import emergencias_commands as commands


class _AllowLimiter:
    def check_and_record(self, _ctx):
        return True, 0, False


class _DuplicateLimiter:
    def check_and_record(self, _ctx):
        return False, 0, True


class _MessagesClient:
    def __init__(self, messages):
        self.messages = messages
        self.seen_text = None

    def query(self, ctx):
        self.seen_text = commands._normalized_command(ctx.text)
        return list(self.messages)


class EmergenciasCommandsTests(unittest.TestCase):
    ENV_NAMES = (
        "EMERGENCIAS_COMMAND_ENABLED",
        "EMERGENCIAS_DM_INTER_MESSAGE_DELAY_SECONDS",
        "EMERGENCIAS_DM_MAX_MESSAGES_PER_RESPONSE",
        "EMERGENCIAS_MESHCORE_CHANNEL",
        "EMERGENCIAS_MESHTASTIC_CHANNEL",
    )

    def setUp(self):
        self.original_limiter = commands._LIMITER
        self.original_client = commands._CLIENT
        self.original_env = {name: os.environ.get(name) for name in self.ENV_NAMES}
        commands._LIMITER = _AllowLimiter()
        os.environ["EMERGENCIAS_COMMAND_ENABLED"] = "true"
        os.environ["EMERGENCIAS_DM_INTER_MESSAGE_DELAY_SECONDS"] = "0"
        os.environ["EMERGENCIAS_DM_MAX_MESSAGES_PER_RESPONSE"] = "4"
        os.environ["EMERGENCIAS_MESHCORE_CHANNEL"] = "-1"
        os.environ["EMERGENCIAS_MESHTASTIC_CHANNEL"] = "-1"

    def tearDown(self):
        commands._LIMITER = self.original_limiter
        commands._CLIENT = self.original_client
        for name, value in self.original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def _ctx(text="emergencias", *, direct=True, channel=None, network="meshcore"):
        return commands.EmergenciasCommandContext(
            network=network,
            source_id="abc123",
            text=text,
            channel=channel,
            is_direct=direct,
            packet_id="pkt-1",
        )

    def test_recognizes_only_complete_command_word(self):
        for text in ("emergencias", "Emergencias A-2", "emerg incendios", "emergencia"):
            self.assertTrue(commands.is_emergencias_command(text))
        for text in ("emergencial", "reemergencias", "farma", ""):
            self.assertFalse(commands.is_emergencias_command(text))

    def test_disabled_command_is_not_consumed(self):
        os.environ["EMERGENCIAS_COMMAND_ENABLED"] = "false"
        sent = []
        self.assertFalse(commands.is_allowed_origin(self._ctx()))
        self.assertFalse(commands.handle_emergencias_command(self._ctx(), sent.append))
        self.assertEqual(sent, [])

    def test_direct_query_uses_api_messages_and_limits_parts(self):
        client = _MessagesClient([f"parte-{index}" for index in range(1, 7)])
        commands._CLIENT = client
        sent = []
        self.assertTrue(commands.handle_emergencias_command(
            self._ctx("  EMERGENCIAS   A-2  "),
            sent.append,
        ))
        self.assertEqual(client.seen_text, "EMERGENCIAS A-2")
        self.assertEqual(sent[:3], client.messages[:3])
        self.assertEqual(sent[3], "Respuesta truncada a 4 mensajes.")
        self.assertEqual(len(sent), 4)

    def test_public_origin_requires_configured_channel(self):
        meshcore = self._ctx(direct=False, channel=2, network="meshcore")
        self.assertFalse(commands.is_allowed_origin(meshcore))
        os.environ["EMERGENCIAS_MESHCORE_CHANNEL"] = "2"
        self.assertTrue(commands.is_allowed_origin(meshcore))

        meshtastic = self._ctx(direct=False, channel=3, network="meshtastic")
        self.assertFalse(commands.is_allowed_origin(meshtastic))
        os.environ["EMERGENCIAS_MESHTASTIC_CHANNEL"] = "3"
        self.assertTrue(commands.is_allowed_origin(meshtastic))

    def test_duplicate_is_consumed_without_second_response(self):
        commands._LIMITER = _DuplicateLimiter()
        commands._CLIENT = _MessagesClient(["no debe enviarse"])
        sent = []
        self.assertTrue(commands.handle_emergencias_command(self._ctx(), sent.append))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
