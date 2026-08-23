#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regresión del encabezado Telegram para mensajes RX MeshCore."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from meshcore_rx_header import normalize_meshcore_rx_header


SAMPLE = (
    "📩 3UTB (meshcore) (MeshCore canal mc:5 (Red-Mesh) · "
    "canal local 4* (PRUEBAS) · mc:5 (Red-Mesh)):\n"
    "47. prueba\n"
    "   • MeshCore repetidores: 3 repetidor(es), nombres no disponibles"
)


class MeshCoreRxHeaderTest(unittest.TestCase):
    """Comprueba que solo cambia la representación visual prevista."""

    def test_meshcore_a_meshtastic_b_hides_local_mapping(self) -> None:
        result = normalize_meshcore_rx_header(
            SAMPLE,
            "meshcore_a_meshtastic_embedded_b",
        )
        self.assertEqual(
            result,
            "📩 3UTB (meshcore) (MeshCore canal mc:5 (Red-Mesh)):\n"
            "47. prueba\n"
            "   • MeshCore repetidores: 3 repetidor(es), nombres no disponibles",
        )

    def test_meshcore_only_hides_local_mapping(self) -> None:
        result = normalize_meshcore_rx_header(SAMPLE, "meshcore_only")
        self.assertIn("(MeshCore canal mc:5 (Red-Mesh)):", result)
        self.assertNotIn("canal local", result)
        self.assertEqual(result.count("mc:5 (Red-Mesh)"), 1)

    def test_historical_profile_keeps_local_mapping_without_duplicate(self) -> None:
        result = normalize_meshcore_rx_header(
            SAMPLE,
            "meshtastic_a_meshcore_embedded_b",
        )
        self.assertIn(
            "(MeshCore canal mc:5 (Red-Mesh) · canal local 4* (PRUEBAS)):",
            result,
        )
        self.assertEqual(result.count("mc:5 (Red-Mesh)"), 1)

    def test_legacy_keeps_local_mapping_without_duplicate(self) -> None:
        result = normalize_meshcore_rx_header(SAMPLE, "")
        self.assertIn("canal local 4* (PRUEBAS)", result)
        self.assertEqual(result.count("mc:5 (Red-Mesh)"), 1)

    def test_meshcore_dm_is_unchanged(self) -> None:
        text = "📩 3UTB (meshcore) (MeshCore DM directo):\n47. prueba"
        self.assertEqual(
            normalize_meshcore_rx_header(text, "meshcore_a_meshtastic_embedded_b"),
            text,
        )

    def test_non_meshcore_message_is_unchanged(self) -> None:
        text = "📩 3UTB (canal 4* (PRUEBAS)):\n47. prueba"
        self.assertEqual(
            normalize_meshcore_rx_header(text, "meshcore_a_meshtastic_embedded_b"),
            text,
        )

    def test_body_is_never_rewritten(self) -> None:
        body = "47. prueba · mc:5 (Red-Mesh) · canal local texto"
        text = SAMPLE.split("\n", 1)[0] + "\n" + body
        result = normalize_meshcore_rx_header(
            text,
            "meshcore_a_meshtastic_embedded_b",
        )
        self.assertEqual(result.split("\n", 1)[1], body)

    def test_normalizer_is_idempotent(self) -> None:
        once = normalize_meshcore_rx_header(
            SAMPLE,
            "meshcore_a_meshtastic_embedded_b",
        )
        twice = normalize_meshcore_rx_header(
            once,
            "meshcore_a_meshtastic_embedded_b",
        )
        self.assertEqual(twice, once)


if __name__ == "__main__":
    unittest.main()
