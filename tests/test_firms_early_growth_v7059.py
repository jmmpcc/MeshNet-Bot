from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.emergencias_guardia.emergencias.config import DEFAULT_CONFIG
from tools.emergencias_guardia.emergencias.formatters import compact_messages
from tools.emergencias_guardia.emergencias.sources.firms_tracking import FirmsTrackedSource


CSV_HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight\n"
)


def _csv_body(*rows: str) -> bytes:
    """Construye un CSV FIRMS mínimo sin llamadas de red."""
    return (CSV_HEADER + "\n".join(rows) + "\n").encode("utf-8")


def _source(**overrides: object) -> FirmsTrackedSource:
    """Crea la fuente v7.0.59 con los mismos umbrales usados en producción."""
    config = {
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
        **overrides,
    }
    return FirmsTrackedSource("nasa_firms", config, {})


def _row(
    lat: float,
    lon: float,
    date: str,
    hhmm: str,
    frp: float,
    confidence: str = "n",
) -> str:
    """Genera una detección VIIRS válida para las pruebas de evolución."""
    return (
        f"{lat},{lon},320,0.5,0.5,{date},{hhmm},N,VIIRS,{confidence},"
        f"2.0NRT,290,{frp},N"
    )


class FirmsEarlyGrowthV7059Tests(unittest.TestCase):
    """Regresiones de alerta temprana y evolución FIRMS sin tocar otras fuentes."""

    def test_emergency_route_keeps_zero_batch_window(self) -> None:
        """La alerta de emergencias sigue sin ventana artificial de espera."""
        incremental = DEFAULT_CONFIG["notifications"]["incremental"]
        self.assertEqual(incremental["batch_window_seconds"]["emergencias"], 0)

    @patch("tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current", return_value={})
    def test_single_detection_is_initial_alert(self, _load_current) -> None:
        """Una sola detección ya produce un foco inicial apto para difusión."""
        events = _source().parse(_csv_body(
            _row(42.4138, -0.77205, "2026-08-18", "0010", 12.0)
        ))

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.metadata["firms_phase"], "initial")
        self.assertEqual(event.metadata["detection_count"], 1)
        self.assertEqual(event.metadata["cluster_extent_km"], 0.0)
        self.assertIn("Inicio", event.title)
        self.assertEqual(event.verification, "satellite_detection")
        self.assertIn("Inicio", compact_messages([event], max_bytes=180)[0])

    def test_later_pass_keeps_event_id_and_reports_growth(self) -> None:
        """Más detecciones cercanas evolucionan el mismo foco a ``growth``."""
        source = _source()
        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={},
        ):
            initial = source.parse(_csv_body(
                _row(42.4138, -0.77205, "2026-08-18", "0010", 10.0)
            ))[0]

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={initial.event_id: initial},
        ):
            later = source.parse(_csv_body(
                _row(42.4140, -0.7719, "2026-08-18", "0610", 15.0),
                _row(42.4165, -0.7350, "2026-08-18", "0610", 16.0),
                _row(42.4170, -0.7345, "2026-08-18", "0610", 18.0, "h"),
            ))[0]

        self.assertEqual(later.event_id, initial.event_id)
        self.assertEqual(later.metadata["firms_phase"], "growth")
        self.assertIn("detections", later.metadata["growth_reasons"])
        self.assertIn("frp", later.metadata["growth_reasons"])
        self.assertIn("extent", later.metadata["growth_reasons"])
        self.assertGreater(later.metadata["cluster_extent_km"], 0.5)
        self.assertIn("Aumento", later.title)
        self.assertIn("Aumento", compact_messages([later], max_bytes=180)[0])

    def test_repeated_same_pass_updates_metadata_without_new_raw_hash(self) -> None:
        """Releer un foco sin crecimiento no genera una actualización notificable."""
        source = _source()
        body = _csv_body(
            _row(42.4138, -0.77205, "2026-08-18", "0010", 10.0)
        )
        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={},
        ):
            initial = source.parse(body)[0]

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={initial.event_id: initial},
        ):
            repeated = source.parse(body)[0]

        self.assertEqual(repeated.event_id, initial.event_id)
        self.assertEqual(repeated.metadata["firms_phase"], "stable")
        self.assertEqual(repeated.raw_hash, initial.raw_hash)
        self.assertEqual(repeated.title, initial.title)

    def test_frp_growth_alone_is_significant(self) -> None:
        """El foco aumenta aunque DET no cambie si FRP supera ambos umbrales."""
        source = _source()
        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={},
        ):
            initial = source.parse(_csv_body(
                _row(42.4138, -0.77205, "2026-08-18", "0010", 10.0)
            ))[0]

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={initial.event_id: initial},
        ):
            later = source.parse(_csv_body(
                _row(42.4140, -0.7720, "2026-08-18", "0610", 20.0)
            ))[0]

        self.assertEqual(later.metadata["firms_phase"], "growth")
        self.assertEqual(later.metadata["growth_reasons"], ["frp"])

    def test_small_frp_oscillation_does_not_trigger_growth(self) -> None:
        """Variaciones pequeñas de FRP quedan absorbidas por la deduplicación."""
        source = _source()
        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={},
        ):
            initial = source.parse(_csv_body(
                _row(42.4138, -0.77205, "2026-08-18", "0010", 20.0)
            ))[0]

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={initial.event_id: initial},
        ):
            later = source.parse(_csv_body(
                _row(42.4140, -0.7720, "2026-08-18", "0610", 22.0)
            ))[0]

        self.assertEqual(later.metadata["firms_phase"], "stable")
        self.assertEqual(later.raw_hash, initial.raw_hash)

    def test_far_detection_creates_independent_focus(self) -> None:
        """Una anomalía térmica lejana no se fusiona con un foco existente."""
        source = _source()
        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={},
        ):
            initial = source.parse(_csv_body(
                _row(42.4138, -0.77205, "2026-08-18", "0010", 10.0)
            ))[0]

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={initial.event_id: initial},
        ):
            far = source.parse(_csv_body(
                _row(42.60, -0.50, "2026-08-18", "0610", 20.0)
            ))[0]

        self.assertNotEqual(far.event_id, initial.event_id)
        self.assertEqual(far.metadata["firms_phase"], "initial")

    def test_detection_after_gap_creates_new_focus(self) -> None:
        """Una reaparición fuera de la ventana temporal inicia otro incidente."""
        source = _source(incident_max_gap_hours=12.0)
        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={},
        ):
            initial = source.parse(_csv_body(
                _row(42.4138, -0.77205, "2026-08-17", "0010", 10.0)
            ))[0]

        with patch(
            "tools.emergencias_guardia.emergencias.sources.firms_tracking.load_current",
            return_value={initial.event_id: initial},
        ):
            later = source.parse(_csv_body(
                _row(42.4140, -0.7720, "2026-08-18", "0610", 20.0)
            ))[0]

        self.assertNotEqual(later.event_id, initial.event_id)
        self.assertEqual(later.metadata["firms_phase"], "initial")


if __name__ == "__main__":
    unittest.main()
