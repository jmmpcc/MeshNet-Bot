from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.emergencias_guardia.emergencias.sources import SOURCE_TYPES
from tools.emergencias_guardia.emergencias.sources.firms_tracking_presentation import (
    FirmsTrackedPresentationSource,
)


CSV_HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight\n"
)


def _csv_body(*rows: str) -> bytes:
    """Construye un CSV FIRMS mínimo sin realizar ninguna llamada de red."""
    return (CSV_HEADER + "\n".join(rows) + "\n").encode("utf-8")


def _row(lat: float, lon: float, date: str, hhmm: str, frp: float) -> str:
    """Genera una fila VIIRS válida para las pruebas de presentación."""
    return (
        f"{lat},{lon},320,0.5,0.5,{date},{hhmm},N,VIIRS,n,"
        f"2.0NRT,290,{frp},N"
    )


def _source() -> FirmsTrackedPresentationSource:
    """Crea la fuente operativa con los umbrales FIRMS actuales."""
    return FirmsTrackedPresentationSource(
        "nasa_firms",
        {
            "dataset": "VIIRS_SNPP_NRT",
            "cluster_enabled": True,
            "cluster_radius_km": 5.0,
            "cluster_time_minutes": 90.0,
            "incident_tracking_enabled": True,
            "incident_radius_km": 8.0,
            "incident_max_gap_hours": 24.0,
            "growth_frp_ratio": 0.25,
            "growth_frp_min_mw": 5.0,
            "growth_extent_ratio": 0.20,
            "growth_extent_min_km": 0.5,
        },
        {},
    )


class FirmsControlPanelPresentationTests(unittest.TestCase):
    """Regresiones de presentación sin alterar deduplicación ni evolución."""

    def test_operational_firms_type_uses_presentation_layer(self) -> None:
        """El flujo real debe usar la capa que corrige el Control Panel."""
        self.assertIs(SOURCE_TYPES["firms"], FirmsTrackedPresentationSource)

    @patch(
        "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
        return_value={},
    )
    def test_new_focus_keeps_initial_title_and_correct_plural(self, _load_current) -> None:
        """Un foco nuevo mantiene INICIO y nunca produce ``detecciónes``."""
        event = _source().parse(
            _csv_body(
                _row(42.8260, -0.8795, "2026-08-19", "0610", 2.0),
                _row(42.8290, -0.8790, "2026-08-19", "0610", 2.0),
            )
        )[0]

        self.assertEqual(event.metadata["firms_phase"], "initial")
        self.assertEqual(event.title, "Inicio de posible foco de incendio satelital")
        self.assertIn("2 detecciones térmicas", event.description)
        self.assertNotIn("detecciónes", event.description)

    def test_legacy_stable_focus_migrates_visual_text_without_raw_hash_change(self) -> None:
        """Un foco pre-v7.0.59 cambia de presentación pero no se retransmite."""
        source = _source()
        body = _csv_body(_row(42.8260, -0.8795, "2026-08-19", "0610", 3.62))

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={},
        ):
            current_shape = source.parse(body)[0]

        # Simula exactamente un evento almacenado antes de v7.0.59: conserva
        # identidad/coordenadas/telemetría, pero carece de firms_phase y usa texto legacy.
        current_shape.metadata.pop("firms_phase", None)
        current_shape.title = "Anomalía térmica NASA FIRMS"
        current_shape.description = "Detección térmica NASA FIRMS"
        current_shape.raw_hash = "legacy-raw-hash"
        previous_event_id = current_shape.event_id

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={previous_event_id: current_shape},
        ):
            evolved = source.parse(body)[0]

        self.assertEqual(evolved.event_id, previous_event_id)
        self.assertEqual(evolved.metadata["firms_phase"], "stable")
        self.assertTrue(evolved.metadata["presentation_migrated_from_legacy"])
        self.assertEqual(evolved.title, "Foco de incendio satelital en seguimiento")
        self.assertIn("Seguimiento de posible foco", evolved.description)
        self.assertEqual(evolved.raw_hash, "legacy-raw-hash")

    def test_growth_still_reports_aumento(self) -> None:
        """La capa visual no modifica la lógica de crecimiento ya validada."""
        source = _source()
        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={},
        ):
            initial = source.parse(
                _csv_body(_row(42.8260, -0.8795, "2026-08-19", "0010", 10.0))
            )[0]

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={initial.event_id: initial},
        ):
            growth = source.parse(
                _csv_body(
                    _row(42.8260, -0.8795, "2026-08-19", "0610", 15.0),
                    _row(42.8290, -0.8500, "2026-08-19", "0610", 16.0),
                )
            )[0]

        self.assertEqual(growth.metadata["firms_phase"], "growth")
        self.assertEqual(growth.title, "Aumento del foco de incendio satelital")
        self.assertNotIn("detecciónes", growth.description)


if __name__ == "__main__":
    unittest.main()
