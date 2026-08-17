#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regresión estructural de la integración visual de balizas en Telegram.

Estas pruebas no arrancan Telegram ni abren conexiones de radio. Comprueban que el
launcher conserva el patrón de extensión aislada, publica las balizas en menú y
ayuda contextual, y no vuelve a registrar un segundo handler directo para el
comando histórico /parar_baliza.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "source" / "Telegram_Bot_ChannelGateway.py"
BEACONS = ROOT / "source" / "beacon_bot.py"


class BeaconBotVisibilityTest(unittest.TestCase):
    """Valida la integración visible sin depender de servicios externos."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.launcher_text = LAUNCHER.read_text(encoding="utf-8")
        cls.beacon_text = BEACONS.read_text(encoding="utf-8")

    def test_python_sources_parse(self) -> None:
        """Los dos módulos modificados/consumidos deben ser Python válido."""
        ast.parse(self.launcher_text, filename=str(LAUNCHER))
        ast.parse(self.beacon_text, filename=str(BEACONS))

    def test_menu_contains_both_transport_families(self) -> None:
        """El launcher publica comandos visibles para Meshtastic y MeshCore."""
        for command in (
            'upsert("baliza",',
            'upsert("balizas",',
            'upsert("parar_baliza",',
            'upsert("baliza_mc",',
            'upsert("balizas_mc",',
            'upsert("parar_baliza_mc",',
        ):
            self.assertIn(command, self.launcher_text)

        self.assertIn('if "meshtastic" in available:', self.launcher_text)
        self.assertIn('if "meshcore" in available:', self.launcher_text)

    def test_help_is_extended_contextually(self) -> None:
        """La ayuda visible reutiliza contextual_help() del gestor de balizas."""
        self.assertIn("contextual_help", self.launcher_text)
        self.assertIn("await original_ayuda(update, context)", self.launcher_text)
        self.assertIn("await message.reply_text(contextual_help())", self.launcher_text)

    def test_parar_baliza_keeps_weather_fallback(self) -> None:
        """/parar_baliza debe despachar y conservar el callback meteorológico."""
        self.assertIn("_replace_parar_baliza_handler(app)", self.launcher_text)
        self.assertIn("original_callback = handler.callback", self.launcher_text)
        self.assertIn("await original_callback(update, context)", self.launcher_text)

        # No debe volver a existir el registro directo que colisionaba con el
        # handler meteorológico histórico.
        self.assertNotIn(
            'CommandHandler("parar_baliza", parar_baliza_cmd)',
            self.launcher_text,
        )

    def test_meshcore_stop_has_independent_handler(self) -> None:
        """MeshCore mantiene su parada independiente por nombre."""
        self.assertIn(
            'CommandHandler("parar_baliza_mc", parar_baliza_mc_cmd)',
            self.launcher_text,
        )


if __name__ == "__main__":
    unittest.main()
