from __future__ import annotations

import json
import unittest
from copy import deepcopy

from shared.meshnet_ai import AIConfig, AIFeatures, AIResult
from shared.meshnet_ai_emergency_correlation import (
    EmergencyAICorrelator,
    correlation_candidate,
)


class FakeAI:
    """Doble mínimo de MeshNetAI para probar IA-2B sin red ni credenciales."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        emergencies: bool = True,
        correlation: bool = True,
        response: AIResult | None = None,
    ) -> None:
        self.config = AIConfig(
            enabled=enabled,
            provider="ollama",
            model="test",
            features=AIFeatures(
                emergencies=emergencies,
                correlation=correlation,
            ),
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


def event(source: str, event_id: str, **overrides):
    """Construye un mapping equivalente a Event para pruebas aisladas."""

    data = {
        "event_id": event_id,
        "source": source,
        "source_event_id": event_id,
        "category": "wildfire",
        "severity": "high",
        "verification": "official",
        "status": "active",
        "title": "Evento de prueba",
        "description": "Descripción factual",
        "municipality": "Zaragoza",
        "province": "Zaragoza",
        "latitude": 41.6500,
        "longitude": -0.8800,
        "started_at": "2026-09-07T10:00:00+00:00",
        "updated_at": "2026-09-07T10:30:00+00:00",
        "metadata": {"private": "no-enviar"},
    }
    data.update(overrides)
    return data


class EmergencyAICorrelationTests(unittest.TestCase):
    def test_near_cross_source_pair_is_candidate(self):
        candidate = correlation_candidate(
            event("nasa_firms", "firms:1"),
            event(
                "aemet_cap",
                "aemet:1",
                category="strong_wind",
                latitude=41.67,
                longitude=-0.89,
                updated_at="2026-09-07T11:00:00+00:00",
            ),
        )

        self.assertTrue(candidate.eligible)
        self.assertIsNotNone(candidate.distance_km)
        self.assertLess(candidate.distance_km, 50.0)
        self.assertEqual(candidate.time_delta_minutes, 30.0)
        self.assertIn("different_sources", candidate.reasons)
        self.assertIn("distance_within_limit", candidate.reasons)
        self.assertIn("time_within_limit", candidate.reasons)

    def test_same_source_pair_is_not_candidate(self):
        candidate = correlation_candidate(
            event("aemet_cap", "aemet:1"),
            event("aemet_cap", "aemet:2"),
        )
        self.assertFalse(candidate.eligible)
        self.assertEqual(candidate.reasons, ("same_source",))

    def test_terminal_event_is_not_candidate(self):
        candidate = correlation_candidate(
            event("nasa_firms", "firms:1", status="resolved"),
            event("aemet_cap", "aemet:1"),
        )
        self.assertFalse(candidate.eligible)
        self.assertEqual(candidate.reasons, ("terminal_event",))

    def test_distance_outside_limit_is_not_candidate(self):
        candidate = correlation_candidate(
            event("nasa_firms", "firms:1"),
            event("aemet_cap", "aemet:1", latitude=43.26, longitude=-2.94),
            max_distance_km=50,
        )
        self.assertFalse(candidate.eligible)
        self.assertEqual(candidate.reasons, ("distance_exceeded",))
        self.assertGreater(candidate.distance_km or 0.0, 50.0)

    def test_time_outside_limit_is_not_candidate(self):
        candidate = correlation_candidate(
            event("nasa_firms", "firms:1"),
            event("datex2", "dgt:1", updated_at="2026-09-10T10:30:00+00:00"),
            max_time_hours=24,
        )
        self.assertFalse(candidate.eligible)
        self.assertEqual(candidate.reasons, ("time_exceeded",))

    def test_missing_coordinates_can_use_same_municipality(self):
        candidate = correlation_candidate(
            event("che", "che:1", latitude=None, longitude=None, category="flood"),
            event("datex2", "dgt:1", latitude=None, longitude=None, category="road_closed"),
        )
        self.assertTrue(candidate.eligible)
        self.assertIsNone(candidate.distance_km)
        self.assertIn("same_municipality", candidate.reasons)

    def test_missing_geo_match_rejects_candidate(self):
        candidate = correlation_candidate(
            event(
                "che",
                "che:1",
                latitude=None,
                longitude=None,
                municipality="Zaragoza",
                province="Zaragoza",
            ),
            event(
                "datex2",
                "dgt:1",
                latitude=None,
                longitude=None,
                municipality="Huesca",
                province="Huesca",
            ),
        )
        self.assertFalse(candidate.eligible)
        self.assertEqual(candidate.reasons, ("insufficient_geo_match",))

    def test_global_disabled_never_calls_provider(self):
        ai = FakeAI(enabled=False)
        result = EmergencyAICorrelator(ai).correlate(
            event("nasa_firms", "firms:1"), event("aemet_cap", "aemet:1")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        self.assertEqual(ai.calls, 0)

    def test_correlation_feature_disabled_never_calls_provider(self):
        ai = FakeAI(correlation=False)
        result = EmergencyAICorrelator(ai).correlate(
            event("nasa_firms", "firms:1"), event("aemet_cap", "aemet:1")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "feature_disabled")
        self.assertEqual(ai.calls, 0)

    def test_non_candidate_never_calls_provider(self):
        ai = FakeAI()
        result = EmergencyAICorrelator(ai).correlate(
            event("nasa_firms", "firms:1"),
            event("aemet_cap", "aemet:1", latitude=43.26, longitude=-2.94),
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.candidate)
        self.assertEqual(result.status, "not_candidate")
        self.assertEqual(ai.calls, 0)

    def test_valid_response_returns_informational_correlation(self):
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                duration_ms=12,
                text=json.dumps(
                    {
                        "relation": "contextual",
                        "explanation": "El aviso meteorológico aporta contexto próximo al foco satelital.",
                        "confidence": 0.84,
                    }
                ),
            )
        )
        first = event("nasa_firms", "firms:1")
        second = event("aemet_cap", "aemet:1", category="strong_wind")
        result = EmergencyAICorrelator(ai).correlate(first, second)

        self.assertTrue(result.ok)
        self.assertTrue(result.candidate)
        self.assertEqual(result.relation, "contextual")
        self.assertEqual(result.confidence, 0.84)
        self.assertEqual(result.status, "available")
        self.assertEqual(ai.calls, 1)

        prompt = json.loads(ai.last_prompt)
        self.assertNotIn("metadata", prompt["event_a"])
        self.assertNotIn("metadata", prompt["event_b"])
        self.assertTrue(prompt["constraints"]["informational_only"])
        self.assertIn("NO prueba", ai.last_system)

    def test_invalid_relation_is_rejected(self):
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps(
                    {
                        "relation": "merge_now",
                        "explanation": "Texto",
                        "confidence": 0.9,
                    }
                ),
            )
        )
        result = EmergencyAICorrelator(ai).correlate(
            event("nasa_firms", "firms:1"), event("aemet_cap", "aemet:1")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertIn("relation", result.error)

    def test_non_string_explanation_is_rejected(self):
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps(
                    {
                        "relation": "uncertain",
                        "explanation": ["texto"],
                        "confidence": 0.5,
                    }
                ),
            )
        )
        result = EmergencyAICorrelator(ai).correlate(
            event("nasa_firms", "firms:1"), event("aemet_cap", "aemet:1")
        )
        self.assertFalse(result.ok)
        self.assertIn("explanation", result.error)

    def test_non_finite_confidence_is_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                ai = FakeAI(
                    response=AIResult(
                        ok=True,
                        status="available",
                        text=json.dumps(
                            {
                                "relation": "uncertain",
                                "explanation": "Datos insuficientes.",
                                "confidence": value,
                            }
                        ),
                    )
                )
                result = EmergencyAICorrelator(ai).correlate(
                    event("nasa_firms", "firms:1"),
                    event("aemet_cap", "aemet:1"),
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "error")

    def test_events_are_not_mutated(self):
        first = event("nasa_firms", "firms:1")
        second = event("aemet_cap", "aemet:1")
        original_first = deepcopy(first)
        original_second = deepcopy(second)
        ai = FakeAI(
            response=AIResult(
                ok=True,
                status="available",
                text=json.dumps(
                    {
                        "relation": "uncertain",
                        "explanation": "Sin evidencia suficiente para afirmar identidad.",
                        "confidence": 0.4,
                    }
                ),
            )
        )

        EmergencyAICorrelator(ai).correlate(first, second)
        self.assertEqual(first, original_first)
        self.assertEqual(second, original_second)


if __name__ == "__main__":
    unittest.main()
