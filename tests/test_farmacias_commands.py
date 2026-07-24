import os
import sys
import unittest
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE_DIR))
import farmacias_commands as commands


class _AllowLimiter:
    def check_and_record(self, _ctx):
        return True, 0, False


class _MessagesClient:
    def __init__(self, messages):
        self.messages = messages
        self.seen_text = None

    def query(self, ctx):
        self.seen_text = commands._normalized_command(ctx.text)
        return list(self.messages)


class FarmaciasCommandsTests(unittest.TestCase):
    def setUp(self):
        self.original_limiter = commands._LIMITER
        self.original_client = commands._CLIENT
        self.original_delay = os.environ.get("FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS")
        self.original_limit = os.environ.get("FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE")
        commands._LIMITER = _AllowLimiter()
        os.environ["FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS"] = "0"
        os.environ["FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE"] = "6"

    def tearDown(self):
        commands._LIMITER = self.original_limiter
        commands._CLIENT = self.original_client
        if self.original_delay is None:
            os.environ.pop("FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS", None)
        else:
            os.environ["FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS"] = self.original_delay
        if self.original_limit is None:
            os.environ.pop("FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE", None)
        else:
            os.environ["FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE"] = self.original_limit

    @staticmethod
    def _ctx(text):
        return commands.FarmaciasCommandContext(
            network="meshcore",
            source_id="abc123",
            text=text,
            channel=None,
            is_direct=True,
            packet_id="pkt-1",
        )

    def test_bare_farma_enqueues_every_part_even_above_configured_limit(self):
        client = _MessagesClient([f"parte-{idx}" for idx in range(1, 9)])
        commands._CLIENT = client
        sent = []
        self.assertTrue(commands.handle_farmacias_command(self._ctx("  FARMA  "), sent.append))
        self.assertEqual(client.seen_text, "FARMA")
        self.assertEqual(sent, client.messages)

    def test_filtered_query_keeps_operational_limit(self):
        client = _MessagesClient([f"parte-{idx}" for idx in range(1, 9)])
        commands._CLIENT = client
        sent = []
        commands.handle_farmacias_command(self._ctx("farma zaragoza"), sent.append)
        self.assertEqual(sent[:6], client.messages[:6])
        self.assertEqual(sent[6], "Respuesta truncada a 6 mensajes.")

    def test_command_normalization_never_adds_zaragoza(self):
        for raw in ("farma", "FARMA", "  farma   "):
            self.assertEqual(commands._normalized_command(raw).casefold(), "farma")


if __name__ == "__main__":
    unittest.main()
