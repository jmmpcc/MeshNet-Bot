#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from bbs_transport import (  # noqa: E402
    handle_bbs_transport_command,
    unwrap_meshcore_sender_prefix,
)


class FakeBbs:
    def __init__(self) -> None:
        self.calls = []

    def handle_text(self, **kwargs):
        self.calls.append(kwargs)
        return [" respuesta 1 ", "", "respuesta 2"]


class BbsTransportTests(unittest.TestCase):
    def test_meshcore_sender_prefix_without_space_exposes_bbs_command(self) -> None:
        alias, command = unwrap_meshcore_sender_prefix("EA2FBO_V4:#BBS EA2BBS-5 MENU")
        self.assertEqual(alias, "EA2FBO_V4")
        self.assertEqual(command, "#BBS EA2BBS-5 MENU")

    def test_normal_text_is_not_scanned_for_embedded_bbs_token(self) -> None:
        alias, text = unwrap_meshcore_sender_prefix("comentario sobre #BBS MENU")
        self.assertEqual(alias, "")
        self.assertEqual(text, "comentario sobre #BBS MENU")

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

    def test_bbs_command_is_consumed_when_engine_is_disabled(self) -> None:
        result = handle_bbs_transport_command(
            engine=None,
            text="#BBS",
            source_id="a1b2c3",
            channel=9,
            is_direct=False,
            bbs_callsign="EA2BBS-5",
            allowed_channels={9},
        )
        self.assertEqual(result, ())



class BbsCompactNewsTests(unittest.TestCase):
    """Regresión aislada de NOTICIAS: no arranca broker ni modifica bases reales."""

    def setUp(self) -> None:
        """Prepara una BBS mínima y una SQLite efímera con paginación reproducible."""
        import sqlite3
        from bbs_server import BbsServer

        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE news (id INTEGER PRIMARY KEY, title TEXT, summary TEXT, "
            "source TEXT, published_at TEXT, created_at TEXT, url TEXT, tags TEXT)"
        )
        self.db.executemany(
            "INSERT INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "Noticia uno", "Texto uno", "Fuente A", "2026-10-15T10:00:00",
                 "2026-10-15T10:00:00", "https://example.org/uno?url=muy-larga", "tecnologia"),
                (2, "Noticia dos", "Texto dos", "Fuente B", "2026-10-16T10:00:00",
                 "2026-10-16T10:00:00", "https://example.org/dos", "ciencia"),
                (3, "Noticia tres", "Texto tres", "Fuente C", "2026-10-17T10:00:00",
                 "2026-10-17T10:00:00", "https://example.org/tres", "tecnologia"),
            ],
        )
        self.bbs = BbsServer.__new__(BbsServer)
        self.bbs._conn = self.db
        self.bbs.list_page_size = 2

    def tearDown(self) -> None:
        """Cierra la conexión temporal sin afectar al servicio."""
        self.db.close()

    def test_compact_listing_keeps_pagination_without_urls(self) -> None:
        """Lista dos noticias por página sin transmitir URL ni resumen."""
        page1 = self.bbs._news_list(page=1)
        self.assertIn("NOTICIAS (pág 1/2)", page1)
        self.assertIn("[3] Noticia tres (Fuente C) 2026-10-17", page1)
        self.assertIn("[2] Noticia dos (Fuente B) 2026-10-16", page1)
        self.assertNotIn("[1] Noticia uno", page1)
        self.assertNotIn("https://", page1)
        self.assertIn("#BBS NOTICIAS 2", page1)
        self.assertIn("#BBS NOTICIAS VER <id>", page1)
        page2 = self.bbs._news_list(page=2)
        self.assertIn("[1] Noticia uno", page2)
        self.assertIn("pág 2/2", page2)

    def test_filtered_listing_preserves_categories_and_page(self) -> None:
        """El filtrado por categoría mantiene los identificadores correctos."""
        listing = self.bbs._news_list(tag="tecnologia", page=1)
        self.assertIn("categoría: tecnologia", listing)
        self.assertIn("[3] Noticia tres", listing)
        self.assertIn("[1] Noticia uno", listing)
        self.assertNotIn("[2] Noticia dos", listing)
        self.assertNotIn("https://", listing)

    def test_detail_retains_full_url_and_summary(self) -> None:
        """VER conserva el enlace íntegro y el resumen sin modificaciones."""
        detail = self.bbs._news_read(1)
        self.assertIn("https://example.org/uno?url=muy-larga", detail)
        self.assertIn("Texto uno", detail)
        self.assertIn("Fuente A", detail)

    def test_missing_news_keeps_original_response(self) -> None:
        """Se conserva la respuesta de noticia inexistente."""
        self.assertEqual(self.bbs._news_read(999), "NOTICIAS: id no encontrado: 999")


if __name__ == "__main__":
    unittest.main(verbosity=2)
