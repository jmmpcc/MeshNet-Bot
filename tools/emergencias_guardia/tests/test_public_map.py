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
    """Regresiones puras del mapa público; no realizan red ni conexiones FTPS."""

    def test_payload_exposes_only_active_geolocated_events(self) -> None:
        """Resueltas o incidencias sin coordenadas nunca llegan al JSON público."""
        with tempfile.TemporaryDirectory() as temporary:
            current = Path(temporary) / "current.json"
            current.write_text(
                json.dumps({
                    "updated_at": "2026-08-19T10:00:00+00:00",
                    "events": [
                        {
                            "event_id": "active",
                            "status": "active",
                            "latitude": 41.6,
                            "longitude": -0.8,
                            "title": "Incendio",
                            "severity": "high",
                            "road": "A-2",
                            "kilometre": 314,
                        },
                        {
                            "event_id": "resolved",
                            "status": "resolved",
                            "latitude": 41.7,
                            "longitude": -0.9,
                            "title": "Resuelta",
                        },
                        {
                            "event_id": "no-coordinates",
                            "status": "active",
                            "title": "Sin posición",
                        },
                    ],
                }),
                encoding="utf-8",
            )

            payload = build_public_payload(current)

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["event_id"], "active")
        self.assertEqual(payload["events"][0]["road"], "A-2")
        self.assertEqual(payload["events"][0]["kilometre"], 314)
        self.assertEqual(payload["source_updated_at"], "2026-08-19T10:00:00+00:00")

    def test_revision_changes_only_when_public_content_changes(self) -> None:
        """La marca generated_at no provoca por sí sola una nueva publicación."""
        with tempfile.TemporaryDirectory() as temporary:
            current = Path(temporary) / "current.json"
            current.write_text(
                json.dumps({
                    "updated_at": "2026-08-19T10:00:00+00:00",
                    "events": [{
                        "event_id": "one",
                        "status": "active",
                        "latitude": 41.6,
                        "longitude": -0.8,
                        "title": "Incidencia",
                    }],
                }),
                encoding="utf-8",
            )
            first = build_public_payload(current)
            second = build_public_payload(current)

        self.assertEqual(first["revision"], second["revision"])

    def test_html_uses_maplibre_openfreemap_and_live_refresh(self) -> None:
        """El visor gratuito no contiene Google y refresca siempre events.json."""
        html = render_public_map_html(refresh_seconds=10)

        self.assertIn("Última actualización", html)
        self.assertIn("events.json?ts=", html)
        self.assertIn("setInterval(refresh,10000)", html)
        self.assertIn("maplibre-gl@5.6.0", html)
        self.assertIn("https://tiles.openfreemap.org/styles/liberty", html)
        self.assertIn("OpenStreetMap contributors", html)
        self.assertIn("entry.event = event", html)
        self.assertNotIn("maps.googleapis.com", html)
        self.assertNotIn("google.maps", html)
        self.assertNotIn("api_key", html.casefold())

    def test_html_has_same_operational_filters_as_control_panel(self) -> None:
        """Periodo, provincia, severidad y tipo se aplican localmente al JSON público."""
        html = render_public_map_html()

        self.assertIn('id="filter-period"', html)
        self.assertIn('value="24" selected', html)
        self.assertIn('value="48"', html)
        self.assertIn('value="72"', html)
        self.assertIn('value="168"', html)
        self.assertIn('id="filter-province"', html)
        self.assertIn('id="filter-severity"', html)
        self.assertIn('id="filter-category"', html)
        self.assertIn("function filteredEvents()", html)
        self.assertIn("function eventReferenceDate(event)", html)

    def test_html_counts_categories_and_uses_category_colour(self) -> None:
        """Los recuentos son pulsables y el color identifica el tipo de incidencia."""
        html = render_public_map_html()

        self.assertIn('id="summary"', html)
        self.assertIn("CATEGORY_PRESENTATION", html)
        self.assertIn("Incendio forestal", html)
        self.assertIn("Corte de carretera", html)
        self.assertIn("Inundación", html)
        self.assertIn("Terremoto", html)
        self.assertIn("renderCategorySummary", html)
        self.assertIn("category-chip", html)
        self.assertIn("markerAppearance(event)", html)
        self.assertIn("border:4", html)

    def test_html_has_hover_summary_and_pinned_complete_detail(self) -> None:
        """Hover resume y click/tap mantiene una ficha completa con los datos públicos."""
        html = render_public_map_html()

        self.assertIn("mouseenter", html)
        self.assertIn("mouseleave", html)
        self.assertIn("quickPopupHtml", html)
        self.assertIn("fullPopupHtml", html)
        self.assertIn("Verificación:", html)
        self.assertIn("Carretera:", html)
        self.assertIn("Coordenadas:", html)
        self.assertIn("Primera detección:", html)
        self.assertIn("Última detección:", html)
        self.assertIn("Fuente oficial:", html)
        self.assertIn("closeOnClick:false", html)

    def test_html_marks_recent_incidents_from_first_seen_without_changing_data_contract(self) -> None:
        """El pulso es puramente visual y usa first_seen, no updated_at, para definir un alta reciente."""
        html = render_public_map_html()

        self.assertIn("RECENT_PULSE_MINUTES = 30", html)
        self.assertIn("RECENT_HALO_MINUTES = 120", html)
        self.assertIn("function eventFirstSeenAgeMinutes(event)", html)
        self.assertIn("function recentState(event)", html)
        self.assertIn("event.first_seen", html)
        self.assertIn("recent-pulse", html)
        self.assertIn("recent-halo", html)
        self.assertIn("Incidencia reciente", html)
        self.assertIn("Pulso = detectada hace menos de 30 min", html)
        self.assertIn("prefers-reduced-motion:reduce", html)

    def test_html_refreshes_recent_marker_state_even_when_revision_is_unchanged(self) -> None:
        """Una incidencia deja de pulsar por edad sin esperar a que cambie events.json."""
        html = render_public_map_html()

        self.assertIn("function refreshMarkerRecency()", html)
        self.assertIn("if (data.revision === revision)", html)
        self.assertIn("refreshMarkerRecency();", html)
        # La recencia debe actualizar exclusivamente el hijo visual de MeshNet.
        # El elemento exterior pertenece a MapLibre y no debe recibir cambios de
        # apariencia, clases o transformaciones durante los refrescos temporales.
        self.assertIn("applyMarkerAppearance(entry.visual,entry.event)", html)
        self.assertNotIn("applyMarkerAppearance(entry.element,entry.event)", html)
        self.assertIn("meshnet-marker-host", html)
        self.assertIn("meshnet-marker-visual", html)
        self.assertIn("visual:markerParts.visual", html)

    def test_emergency_directory_denies_listing_and_unknown_resources(self) -> None:
        """El directorio público mantiene una superficie mínima y controlada."""
        htaccess = render_directory_htaccess("https://ciberforense.com.es")

        self.assertIn("Options -Indexes", htaccess)
        self.assertIn("events\\.json", htaccess)
        self.assertIn("Require all denied", htaccess)
        self.assertIn("https://ciberforense.com.es/", htaccess)
        self.assertIn("X-Content-Type-Options", htaccess)


if __name__ == "__main__":
    unittest.main()
