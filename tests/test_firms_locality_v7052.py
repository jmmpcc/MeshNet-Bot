from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from tools.emergencias_guardia.emergencias.engine import (
    _enrich_accepted_firms_locations,
)
from tools.emergencias_guardia.emergencias.formatters import aprs_emergency_text
from tools.emergencias_guardia.emergencias.geo_admin import (
    enrich_event_municipality,
    resolve_nearest_population,
)
from tools.emergencias_guardia.emergencias.models import Event


def make_event(source: str = "nasa_firms") -> Event:
    """Crea un Event mínimo para las regresiones de localización v7.0.52."""
    return Event(
        event_id=f"{source}:TEST-7052",
        source=source,
        source_event_id="TEST-7052",
        category="wildfire",
        verification="satellite_detection" if source == "nasa_firms" else "official",
        severity="medium",
        status="active",
        title="Detección térmica satelital agrupada",
        description="Prueba",
        latitude=42.4407,
        longitude=-0.7678,
        province="Huesca",
        municipality="",
        metadata={
            "detection_count": 42,
            "frp_mw": 17.89,
            "frp_max_mw": 17.89,
            "frp_total_mw": 176.48,
            "confidence": "n",
            "confidence_label": "nominal",
            "satellite": "Suomi-NPP",
        },
    )


