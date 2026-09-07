"""Pruebas contractuales adicionales de IA-1 sobre flags y ausencia de efectos."""

from __future__ import annotations

import unittest

from shared.meshnet_ai import AIConfig, AIResult, AIFeatures, MeshNetAI
from shared.meshnet_ai_tasks import MeshNetAITasks


class CountingAI(MeshNetAI):
    """IA controlada que permite verificar cuándo se intenta una llamada."""

    def __init__(self, config: AIConfig):
        super().__init__(config)
        self.calls = 0

    def generate_text(self, prompt: str, system: str = "") -> AIResult:
        self.calls += 1
        return AIResult(ok=True, text="resumen", status="available")


class IA1ContractTests(unittest.TestCase):
    """Garantiza que IA-1 no pueda activarse de forma implícita."""

    def test_global_off_has_priority_over_summarize_flag(self) -> None:
        config = AIConfig(
            enabled=False,
            provider="ollama",
            model="modelo-prueba",
            features=AIFeatures(summarize=True),
        )
        ai = CountingAI(config)
        result = MeshNetAITasks(ai).summarize_text("Texto")
        self.assertFalse(result.ok)
        self.assertEqual(ai.calls, 0)

    def test_empty_summary_input_never_calls_provider(self) -> None:
        config = AIConfig(
            enabled=True,
            provider="ollama",
            model="modelo-prueba",
            features=AIFeatures(summarize=True),
        )
        ai = CountingAI(config)
        result = MeshNetAITasks(ai).summarize_text("   ")
        self.assertFalse(result.ok)
        self.assertEqual(ai.calls, 0)

    def test_classification_global_off_never_calls_provider(self) -> None:
        config = AIConfig(enabled=False, provider="ollama", model="modelo-prueba")
        ai = CountingAI(config)
        result = MeshNetAITasks(ai).classify_text("Texto", ["a", "b"])
        self.assertFalse(result.ok)
        self.assertEqual(ai.calls, 0)


if __name__ == "__main__":
    unittest.main()
