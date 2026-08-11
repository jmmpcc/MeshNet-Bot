from __future__ import annotations

import csv
import io
import json
import os
import unittest
from unittest.mock import patch

from tools.emergencias_guardia.emergencias.emergency_dispatcher import dispatch_secondary_outputs
from tools.emergencias_guardia.emergencias.formatters import aprs_emergency_text
from tools.emergencias_guardia.emergencias.models import Event
from tools.emergencias_guardia.emergencias.sources.firms import FirmsSource


CSV_HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight\n"
)


def _csv_body(*rows: str) -> bytes:
    """Construye un CSV FIRMS mínimo para pruebas sin realizar llamadas HTTP."""
    return (CSV_HEADER + "\n".join(rows) + "\n").encode("utf-8")


def _source(**overrides: object) -> FirmsSource:
    """Crea una fuente FIRMS aislada con agrupación configurable."""
    config = {
        "dataset": "VIIRS_SNPP_NRT",
        "cluster_enabled": True,
        "cluster_radius_km": 5.0,
        "cluster_time_minutes": 90.0,
        **overrides,
    }
    return FirmsSource("nasa_firms", config, {})


def _firms_event() -> Event:
    """Evento agregado representativo para validar transportes APRS."""
    return Event(
        event_id="nasa_firms:cluster-test",
        source="nasa_firms",
        source_event_id="cluster-test",
        title="Detección térmica satelital agrupada",
        description="FIRMS: 12 detecciones térmicas agrupadas",
        category="wildfire",
        severity="high",
        status="active",
        verification="satellite_detection",
        latitude=42.423456,
        longitude=-0.754321,
        started_at="2026-08-11T01:32:00Z",
        updated_at="2026-08-11T01:32:00Z",
        metadata={
            "detection_count": 12,
            "frp_mw": 150.0,
            "frp_max_mw": 150.0,
            "frp_total_mw": 430.5,
            "confidence": "h",
            "confidence_label": "high",
            "satellite": "Suomi-NPP",
        },
    )


