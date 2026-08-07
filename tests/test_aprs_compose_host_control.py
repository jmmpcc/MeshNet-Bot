from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.rpi.yml"


class AprsComposeHostControlTests(unittest.TestCase):
    """Valida sin dependencias externas el despliegue APRS del Compose RPi."""

    @classmethod
    def setUpClass(cls):
        cls.compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    def test_aprs_shares_broker_network_namespace(self):
        self.assertIn('network_mode: "service:broker"', self.compose_text)

    def test_broker_publishes_aprs_udp_only_on_host_loopback(self):
        expected = '127.0.0.1:${APRS_CTRL_PORT_HOST:-9464}:${APRS_CTRL_PORT:-9464}/udp'
        self.assertIn(expected, self.compose_text)
        self.assertNotIn('0.0.0.0:${APRS_CTRL_PORT_HOST', self.compose_text)

    def test_aprs_runs_the_local_gateway_delivered_by_the_phase(self):
        self.assertIn(
            './source/meshtastic_to_aprs.py:/app/source/meshtastic_to_aprs.py:ro',
            self.compose_text,
        )


if __name__ == "__main__":
    unittest.main()
