#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pruebas unitarias del resolvedor común de perfiles de radio.

Ejecución desde la raíz del proyecto::

    python -m unittest discover -s tests -v

Estas pruebas no abren conexiones de red ni modifican el entorno real del
proceso. Verifican aliases, capacidades, overrides y requisitos mínimos de los
tres perfiles soportados.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from radio_profile import (  # noqa: E402
    PROFILE_LEGACY,
    PROFILE_MESHCORE_A_MESHTASTIC_B,
    PROFILE_MESHCORE_ONLY,
    PROFILE_MESHTASTIC_A_MESHCORE_B,
    apply_radio_profile_to_environment,
    bridge_profile_matches_radio_profile,
    default_transport_for_radio_profile,
    normalize_radio_profile,
    radio_profile_enables_transport,
    resolve_radio_profile,
    validate_radio_profile_environment,
)


class RadioProfileNormalizationTests(unittest.TestCase):
    """Comprueba nombres canónicos y aliases históricos."""

    def test_empty_profile_keeps_legacy_mode(self) -> None:
        self.assertEqual(normalize_radio_profile(""), PROFILE_LEGACY)

    def test_historical_meshcore_embedded_alias(self) -> None:
        self.assertEqual(
            normalize_radio_profile("meshcore_embedded"),
            PROFILE_MESHTASTIC_A_MESHCORE_B,
        )

    def test_documented_short_aliases(self) -> None:
        self.assertEqual(
            normalize_radio_profile("meshtastic_a_meshcore_b"),
            PROFILE_MESHTASTIC_A_MESHCORE_B,
        )
        self.assertEqual(
            normalize_radio_profile("meshcore_a_meshtastic_b"),
            PROFILE_MESHCORE_A_MESHTASTIC_B,
        )


class RadioProfileCapabilityTests(unittest.TestCase):
    """Valida capacidades y overrides sin tocar os.environ."""

    def test_meshcore_only_disables_meshtastic_and_bridge(self) -> None:
        caps = resolve_radio_profile(PROFILE_MESHCORE_ONLY, env={})
        self.assertTrue(caps.valid)
        self.assertTrue(caps.meshcore_enabled)
        self.assertFalse(caps.meshtastic_enabled)
        self.assertEqual(caps.default_transport, "meshcore")
        self.assertEqual(caps.environment_overrides["BRIDGE_ENABLED"], "0")
        self.assertEqual(caps.environment_overrides["BBS_ENABLED"], "0")

    def test_historical_combined_profile_keeps_both_radios(self) -> None:
        caps = resolve_radio_profile(PROFILE_MESHTASTIC_A_MESHCORE_B, env={})
        self.assertTrue(caps.meshcore_enabled)
        self.assertTrue(caps.meshtastic_enabled)
        self.assertEqual(caps.node_a_transport, "meshtastic")
        self.assertEqual(caps.node_b_transport, "meshcore")
        self.assertEqual(caps.default_transport, "meshtastic")
        self.assertNotIn("BRIDGE_ENABLED", caps.environment_overrides)

    def test_inverted_profile_uses_meshcore_as_default(self) -> None:
        caps = resolve_radio_profile(PROFILE_MESHCORE_A_MESHTASTIC_B, env={})
        self.assertTrue(caps.meshcore_enabled)
        self.assertTrue(caps.meshtastic_enabled)
        self.assertEqual(caps.node_a_transport, "meshcore")
        self.assertEqual(caps.node_b_transport, "meshtastic")
        self.assertEqual(caps.default_transport, "meshcore")
        self.assertEqual(caps.environment_overrides["BRIDGE_ENABLED"], "0")
        self.assertEqual(default_transport_for_radio_profile("meshcore_a_meshtastic_b"), "meshcore")
        self.assertTrue(radio_profile_enables_transport(PROFILE_MESHCORE_A_MESHTASTIC_B, "meshcore"))
        self.assertTrue(radio_profile_enables_transport(PROFILE_MESHCORE_A_MESHTASTIC_B, "meshtastic"))

    def test_meshcore_only_does_not_enable_meshtastic(self) -> None:
        self.assertFalse(radio_profile_enables_transport(PROFILE_MESHCORE_ONLY, "meshtastic"))

    def test_unknown_profile_never_applies_partial_overrides(self) -> None:
        env = {"RADIO_PROFILE": "perfil_inexistente", "BRIDGE_ENABLED": "1"}
        caps = apply_radio_profile_to_environment(env=env, strict=False)
        self.assertFalse(caps.valid)
        self.assertEqual(env["BRIDGE_ENABLED"], "1")
        self.assertEqual(env["RADIO_PROFILE"], "perfil_inexistente")


class RadioProfileCompatibilityTests(unittest.TestCase):
    """Verifica que bridge_config.json no suplante un perfil explícito."""

    def test_matching_alias_is_accepted(self) -> None:
        self.assertTrue(
            bridge_profile_matches_radio_profile(
                PROFILE_MESHTASTIC_A_MESHCORE_B,
                "meshtastic_a_meshcore_b",
            )
        )

    def test_conflicting_profile_is_rejected(self) -> None:
        self.assertFalse(
            bridge_profile_matches_radio_profile(
                PROFILE_MESHCORE_ONLY,
                PROFILE_MESHCORE_A_MESHTASTIC_B,
            )
        )

    def test_off_json_profile_does_not_conflict(self) -> None:
        self.assertTrue(bridge_profile_matches_radio_profile(PROFILE_MESHCORE_ONLY, "off"))


class RadioProfileEnvironmentValidationTests(unittest.TestCase):
    """Comprueba requisitos de host para cada combinación."""

    def test_meshcore_only_requires_only_meshcore_tcp_host(self) -> None:
        env = {
            "RADIO_PROFILE": PROFILE_MESHCORE_ONLY,
            "MESHCORE_MODE": "tcp",
            "MESHCORE_TCP_HOST": "192.168.1.23",
        }
        result = validate_radio_profile_environment(env=env)
        self.assertTrue(result["ok"], result["errors"])

    def test_combined_profile_requires_meshtastic_host(self) -> None:
        env = {
            "RADIO_PROFILE": PROFILE_MESHTASTIC_A_MESHCORE_B,
            "MESHCORE_MODE": "tcp",
            "MESHCORE_TCP_HOST": "192.168.1.23",
        }
        result = validate_radio_profile_environment(env=env)
        self.assertFalse(result["ok"])
        self.assertTrue(any("host Meshtastic" in item for item in result["errors"]))

    def test_combined_profile_accepts_complete_environment(self) -> None:
        env = {
            "RADIO_PROFILE": PROFILE_MESHCORE_A_MESHTASTIC_B,
            "MESHCORE_MODE": "tcp",
            "MESHCORE_TCP_HOST": "192.168.1.23",
            "MESHTASTIC_HOST": "192.168.1.22",
        }
        result = validate_radio_profile_environment(env=env)
        self.assertTrue(result["ok"], result["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
