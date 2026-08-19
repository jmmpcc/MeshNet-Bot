from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.emergencias_guardia.emergencias.formatters import compact_messages
from tools.emergencias_guardia.emergencias.models import Event
from tools.emergencias_guardia.emergencias.notifier import _send_message


def _firms_event(*, phase: str, status: str = "active") -> Event:
    """Construye un foco FIRMS con datos suficientes para probar el formato Mesh.

    Uso:
        ``event = _firms_event(phase="growth")``.

    Parámetros:
        phase: ``initial`` o ``growth``.
        status: estado operativo; ``resolved`` se usa para validar ``FIN``.

    Funcionalidad:
        Incluye una ubicación y telemetría suficientemente extensas para forzar
        el algoritmo de compactación de 140 bytes sin depender de NASA FIRMS.
    """
    growth = phase == "growth"
    return Event(
        event_id="nasa_firms:mesh-format-test",
        source="nasa_firms",
        source_event_id="mesh-format-test",
        category="wildfire",
        status=status,
        verification="satellite_detection",
        severity="high" if growth else "medium",
        title=(
            "Aumento del foco de incendio satelital"
            if growth else "Inicio de posible foco de incendio satelital"
        ),
        description=(
            "Aumento de posible foco de incendio detectado por NASA FIRMS; "
            "5 detecciones térmicas; FRP total 27.6 MW; extensión observada 1.4 km"
            if growth else
            "Inicio de posible foco de incendio detectado por NASA FIRMS; "
            "2 detecciones térmicas; FRP total 3.62 MW; extensión observada 0.5 km"
        ),
        province="Navarra",
        latitude=42.826005,
        longitude=-0.87949,
        metadata={
            "firms_phase": phase,
            "growth_reasons": ["detections", "frp", "extent"] if growth else [],
            "detection_count": 5 if growth else 2,
            "previous_detection_count": 2,
            "latest_detection_count": 5 if growth else 2,
            "frp_total_mw": 27.6 if growth else 3.62,
            "previous_frp_total_mw": 3.62,
            "latest_frp_total_mw": 27.6 if growth else 3.62,
            "cluster_extent_km": 1.4 if growth else 0.47,
            "previous_extent_km": 0.47,
            "latest_extent_km": 1.4 if growth else 0.47,
        },
    )


class FirmsMeshEvolutionV7059Tests(unittest.TestCase):
    """Protege la semántica FIRMS enviada por MeshCore y Meshtastic."""

    def test_initial_phase_is_mandatory_with_140_byte_limit(self) -> None:
        """La alerta inicial conserva INICIO aunque el mensaje deba compactarse."""
        message = compact_messages(
            [_firms_event(phase="initial")],
            max_bytes=140,
            prefix="NUEVA · EMERG",
        )[0]
        self.assertTrue(message.startswith("INICIO · NUEVA · EMERG\n"))
        self.assertIn("MEDIA · INCENDIO", message)
        self.assertLessEqual(len(message.encode("utf-8")), 140)

    def test_growth_phase_and_metrics_survive_mesh_compaction(self) -> None:
        """AUMENTO y la evolución DET/FRP/EXT tienen prioridad en Mesh."""
        message = compact_messages(
            [_firms_event(phase="growth")],
            max_bytes=140,
            prefix="ACTUALIZACIÓN · EMERG",
        )[0]
        self.assertTrue(message.startswith("AUMENTO · ACTUALIZACIÓN · EMERG\n"))
        self.assertIn("DET 2>5", message)
        self.assertIn("FRP 3.62>27.6MW", message)
        self.assertIn("EXT 0.47>1.4km", message)
        self.assertLessEqual(len(message.encode("utf-8")), 140)

    def test_terminal_phase_has_priority_in_mesh(self) -> None:
        """Un cierre FIRMS se identifica como FIN aunque conserve metadata growth."""
        event = _firms_event(phase="growth", status="resolved")
        message = compact_messages(
            [event],
            max_bytes=140,
            prefix="FINALIZADA · EMERG",
        )[0]
        self.assertTrue(message.startswith("FIN · FINALIZADA · EMERG\n"))
        self.assertLessEqual(len(message.encode("utf-8")), 140)

    @patch("tools.emergencias_guardia.emergencias.notifier.broker_request")
    def test_same_compact_text_is_delivered_to_meshcore_and_meshtastic(self, broker) -> None:
        """Ambos transportes reciben sin reinterpretación el mismo texto FIRMS."""
        broker.return_value = {"ok": True, "sent": True}
        message = compact_messages(
            [_firms_event(phase="growth")],
            max_bytes=140,
            prefix="ACTUALIZACIÓN · EMERG",
        )[0]
        config = {"notifications": {"broker": {}}}

        _send_message(config, {"network": "meshcore", "channel": 8}, message)
        _send_message(config, {"network": "meshtastic", "channel": 8}, message)

        first = broker.call_args_list[0]
        second = broker.call_args_list[1]
        self.assertEqual(first.args[1], "MESHCORE_SEND")
        self.assertEqual(first.args[2]["text"], message)
        self.assertEqual(second.args[1], "SEND_TEXT")
        self.assertEqual(second.args[2]["text"], message)

    def test_non_firms_message_keeps_historical_header(self) -> None:
        """La mejora FIRMS no cambia el formato compacto de otras emergencias."""
        event = Event(
            event_id="ign:test",
            source="ign_earthquakes",
            source_event_id="test",
            category="earthquake",
            status="active",
            verification="official",
            severity="high",
            title="Terremoto de prueba",
            province="Navarra",
        )
        message = compact_messages([event], max_bytes=140, prefix="NUEVA · EMERG")[0]
        self.assertTrue(message.startswith("NUEVA · EMERG\nALTA · TERREMOTO\n"))
        self.assertNotIn("INICIO · NUEVA", message)


if __name__ == "__main__":
    unittest.main()
