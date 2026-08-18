from __future__ import annotations

import unittest

from tools.emergencias_guardia.emergencias.formatters import aprs_emergency_text
from tools.emergencias_guardia.emergencias.models import Event


def _event(*, phase: str, status: str = "active", severity: str = "medium") -> Event:
    """Construye un evento FIRMS mínimo para validar únicamente el formatter APRS."""
    return Event(
        event_id="nasa_firms:test-focus",
        source="nasa_firms",
        source_event_id="test-focus",
        category="wildfire",
        status=status,
        verification="satellite_detection",
        severity=severity,
        title="Foco FIRMS de prueba",
        description="Prueba controlada de evolución FIRMS",
        latitude=42.8260,
        longitude=-0.8795,
        province="Navarra",
        metadata={
            "firms_phase": phase,
            "nearest_population": "Isaba/Izaba",
            "nearest_population_distance_km": 5.29,
            "detection_count": 5 if phase == "growth" else 2,
            "previous_detection_count": 2,
            "latest_detection_count": 5 if phase == "growth" else 2,
            "frp_max_mw": 8.4 if phase == "growth" else 1.81,
            "frp_total_mw": 27.6 if phase == "growth" else 3.62,
            "previous_frp_total_mw": 3.62,
            "latest_frp_total_mw": 27.6 if phase == "growth" else 3.62,
            "previous_extent_km": 0.47,
            "latest_extent_km": 1.4 if phase == "growth" else 0.47,
            "cluster_extent_km": 1.4 if phase == "growth" else 0.47,
            "growth_reasons": ["detections", "frp", "extent"] if phase == "growth" else [],
            "confidence_label": "high" if phase == "growth" else "nominal",
            "satellite": "Suomi-NPP",
        },
    )


class FirmsAprsEvolutionV7059Tests(unittest.TestCase):
    """Protege APRS-IS/RF y compatibilidad de eventos FIRMS anteriores."""

    def test_initial_aprsis_uses_inicio_and_stays_within_67(self) -> None:
        text = aprs_emergency_text(_event(phase="initial"), max_chars=67)
        self.assertTrue(text.startswith("INICIO INCENDIO SAT 42.8260,-0.8795"))
        self.assertLessEqual(len(text), 67)

    def test_growth_aprsis_uses_aumento_and_reports_growth(self) -> None:
        text = aprs_emergency_text(_event(phase="growth"), max_chars=67)
        self.assertTrue(text.startswith("AUMENTO INCENDIO SAT 42.8260,-0.8795"))
        self.assertIn("DET 2>5", text)
        self.assertLessEqual(len(text), 67)

    def test_growth_rf_reports_previous_and_current_metrics(self) -> None:
        text = aprs_emergency_text(_event(phase="growth"), max_chars=160)
        self.assertTrue(text.startswith("AUMENTO INCENDIO SAT 42.8260,-0.8795"))
        self.assertIn("DET 2>5", text)
        self.assertIn("FRP 3.62>27.6MW", text)
        self.assertIn("EXT 0.47>1.4km", text)
        self.assertLessEqual(len(text), 160)

    def test_terminal_status_has_priority_over_phase(self) -> None:
        text = aprs_emergency_text(_event(phase="growth", status="resolved"), max_chars=67)
        self.assertTrue(text.startswith("FIN INCENDIO SAT"))

    def test_legacy_firms_without_phase_keeps_historical_prefix(self) -> None:
        event = _event(phase="")
        text = aprs_emergency_text(event, max_chars=67)
        self.assertTrue(text.startswith("EMERG INCENDIO SAT"))

    def test_legacy_critical_without_phase_keeps_critical_prefix(self) -> None:
        event = _event(phase="", severity="critical")
        text = aprs_emergency_text(event, max_chars=67)
        self.assertTrue(text.startswith("CRIT INCENDIO SAT"))


if __name__ == "__main__":
    unittest.main()
