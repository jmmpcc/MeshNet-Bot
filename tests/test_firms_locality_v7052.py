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
    resolve_municipality,
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
        """El motor sólo llama al resolver municipal para nasa_firms ya filtrado."""
        enrich_mock.side_effect = lambda event: setattr(event, "municipality", "Bailo") or True
        firms_event = make_event("nasa_firms")
        changed = _enrich_accepted_firms_locations({firms_event.event_id: firms_event}, "nasa_firms")
        self.assertEqual(changed, 1)
        self.assertEqual(firms_event.municipality, "Bailo")
        enrich_mock.assert_called_once_with(firms_event)

        enrich_mock.reset_mock()
        dgt_event = make_event("dgt_datex")
        changed = _enrich_accepted_firms_locations({dgt_event.event_id: dgt_event}, "dgt_datex")
        self.assertEqual(changed, 0)
        enrich_mock.assert_not_called()
        self.assertEqual(dgt_event.municipality, "")

    @patch("tools.emergencias_guardia.emergencias.geo_admin.urllib.request.urlopen")
    def test_resolve_municipality_uses_ign_polygon_and_nameunit(self, urlopen_mock):
        """La respuesta OGC del IGN se valida point-in-polygon antes de aceptar el nombre."""
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "nationallevelname": "Municipio",
                        "nameunit": "Bailo",
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-0.9, 42.3],
                            [-0.6, 42.3],
                            [-0.6, 42.6],
                            [-0.9, 42.6],
                            [-0.9, 42.3],
                        ]],
                    },
                }
            ],
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        response.__exit__.return_value = False
        urlopen_mock.return_value = response

        with patch.dict(os.environ, {"EMERGENCIAS_GEO_MUNICIPALITY_ENABLED": "1"}, clear=False):
            municipality = resolve_municipality(
                42.4407,
                -0.7678,
                endpoint="https://example.invalid/administrativeunit/items",
                timeout_seconds=1.0,
            )
        self.assertEqual(municipality, "Bailo")

    @patch("tools.emergencias_guardia.emergencias.geo_admin.resolve_municipality")
    def test_ign_failure_does_not_modify_event(self, resolver_mock):
        """Un fallo/None del IGN nunca rompe ni reclasifica el evento FIRMS."""
        resolver_mock.return_value = None
        event = make_event()
        original_hash = event.raw_hash
        original_coordinates = (event.latitude, event.longitude)

        changed = enrich_event_municipality(event)

        self.assertFalse(changed)
        self.assertEqual(event.municipality, "")
        self.assertEqual((event.latitude, event.longitude), original_coordinates)
        self.assertEqual(event.raw_hash, original_hash)

    def test_aprs_firms_includes_municipality_and_coordinates(self):
        """APRS-IS conserva coordenadas e incorpora municipio cuando existe."""
        event = make_event()
        event.municipality = "Bailo"
        text = aprs_emergency_text(event, max_chars=67)
        self.assertLessEqual(len(text), 67)
        self.assertIn("42.4407,-0.7678", text)
        self.assertIn("Bailo", text)
        self.assertTrue(text.startswith("EMERG INCENDIO SAT"))

    def test_aprs_firms_without_municipality_keeps_v7051_shape(self):
        """Sin localidad resuelta se mantiene el formato operativo previo."""
        event = make_event()
        text = aprs_emergency_text(event, max_chars=67)
        self.assertLessEqual(len(text), 67)
        self.assertIn("42.4407,-0.7678", text)
        self.assertNotIn("Bailo", text)


if __name__ == "__main__":
    unittest.main()
