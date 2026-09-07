from __future__ import annotations

import json
import unittest

from shared.meshnet_ai import AIConfig, AIFeatures, AIResult
from shared.meshnet_ai_emergency_correlation import EmergencyAICorrelator


class FakeAI:
    """Proveedor falso mínimo para validar el contrato estricto de IA-2B.

    Cómo se llama:
        Se instancia desde cada prueba con una respuesta JSON controlada.

    Funcionalidad:
        Simula exclusivamente ``MeshNetAI`` en el punto necesario para que
        ``EmergencyAICorrelator`` valide el tipo JSON de ``confidence`` sin red,
        credenciales ni efectos laterales.
    """

    def __init__(self, confidence) -> None:
        self.config = AIConfig(
            enabled=True,
            provider="ollama",
            model="test",
            features=AIFeatures(emergencies=True, correlation=True),
        )
        self.confidence = confidence
        self.calls = 0

    def feature_enabled(self, feature: str) -> bool:
        return self.config.enabled and bool(self.config.features.as_dict().get(feature, False))

    def generate_text(self, prompt: str, system: str = "") -> AIResult:
        self.calls += 1
        return AIResult(
            ok=True,
            status="available",
            text=json.dumps(
                {
                    "relation": "contextual",
                    "explanation": "Contexto meteorológico próximo al evento observado.",
                    "confidence": self.confidence,
                }
            ),
        )


def event(source: str, event_id: str) -> dict:
    """Construye un evento mínimo válido y candidato para IA-2B."""

    return {
        "event_id": event_id,
        "source": source,
        "category": "wildfire" if source == "nasa_firms" else "strong_wind",
        "severity": "high",
        "verification": "official",
        "status": "active",
        "title": "Evento de prueba",
        "description": "Descripción factual",
        "municipality": "Zaragoza",
        "province": "Zaragoza",
        "latitude": 41.6500 if source == "nasa_firms" else 41.6700,
        "longitude": -0.8800 if source == "nasa_firms" else -0.8900,
        "updated_at": "2026-09-07T10:30:00+00:00",
    }


class EmergencyAICorrelationConfidenceContractTests(unittest.TestCase):
    """Regresiones del P2 de Codex sobre ``confidence`` numérica estricta."""

    def correlate(self, confidence):
        ai = FakeAI(confidence)
        result = EmergencyAICorrelator(ai).correlate(
            event("nasa_firms", "firms:contract"),
            event("aemet_cap", "aemet:contract"),
        )
        self.assertEqual(ai.calls, 1)
        return result

    def test_numeric_string_confidence_is_rejected(self):
        """Una cadena JSON numérica no satisface el contrato de número JSON."""
        result = self.correlate("0.7")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertIn("confidence", result.error)

    def test_true_confidence_is_rejected(self):
        """``true`` es booleano JSON y no debe convertirse implícitamente a 1.0."""
        result = self.correlate(True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertIn("confidence", result.error)

    def test_false_confidence_is_rejected(self):
        """``false`` es booleano JSON y no debe convertirse implícitamente a 0.0."""
        result = self.correlate(False)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertIn("confidence", result.error)

    def test_float_confidence_is_accepted(self):
        """Un número JSON decimal válido continúa siendo aceptado."""
        result = self.correlate(0.7)
        self.assertTrue(result.ok)
        self.assertEqual(result.confidence, 0.7)

    def test_integer_confidence_is_accepted(self):
        """Un entero JSON es numérico y se normaliza de forma segura a float."""
        result = self.correlate(1)
        self.assertTrue(result.ok)
        self.assertEqual(result.confidence, 1.0)


if __name__ == "__main__":
    unittest.main()
