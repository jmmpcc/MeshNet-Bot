#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pruebas de configuración de la caché de la baliza meteorológica."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from weather_beacon import _weather_cache_seconds  # noqa: E402


class WeatherBeaconCacheTests(unittest.TestCase):
    """Comprueba el valor predeterminado y su ajuste mediante el entorno."""

    def test_default_cache_is_two_hours(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_weather_cache_seconds(), 120 * 60)

    def test_cache_seconds_can_be_configured(self) -> None:
        with patch.dict(os.environ, {"WEATHER_BEACON_CACHE_SEC": "900"}, clear=True):
            self.assertEqual(_weather_cache_seconds(), 900)

    def test_negative_cache_is_disabled_safely(self) -> None:
        with patch.dict(os.environ, {"WEATHER_BEACON_CACHE_SEC": "-1"}, clear=True):
            self.assertEqual(_weather_cache_seconds(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