class FirmsGroupingAndAprsTests(unittest.TestCase):
    """Regresiones de la fase FIRMS/APRS sin modificar fuentes ajenas."""

    def test_viirs_high_confidence_and_satellite_code_are_normalized(self) -> None:
        """VIIRS ``h`` debe producir severidad alta y ``N`` debe ser Suomi-NPP."""
        body = _csv_body(
            "42.4138,-0.77205,305.96,0.54,0.51,2026-08-11,0132,N,VIIRS,h,2.0NRT,285.24,12.5,N"
        )
        events = _source(cluster_enabled=False).parse(body)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.severity, "high")
        self.assertEqual(event.metadata.get("confidence"), "h")
        self.assertEqual(event.metadata.get("confidence_label"), "high")
        self.assertEqual(event.metadata.get("satellite"), "Suomi-NPP")

    def test_nearby_same_pass_pixels_are_grouped_but_far_pixel_is_not(self) -> None:
        """Tres píxeles próximos forman un foco agrupado y uno lejano queda aparte."""
        body = _csv_body(
            "42.4138,-0.77205,305.96,0.54,0.51,2026-08-11,0132,N,VIIRS,n,2.0NRT,285.24,2.0,N",
            "42.4154,-0.72981,324.35,0.54,0.51,2026-08-11,0132,N,VIIRS,n,2.0NRT,288.69,5.5,N",
            "42.4167,-0.73008,333.34,0.54,0.51,2026-08-11,0132,N,VIIRS,h,2.0NRT,289.92,6.5,N",
            "40.4168,-3.7038,333.34,0.54,0.51,2026-08-11,0132,N,VIIRS,n,2.0NRT,289.92,10.0,N",
        )
        events = _source().parse(body)

        self.assertEqual(len(events), 2)
        grouped = max(events, key=lambda event: int(event.metadata.get("detection_count", 0)))
        self.assertEqual(grouped.metadata.get("detection_count"), 3)
        self.assertEqual(grouped.metadata.get("confidence"), "h")
        self.assertEqual(grouped.severity, "high")
        self.assertAlmostEqual(float(grouped.metadata.get("frp_max_mw")), 6.5)
        self.assertAlmostEqual(float(grouped.metadata.get("frp_total_mw")), 14.0)
        self.assertEqual(grouped.metadata.get("satellite"), "Suomi-NPP")

    def test_grouping_can_be_disabled_for_direct_diagnostics(self) -> None:
        """El modo diagnóstico conserva la equivalencia histórica fila=evento."""
        body = _csv_body(
            "42.4138,-0.77205,305.96,0.54,0.51,2026-08-11,0132,N,VIIRS,n,2.0NRT,285.24,2.0,N",
            "42.4154,-0.72981,324.35,0.54,0.51,2026-08-11,0132,N,VIIRS,n,2.0NRT,288.69,5.5,N",
        )
        events = _source(cluster_enabled=False).parse(body)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event.metadata.get("detection_count") == 1 for event in events))

    def test_firms_aprs_67_chars_keeps_coordinates(self) -> None:
        """El boletín clásico conserva tipo y coordenadas dentro de 67 caracteres."""
        text = aprs_emergency_text(_firms_event(), max_chars=67)
        self.assertLessEqual(len(text), 67)
        self.assertTrue(text.startswith("EMERG INCENDIO SAT"))
        self.assertIn("42.4235,-0.7543", text)

    def test_firms_long_rf_text_adds_telemetry_after_coordinates(self) -> None:
        """Un presupuesto RF mayor añade telemetría sin desplazar coordenadas."""
        text = aprs_emergency_text(_firms_event(), max_chars=160)
        self.assertGreater(len(text), 67)
        self.assertTrue(text.startswith("EMERG INCENDIO SAT 42.4235,-0.7543"))
        self.assertIn("DET 12", text)
        self.assertIn("FRP 150MW", text)
        self.assertIn("CONF H", text)
        self.assertIn("Suomi-NPP", text)

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher.socket.socket")
    def test_firms_rf_allows_gateway_multipart_with_existing_chunk_cap(self, socket_factory) -> None:
        """FIRMS usa resumen RF largo pero respeta EMERGENCIAS_APRS_RF_MAX_CHUNKS."""
        client = socket_factory.return_value.__enter__.return_value
        client.recvfrom.side_effect = [
            (b'{"ok": true, "preview": true, "dest": "broadcast", "parts": 2}', ("127.0.0.1", 9464)),
            (b'{"ok": true, "dest": "broadcast", "parts": 2, "sent": 2, "rf": true}', ("127.0.0.1", 9464)),
        ]
        env = {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias",
            "EMERGENCIAS_APRS_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_ENABLED": "1",
            "EMERGENCIAS_APRS_RF_MIN_LEVEL": "high",
            "EMERGENCIAS_APRS_RF_MAX_CHUNKS": "3",
            "EMERGENCIAS_APRS_RF_FIRMS_TEXT_MAX_CHARS": "160",
        }
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(_firms_event(), "mensaje Mesh")

        self.assertTrue(result["aprs_rf"]["ok"])
        self.assertEqual(result["aprs_rf"]["rf_parts"], 2)
        self.assertTrue(result["aprs_rf"]["firms_multipart"])
        self.assertGreater(len(result["aprs_rf"]["aprs_text"]), 67)
        self.assertIn("42.4235,-0.7543", result["aprs_rf"]["aprs_text"])

    @patch("tools.emergencias_guardia.emergencias.emergency_dispatcher.socket.socket")
    def test_firms_aprsis_remains_classic_and_keeps_coordinates(self, socket_factory) -> None:
        """APRS-IS no usa texto largo; conserva las coordenadas en su única línea BLN."""
        client = socket_factory.return_value.__enter__.return_value
        client.recvfrom.return_value = (
            b'{"ok": true, "sent": true, "bulletin": "BLN0EMERG"}',
            ("127.0.0.1", 9464),
        )
        env = {
            "APPS_APRS_ENABLED": "1",
            "APPS_APRS_ALLOWED_SOURCES": "emergencias",
            "EMERGENCIAS_APRS_ENABLED": "1",
            "APRSIS_PUSH_ENABLED": "1",
            "APRSIS_EMERGENCY_BULLETIN_ENABLED": "1",
            "APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL": "high",
            "EMERGENCIAS_APRS_TEXT_MAX_CHARS": "67",
        }
        with patch.dict(os.environ, env, clear=True):
            result = dispatch_secondary_outputs(_firms_event(), "mensaje Mesh")

        self.assertTrue(result["aprsis_bulletin"]["sent"])
        payload = json.loads(client.sendto.call_args.args[0].decode("utf-8"))
        self.assertEqual(payload["mode"], "aprsis_emergency_bulletin")
        self.assertLessEqual(len(payload["text"]), 67)
        self.assertTrue(payload["text"].startswith("EMERG INCENDIO SAT"))
        self.assertIn("42.4235,-0.7543", payload["text"])


if __name__ == "__main__":
    unittest.main()
