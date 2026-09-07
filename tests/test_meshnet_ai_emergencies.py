from __future__ import annotations

import json
import unittest
from copy import deepcopy
from types import SimpleNamespace

from shared.meshnet_ai import AIConfig, AIFeatures, AIResult
from shared.meshnet_ai_emergencies import EmergencyAIObserver, deterministic_phase


class FakeAI:
    """Doble mínimo de MeshNetAI para comprobar IA-2A sin red ni credenciales.

    Cómo se usa:
        ``FakeAI(enabled=True, emergencies=True, response=AIResult(...))``.

    Funcionalidad:
        Expone la misma superficie utilizada por ``EmergencyAIObserver`` y cuenta
        llamadas para demostrar que los flags y validaciones bloquean el proveedor.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        emergencies: bool,
        response: AIResult | None = None,
    ) -> None:
        self.config = AIConfig(
            enabled=enabled,
            provider="ollama",
            model="test",
            features=AIFeatures(emergencies=emergencies),
        )
        self.response = response or AIResult(ok=False, status="degraded", error="sin respuesta")
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


def sample_event(**overrides):
    """Construye un evento equivalente al modelo real sin importar Emergencias."""
    data = {
        "event_id": "nasa_firms:focus-1",
        "source": "nasa_firms",
        "category": "wildfire",
        "severity": "high",
        "verification": "satellite_detection",
        "status": "active",
        "title": "Aumento del foco de incendio satelital",
        "description": "Aumento de posible foco detectado por NASA FIRMS",
        "road": "",
        "municipality": "Zaragoza",
        "province": "Zaragoza",
        "started_at": "2026-09-07T08:00:00+00:00",
        "updated_at": "2026-09-07T09:00:00+00:00",
        "metadata": {"firms_phase": "growth", "frp_total_mw": 25.0},
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class EmergencyAIObserverTests(unittest.TestCase):
    def test_global_disabled_never_calls_provider(self):
        ai = FakeAI(enabled=False, emergencies=True)
        result = EmergencyAIObserver(ai).analyze_event(sample_event(), change="updated")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.phase, "growth")
        self.assertEqual(ai.calls, 0)

    def test_emergencies_feature_disabled_never_calls_provider(self):
        ai = FakeAI(enabled=True, emergencies=False)
        result = EmergencyAIObserver(ai).analyze_event(sample_event(), change="updated")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "feature_disabled")
        self.assertEqual(ai.calls, 0)

    def test_non_positive_limits_never_call_provider(self):
        ai = FakeAI(enabled=True, emergencies=True)
        result = EmergencyAIObserver(ai).analyze_event(
            sample_event(), max_summary_chars=0
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertEqual(ai.calls, 0)

    def test_valid_json_returns_shadow_analysis(self):
        response = AIResult(
            ok=True,
            status="available",
            duration_ms=17,
            text=json.dumps(
                {
                    "summary": "Foco satelital en evolución en Zaragoza.",
                    "notes": "Detección satelital; no equivale a confirmación oficial.",
                    "confidence": 0.82,
                }
            ),
        )
        ai = FakeAI(enabled=True, emergencies=True, response=response)
        result = EmergencyAIObserver(ai).analyze_event(sample_event(), change="updated")

        self.assertTrue(result.ok)
        self.assertEqual(result.phase, "growth")
        self.assertEqual(result.status, "available")
        self.assertEqual(result.confidence, 0.82)
        self.assertEqual(ai.calls, 1)
        payload = json.loads(ai.last_prompt)
        self.assertEqual(payload["event"]["phase"], "growth")
        self.assertNotIn("metadata", payload["event"])

    def test_summary_and_notes_respect_exact_limits(self):
        response = AIResult(
            ok=True,
            status="available",
            text=json.dumps(
                {
                    "summary": "Incendio satelital observado en Zaragoza",
                    "notes": "Información auxiliar no operativa",
                    "confidence": 2.5,
                }
            ),
        )
        ai = FakeAI(enabled=True, emergencies=True, response=response)
        result = EmergencyAIObserver(ai).analyze_event(
            sample_event(), max_summary_chars=7, max_notes_chars=10
        )

        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.summary), 7)
        self.assertLessEqual(len(result.notes), 10)
        self.assertEqual(result.confidence, 1.0)

    def test_invalid_json_is_rejected(self):
        ai = FakeAI(
            enabled=True,
            emergencies=True,
            response=AIResult(ok=True, status="available", text="no-json"),
        )
        result = EmergencyAIObserver(ai).analyze_event(sample_event())

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertIn("JSON", result.error)

    def test_empty_summary_is_rejected(self):
        ai = FakeAI(
            enabled=True,
            emergencies=True,
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps({"summary": "", "notes": "x", "confidence": 0.5}),
            ),
        )
        result = EmergencyAIObserver(ai).analyze_event(sample_event())

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertEqual(ai.calls, 1)

    def test_provider_failure_is_propagated_as_safe_fallback(self):
        ai = FakeAI(
            enabled=True,
            emergencies=True,
            response=AIResult(
                ok=False,
                status="degraded",
                error="proveedor IA no accesible",
                duration_ms=25,
            ),
        )
        result = EmergencyAIObserver(ai).analyze_event(sample_event())

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.duration_ms, 25)

    def test_deterministic_phase_terminal_has_priority(self):
        event = sample_event(status="resolved", metadata={"firms_phase": "growth"})
        self.assertEqual(deterministic_phase(event, "updated"), "resolved")

    def test_deterministic_phase_reuses_firms_tracking(self):
        self.assertEqual(deterministic_phase(sample_event(), "updated"), "growth")
        self.assertEqual(
            deterministic_phase(sample_event(metadata={"firms_phase": "stable"}), "updated"),
            "stable",
        )

    def test_non_firms_change_is_preserved_without_ai_decision(self):
        event = sample_event(source="aemet", metadata={}, category="storm")
        self.assertEqual(deterministic_phase(event, "new"), "new")
        self.assertEqual(deterministic_phase(event, "updated"), "updated")

    def test_event_is_not_mutated(self):
        event = {
            "event_id": "aemet:1",
            "source": "aemet",
            "category": "storm",
            "severity": "high",
            "verification": "official",
            "status": "active",
            "title": "Aviso meteorológico",
            "description": "Viento fuerte",
            "metadata": {"private_aux": {"x": 1}},
        }
        original = deepcopy(event)
        ai = FakeAI(
            enabled=True,
            emergencies=True,
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps({"summary": "Aviso por viento.", "notes": "", "confidence": 0.9}),
            ),
        )

        EmergencyAIObserver(ai).analyze_event(event, change="new")
        self.assertEqual(event, original)


if __name__ == "__main__":
    unittest.main()
