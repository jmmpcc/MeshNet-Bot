#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from bbs_transport import handle_bbs_transport_command  # noqa: E402


class FakeBbs:
    def __init__(self) -> None:
        self.calls = []

    def handle_text(self, **kwargs):
        self.calls.append(kwargs)
        return [" respuesta 1 ", "", "respuesta 2"]


class BbsTransportTests(unittest.TestCase):
    def test_meshcore_dm_is_processed_and_replied_by_dm(self) -> None:
        engine = FakeBbs()
        replies = handle_bbs_transport_command(
            engine=engine,
            text="#BBS MENU",
            source_id="a1b2c3",
            channel=None,
            is_direct=True,
            bbs_callsign="EA2BBS-5",
            allowed_channels={5},
        )
        self.assertEqual([reply.text for reply in replies], ["respuesta 1", "respuesta 2"])
        self.assertTrue(all(reply.direct for reply in replies))
        self.assertEqual(engine.calls[0], {"from_id": "a1b2c3", "ch": 0, "text": "#BBS MENU"})

    def test_public_meshcore_channel_uses_callsign_and_dm_only(self) -> None:
        engine = FakeBbs()
        replies = handle_bbs_transport_command(
            engine=engine,
            text="#BBS EA2BBS-5 LISTAR",
            source_id="a1b2c3",
            channel=7,
            is_direct=False,
            bbs_callsign="EA2BBS-5",
            allowed_channels={7},
            dm_channel=0,
            dm_only=True,
        )
        self.assertEqual(engine.calls[0]["text"], "#BBS LISTAR")
        self.assertEqual(engine.calls[0]["ch"], 0)
        self.assertTrue(all(reply.direct and reply.channel == 0 for reply in replies))

    def test_public_reply_stays_on_meshcore_channel_when_dm_only_is_off(self) -> None:
        engine = FakeBbs()
        replies = handle_bbs_transport_command(
            engine=engine,
            text="#BBS EA2BBS-5 MENU",
            source_id="a1b2c3",
            channel=7,
            is_direct=False,
            bbs_callsign="EA2BBS-5",
            allowed_channels={7},
            dm_only=False,
        )
        self.assertFalse(any(reply.direct for reply in replies))
        self.assertTrue(all(reply.channel == 7 for reply in replies))
        self.assertEqual(engine.calls[0]["text"], "#BBS EA2BBS-5 MENU")

    def test_other_bbs_and_unauthorized_channel_are_ignored(self) -> None:
        engine = FakeBbs()
        common = dict(
            engine=engine,
            source_id="a1b2c3",
            channel=7,
            is_direct=False,
            bbs_callsign="EA2BBS-5",
            allowed_channels={7},
        )
        self.assertEqual(handle_bbs_transport_command(text="#BBS EA1XYZ-3 MENU", **common), ())
        common["channel"] = 8
        self.assertEqual(handle_bbs_transport_command(text="#BBS EA2BBS-5 MENU", **common), ())
        self.assertEqual(engine.calls, [])

    def test_non_bbs_text_is_not_intercepted(self) -> None:
        engine = FakeBbs()
        result = handle_bbs_transport_command(
            engine=engine,
            text="hola mesh",
            source_id="a1b2c3",
            channel=7,
            is_direct=False,
            bbs_callsign="EA2BBS-5",
            allowed_channels={7},
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
