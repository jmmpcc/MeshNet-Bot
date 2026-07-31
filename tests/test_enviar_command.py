#!/usr/bin/env python3
"""Regresiones estructurales del comando directo /enviar."""

from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "source" / "Telegram_Bot_Broker.py"


class EnviarCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_enviar_is_a_single_direct_command_handler(self) -> None:
        registration = 'app.add_handler(CommandHandler("enviar", enviar_cmd))'
        self.assertEqual(self.source.count(registration), 1)
        self.assertNotIn('entry_points=[CommandHandler("enviar", enviar_cmd)]', self.source)

    def test_empty_command_help_lists_supported_routes(self) -> None:
        for example in (
            "/enviar canal 2 Aviso de prueba",
            "/enviar broadcast:1 Mensaje general",
            "/enviar !b03df4cc:2 Mensaje directo",
            "/enviar aprs EB2ABC-7: Aviso APRS",
        ):
            self.assertIn(example, self.source)


if __name__ == "__main__":
    unittest.main()
