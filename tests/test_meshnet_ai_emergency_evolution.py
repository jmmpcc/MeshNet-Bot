from __future__ import annotations

import json
import unittest
from copy import deepcopy

from shared.meshnet_ai import AIConfig, AIFeatures, AIResult
from shared.meshnet_ai_emergency_evolution import (
    EmergencyAIEvolutionExplainer,
    deterministic_evolution_snapshot,
)


class FakeAI:
    """Doble mínimo de MeshNetAI para probar IA-2C sin red ni credenciales."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        emergencies: bool = True,
        response: AIResult | None = None,
    ) -> None:
        self.config = AIConfig(
            enabled=enabled,
            provider="ollama",
            model="test",
            features=AIFeatures(emergencies=emergencies),
        )
        self.response = response or AIResult(
            ok=False,
            status="degraded",
            error="sin respuesta",
        )
        self.calls = 0
        self.last_prompt = ""
        self.last_system = ""

    def feature_enabled(self, feature: str) -> bool:
        return self.config.enabled and bool(self.config.features.as_dict().get(feature, False))

    def generate_text(self, prompt: str, system: str = "") -> AIResult:
        self.calls += 1
        self.last_prompt = prompt
        self.last_system = system
        return self.response


def firms_event(phase: str = "growth", **overrides):
    """Construye un FIRMS evolucionado equivalente a Event para pruebas aisladas."""

    data = {
        "event_id": "nasa_firms:test:001",
        "source": "nasa_firms",
        "category": "wildfire",
        "severity": "high",
        "verification": "satellite_detection",
        "status": "active",
        "change": "updated",
        "title": "Aumento del foco de incendio satelital",
        "description": "Aumento de posible foco detectado por NASA FIRMS",
        "municipality": "Zaragoza",
        "province": "Zaragoza",
        "latitude": 41.65,
        "longitude": -0.88,
        "started_at": "2026-09-07T10:00:00+00:00",
        "updated_at": "2026-09-07T12:00:00+00:00",
        "metadata": {
            "firms_phase": phase,
            "growth_reasons": ["detections", "frp", "extent"] if phase == "growth" else [],
            "incident_first_detection_at": "2026-09-07T10:00:00+00:00",
            "incident_last_detection_at": "2026-09-07T12:00:00+00:00",
            "incident_passes": 2,
            "previous_detection_count": 3,
            "latest_detection_count": 5,
            "incident_peak_detection_count": 5,
            "previous_frp_total_mw": 18.4,
            "latest_frp_total_mw": 28.0,
            "incident_peak_frp_total_mw": 28.0,
            "previous_extent_km": 1.2,
            "latest_extent_km": 2.0,
            "incident_peak_extent_km": 2.0,
            "private_secret": "NO DEBE ENVIARSE",
        },
    }
    data.update(overrides)
    return data


class EmergencyAIEvolutionTests(unittest.TestCase):
    def test_snapshot_reuses_deterministic_firms_phase(self):
        snapshot = deterministic_evolution_snapshot(firms_event("growth"))
        self.assertEqual(snapshot["phase"], "growth")
        self.assertEqual(snapshot["firms_tracking"]["growth_reasons"], ["detections", "frp", "extent"])

    def test_snapshot_never_forwards_full_metadata(self):
        snapshot = deterministic_evolution_snapshot(firms_event())
        self.assertNotIn("metadata", snapshot["event"])
        self.assertNotIn("private_secret", snapshot["firms_tracking"])

    def test_non_firms_cannot_inherit_firms_phase(self):
        event = firms_event(source="aemet_cap", metadata={"firms_phase": "growth"})
        snapshot = deterministic_evolution_snapshot(event)
        self.assertEqual(snapshot["phase"], "updated")
        self.assertNotIn("firms_tracking", snapshot)

    def test_global_disabled_never_calls_provider(self):
        ai = FakeAI(enabled=False)
        result = EmergencyAIEvolutionExplainer(ai).explain(firms_event())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        self.assertEqual(ai.calls, 0)

    def test_emergencies_disabled_never_calls_provider(self):
        ai = FakeAI(emergencies=False)
        result = EmergencyAIEvolutionExplainer(ai).explain(firms_event())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "feature_disabled")
        self.assertEqual(ai.calls, 0)

    def test_unknown_phase_never_calls_provider(self):
        ai = FakeAI()
        event = firms_event(source="aemet_cap", change="", status="active", metadata={})
        result = EmergencyAIEvolutionExplainer(ai).explain(event)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_available")
        self.assertEqual(ai.calls, 0)

    def test_invalid_limit_never_calls_provider(self):
        ai = FakeAI()
        result = EmergencyAIEvolutionExplainer(ai).explain(firms_event(), max_explanation_chars=0)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertEqual(ai.calls, 0)

    def test_valid_growth_explanation_is_informational(self):
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                duration_ms=15,
                text=json.dumps({
                    "explanation": "La pasada más reciente aumenta detecciones, FRP y extensión observada respecto al máximo previo.",
                    "confidence": 0.91,
                }),
            )
        )
        result = EmergencyAIEvolutionExplainer(ai).explain(firms_event())
        self.assertTrue(result.ok)
        self.assertEqual(result.phase, "growth")
        self.assertEqual(result.confidence, 0.91)
        self.assertEqual(result.status, "available")
        self.assertEqual(ai.calls, 1)
        prompt = json.loads(ai.last_prompt)
        self.assertTrue(prompt["constraints"]["phase_is_authoritative"])
        self.assertIn("NO la cambies", ai.last_system)
        self.assertIn("no de un incendio confirmado", ai.last_system)
        self.assertIn("no debe describirse como intensidad del incendio", ai.last_system)

    def test_stable_prompt_forbids_equating_stable_with_extinguished(self):
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps({"explanation": "Sin crecimiento significativo en esta pasada.", "confidence": 0.8}),
            )
        )
        result = EmergencyAIEvolutionExplainer(ai).explain(firms_event("stable"))
        self.assertTrue(result.ok)
        self.assertEqual(result.phase, "stable")
        self.assertIn("NO incendio extinguido", ai.last_system)

    def test_real_provider_overclaims_are_rejected(self):
        """Reproduce las conclusiones no sustentadas observadas en la prueba real.

        Cómo se llama:
            La suite entrega al explicador una respuesta equivalente a la salida
            real que convirtió extensión FIRMS en área afectada y FRP en intensidad.

        Funcionalidad:
            Verifica que la barrera determinista rechace esa explicación con
            ``ok=False`` aunque el proveedor haya respondido correctamente.
        """
        explanation = (
            "La extensión observada del conjunto de detecciones ha crecido de 1.2 km a 2.0 km, "
            "indicando que el área afectada por las señales térmicas es más amplia. "
            "La potencia radiante total aumentó de 18.4 MW a 28.0 MW, evidenciando una mayor "
            "intensidad del incendio."
        )
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps({"explanation": explanation, "confidence": 0.95}),
            )
        )
        result = EmergencyAIEvolutionExplainer(ai).explain(firms_event())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertIn("no sustentada", result.error)

    def test_detection_count_only_cannot_infer_extent(self):
        """Regresión IA-2E: detecciones 2->4 no autorizan afirmar mayor extensión.

        Cómo se llama:
            Simula la salida observada con proveedor real usando un evento cuyo
            tracking solo contiene aumento del número de detecciones.

        Funcionalidad:
            Verifica que la nueva barrera determinista rechace cualquier mención de
            extensión cuando el snapshot no contiene campos/reason de extensión.
        """
        detection_only = firms_event(
            metadata={
                "firms_phase": "growth",
                "growth_reasons": ["increase_in_detection_count"],
                "previous_detection_count": 2,
                "latest_detection_count": 4,
            }
        )
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps({
                    "explanation": (
                        "Las detecciones aumentan de 2 a 4, indicando también un "
                        "aumento de la extensión del posible foco detectado."
                    ),
                    "confidence": 0.95,
                }),
            )
        )

        result = EmergencyAIEvolutionExplainer(ai).explain(detection_only)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertIn("sin evidencia determinista", result.error)

    def test_detection_count_only_allows_detection_wording_and_prompt_forbids_inference(self):
        """El mismo snapshot acepta una explicación limitada a lo observado."""
        detection_only = firms_event(
            metadata={
                "firms_phase": "growth",
                "growth_reasons": ["increase_in_detection_count"],
                "previous_detection_count": 2,
                "latest_detection_count": 4,
            }
        )
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps({
                    "explanation": (
                        "El número de detecciones satelitales aumenta de 2 a 4 "
                        "respecto a la observación anterior."
                    ),
                    "confidence": 0.9,
                }),
            )
        )

        result = EmergencyAIEvolutionExplainer(ai).explain(detection_only)

        self.assertTrue(result.ok)
        self.assertEqual(result.phase, "growth")
        self.assertIn(
            "describe únicamente el cambio del número de detecciones",
            ai.last_system,
        )
        self.assertIn("No describas ningún otro cambio cuantitativo", ai.last_system)
        prompt = json.loads(ai.last_prompt)
        self.assertEqual(
            prompt["constraints"]["firms_evidence"],
            {"detections": True, "extent": False, "frp": False},
        )
        self.assertEqual(prompt["constraints"]["output_language"], "es")
        self.assertEqual(
            prompt["constraints"]["allowed_facts"],
            ["El número de detecciones satelitales pasa de 2 a 4."],
        )
        self.assertIn("Responde SIEMPRE en español", ai.last_system)
        self.assertIn("constraints.allowed_facts", ai.last_system)

    def test_truncation_reuses_safe_word_boundary_helper(self):
        """Evita que el límite IA-2C corte la explicación a mitad de palabra.

        Cómo se llama:
            Ejecuta ``explain`` con un límite corto sobre una explicación larga.

        Funcionalidad:
            Confirma que IA-2C reutiliza ``_fit_text`` de IA-2A y que la salida
            resultante permanece dentro del presupuesto sin terminar en un fragmento.
        """
        explanation = (
            "La pasada más reciente muestra más detecciones térmicas y mayor FRP observado. "
            "La extensión del conjunto de detecciones también aumenta respecto a la pasada anterior."
        )
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps({"explanation": explanation, "confidence": 0.9}),
            )
        )
        result = EmergencyAIEvolutionExplainer(ai).explain(
            firms_event(),
            max_explanation_chars=90,
        )
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.explanation), 90)
        self.assertTrue(result.explanation.endswith("."))
        self.assertNotIn("anteri", result.explanation)

    def test_non_string_explanation_is_rejected(self):
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({"explanation": ["texto"], "confidence": 0.5})))
        result = EmergencyAIEvolutionExplainer(ai).explain(firms_event())
        self.assertFalse(result.ok)
        self.assertIn("explanation", result.error)

    def test_non_numeric_confidence_types_are_rejected(self):
        for value in ("0.7", True, False, None):
            with self.subTest(value=value):
                ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({"explanation": "Texto factual.", "confidence": value})))
                result = EmergencyAIEvolutionExplainer(ai).explain(firms_event())
                self.assertFalse(result.ok)
                self.assertIn("confidence", result.error)

    def test_non_finite_and_out_of_range_confidence_are_rejected(self):
        for value in (float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(value=value):
                text = '{"explanation":"Texto factual.","confidence":NaN}' if value != value else json.dumps({"explanation": "Texto factual.", "confidence": value})
                ai = FakeAI(response=AIResult(ok=True, status="available", text=text))
                result = EmergencyAIEvolutionExplainer(ai).explain(firms_event())
                self.assertFalse(result.ok)
                self.assertIn("confidence", result.error)

    def test_events_are_not_mutated(self):
        event = firms_event()
        original = deepcopy(event)
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({"explanation": "Evolución observada.", "confidence": 0.7})))
        EmergencyAIEvolutionExplainer(ai).explain(event)
        self.assertEqual(event, original)


if __name__ == "__main__":
    unittest.main()