class FirmsLocalityV7052Tests(unittest.TestCase):
    """Protege el enriquecimiento FIRMS sin alterar fuentes ni filtros previos."""

    @patch("tools.emergencias_guardia.emergencias.engine.enrich_event_municipality")
    def test_engine_enriches_only_accepted_nasa_firms(self, enrich_mock):
        """El motor sólo llama al enriquecedor geográfico para FIRMS aceptados."""
        def enrich(event):
            event.metadata["nearest_population"] = "Bailo"
            event.metadata["nearest_population_distance_km"] = 3.2
            return True

        enrich_mock.side_effect = enrich
        firms_event = make_event("nasa_firms")
        changed = _enrich_accepted_firms_locations(
            {firms_event.event_id: firms_event},
            "nasa_firms",
        )
        self.assertEqual(changed, 1)
        self.assertEqual(firms_event.metadata["nearest_population"], "Bailo")
        self.assertEqual(firms_event.municipality, "")
        enrich_mock.assert_called_once_with(firms_event)

        enrich_mock.reset_mock()
        dgt_event = make_event("dgt_datex")
        changed = _enrich_accepted_firms_locations(
            {dgt_event.event_id: dgt_event},
            "dgt_datex",
        )
        self.assertEqual(changed, 0)
        enrich_mock.assert_not_called()
        self.assertNotIn("nearest_population", dgt_event.metadata)

    @patch("tools.emergencias_guardia.emergencias.geo_admin.urllib.request.urlopen")
    def test_resolve_nearest_population_selects_haversine_nearest(self, urlopen_mock):
        """El resolver IGN elige el núcleo realmente más cercano por Haversine."""
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "nombre": "Lejano",
                        "latitud": 42.50,
                        "longitud": -0.80,
                        "habitantes": 100,
                        "cpro": "22",
                        "codine": "22000000000",
                    },
                    "geometry": None,
                },
                {
                    "type": "Feature",
                    "properties": {
                        "nombre": "Bailo",
                        "latitud": 42.442,
                        "longitud": -0.768,
                        "habitantes": 200,
                        "cpro": "22",
                        "codine": "22044000000",
                    },
                    "geometry": None,
                },
            ],
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        response.__exit__.return_value = False
        urlopen_mock.return_value = response

        with patch.dict(os.environ, {"EMERGENCIAS_GEO_POPULATION_ENABLED": "1"}, clear=False):
            result = resolve_nearest_population(
                42.4407,
                -0.7678,
                endpoint="https://example.invalid/nuc/items",
                timeout_seconds=1.0,
                max_radius_km=30.0,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Bailo")
        self.assertLess(result["distance_km"], 1.0)

        requested_url = urlopen_mock.call_args.args[0].full_url
        self.assertIn("skipGeometry=true", requested_url)
        self.assertIn("properties=nombre%2Clatitud%2Clongitud%2Chabitantes%2Ccpro%2Ccodine", requested_url)

    @patch("tools.emergencias_guardia.emergencias.geo_admin.resolve_nearest_population")
    def test_population_failure_does_not_modify_event(self, resolver_mock):
        """Un fallo/None del IGN nunca reclasifica ni altera el evento FIRMS."""
        resolver_mock.return_value = None
        event = make_event()
        original_hash = event.raw_hash
        original_coordinates = (event.latitude, event.longitude)

        changed = enrich_event_municipality(event)

        self.assertFalse(changed)
        self.assertEqual(event.municipality, "")
        self.assertNotIn("nearest_population", event.metadata)
        self.assertEqual((event.latitude, event.longitude), original_coordinates)
        self.assertEqual(event.raw_hash, original_hash)

    @patch("tools.emergencias_guardia.emergencias.geo_admin.resolve_nearest_population")
    def test_population_enrichment_uses_metadata_not_municipality(self, resolver_mock):
        """La referencia CERCA se guarda en metadata, nunca en municipality."""
        resolver_mock.return_value = {
            "name": "Bailo",
            "distance_km": 7.42,
            "latitude": 42.509,
            "longitude": -0.812,
            "inhabitants": 200,
            "province_code": "22",
            "codine": "22044000000",
        }
        event = make_event()

        changed = enrich_event_municipality(event)

        self.assertTrue(changed)
        self.assertEqual(event.municipality, "")
        self.assertEqual(event.metadata["nearest_population"], "Bailo")
        self.assertEqual(event.metadata["nearest_population_distance_km"], 7.42)
        self.assertEqual(
            event.metadata["nearest_population_resolution_method"],
            "ign_api_features_nuc_haversine",
        )

    def test_aprs_firms_includes_nearest_population_and_coordinates(self):
        """APRS conserva coordenadas e incorpora CERCA cuando existe referencia."""
        event = make_event()
        event.metadata["nearest_population"] = "Bailo"
        event.metadata["nearest_population_distance_km"] = 7.42

        text_is = aprs_emergency_text(event, max_chars=67)
        text_rf = aprs_emergency_text(event, max_chars=160)

        self.assertLessEqual(len(text_is), 67)
        self.assertIn("42.4407,-0.7678", text_is)
        self.assertIn("CERCA Bailo", text_is)
        self.assertTrue(text_is.startswith("EMERG INCENDIO SAT"))

        self.assertIn("CERCA Bailo,Huesca 7.42km", text_rf)
        self.assertIn("DET 42", text_rf)

    def test_aprs_firms_real_population_name_uses_complete_words(self):
        """El caso real Salinas de Jaca nunca queda como 'CERCA Salinas de'."""
        event = make_event()
        event.metadata["nearest_population"] = "Salinas de Jaca"
        event.metadata["nearest_population_distance_km"] = 3.6

        text_is = aprs_emergency_text(event, max_chars=67)
        text_rf = aprs_emergency_text(event, max_chars=160)

        self.assertLessEqual(len(text_is), 67)
        self.assertIn("CERCA Salinas", text_is)
        self.assertNotIn("CERCA Salinas de", text_is)
        self.assertIn("DET 42", text_is)
        self.assertIn("CERCA Salinas de Jaca,Huesca 3.6km", text_rf)

    def test_aprs_firms_without_population_keeps_v7051_shape(self):
        """Sin referencia cercana se mantiene el formato operativo v7.0.51."""
        event = make_event()
        text = aprs_emergency_text(event, max_chars=67)
        self.assertLessEqual(len(text), 67)
        self.assertIn("42.4407,-0.7678", text)
        self.assertNotIn("CERCA", text)


if __name__ == "__main__":
    unittest.main()
