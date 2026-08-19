from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.emergencias_guardia.emergencias.public_map import (
    build_public_payload,
    render_directory_htaccess,
    render_public_map_html,
)


class PublicEmergencyMapTests(unittest.TestCase):
    """Regresiones puras: no realizan red, FTPS ni llamadas a Google Maps."""

    def test_payload_exposes_only_active_geolocated_events(self) -> None:
        """Resueltas o incidencias sin coordenadas nunca llegan al JSON público."""
        with tempfile.TemporaryDirectory() as temporary:
            current = Path(temporary) / "current.json"
            current.write_text(json.dumps({
                "updated_at": "2026-08-19T10:00:00+00:00",
                "events": [
                    {"event_id": "active", "status": "active", "latitude": 41.6,
                     "longitude": -0.8, "title": "Incendio", "severity": "high"},
                    {"event_id": "resolved", "status": "resolved", "latitude": 41.7,
                     "longitude": -0.9, "title": "Resuelta"},
                    {"event_id": "no-coordinates", "status": "active", "title": "Sin posición"},
                ],
            }), encoding="utf-8")

            payload = build_public_payload(current)

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["event_id"], "active")
        self.assertEqual(payload["source_updated_at"], "2026-08-19T10:00:00+00:00")

    def test_revision_changes_only_when_public_content_changes(self) -> None:
        """La marca generated_at no provoca por sí sola una nueva publicación."""
        with tempfile.TemporaryDirectory() as temporary:
            current = Path(temporary) / "current.json"
            current.write_text(json.dumps({
                "updated_at": "2026-08-19T10:00:00+00:00",
                "events": [{"event_id": "one", "status": "active", "latitude": 41.6,
                            "longitude": -0.8, "title": "Incidencia"}],
            }), encoding="utf-8")
            first = build_public_payload(current)
            second = build_public_payload(current)

        self.assertEqual(first["revision"], second["revision"])

    def test_html_contains_live_refresh_and_visible_update_time(self) -> None:
        """El HTML refresca JSON y el popup consulta siempre el evento más reciente."""
        html = render_public_map_html("test-key", refresh_seconds=10)
        self.assertIn("Última actualización", html)
        self.assertIn("events.json?ts=", html)
        self.assertIn("setInterval(refresh,10000)", html)
        self.assertIn("maps.googleapis.com/maps/api/js?key=test-key", html)
        self.assertIn("m.__meshnetEvent=e", html)
        self.assertIn("popup(m.__meshnetEvent)", html)

    def test_emergency_directory_denies_listing_and_unknown_resources(self) -> None:
        htaccess = render_directory_htaccess("https://ciberforense.com.es")
        self.assertIn("Options -Indexes", htaccess)
        self.assertIn("events\\.json", htaccess)
        self.assertIn("Require all denied", htaccess)
        self.assertIn("https://ciberforense.com.es/", htaccess)


if __name__ == "__main__":
    unittest.main()
