#!/usr/bin/env python3
"""Regresiones del destino/canal recibido por el diálogo de /enviar."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE))

from send_command_parser import normalize_send_args  # noqa: E402


class SendCommandParserTests(unittest.TestCase):
    def test_guided_channel_is_expanded_like_direct_command(self) -> None:
        self.assertEqual(
            normalize_send_args(["canal 2", "mensaje de prueba"]),
            ["canal", "2", "mensaje de prueba"],
        )

    def test_forced_guided_channel_is_expanded(self) -> None:
        self.assertEqual(
            normalize_send_args(["forzado canal 1", "aviso"]),
            ["forzado", "canal", "1", "aviso"],
        )

    def test_alias_with_spaces_is_not_split(self) -> None:
        self.assertEqual(
            normalize_send_args(["Nodo Zaragoza", "hola"]),
            ["Nodo Zaragoza", "hola"],
        )

    def test_enviar_is_registered_only_in_conversation_handler(self) -> None:
        source = (SOURCE / "Telegram_Bot_Broker.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('CommandHandler("enviar", enviar_cmd)'), 1)
        standalone = 'app.add_handler(CommandHandler("enviar", enviar_cmd))'
        self.assertNotIn(standalone, source)
        self.assertIn('entry_points=[CommandHandler("enviar", enviar_cmd)]', source)


if __name__ == "__main__":
    unittest.main()
