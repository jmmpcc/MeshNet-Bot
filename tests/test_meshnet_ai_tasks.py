"""Pruebas de Fase IA-1: resumen y clasificación sin proveedor real."""

from __future__ import annotations

import unittest

from shared.meshnet_ai import AIConfig, AIResult, AIFeatures, MeshNetAI
from shared.meshnet_ai_tasks import MeshNetAITasks, _fit_text


class FakeMeshNetAI(MeshNetAI):
    """Proveedor controlado para probar IA-1 sin red ni credenciales."""

    def __init__(self, config: AIConfig, replies: list[AIResult]):
        super().__init__(config)
        self._replies = list(replies)
        self.calls = 0

    def generate_text(self, prompt: str, system: str = "") -> AIResult:
        self.calls += 1
        if not self._replies:
            return AIResult(ok=False, status="degraded", error="sin respuesta de prueba")
        return self._replies.pop(0)


class MeshNetAITasksTests(unittest.TestCase):
    """Verifica activación, validación y fallback del motor IA-1."""

    def _config(self, *, enabled: bool = True, summarize: bool = True) -> AIConfig:
        return AIConfig(
            enabled=enabled,
            provider="ollama",
            model="modelo-prueba",
            features=AIFeatures(summarize=summarize),
        )

    def test_summary_disabled_does_not_call_provider(self) -> None:
        ai = FakeMeshNetAI(self._config(enabled=False), [])
        result = MeshNetAITasks(ai).summarize_text("Texto que no debe salir.")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        self.assertEqual(ai.calls, 0)

    def test_summary_feature_disabled_does_not_call_provider(self) -> None:
        ai = FakeMeshNetAI(self._config(summarize=False), [])
        result = MeshNetAITasks(ai).summarize_text("Texto que no debe salir.")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "feature_disabled")
        self.assertEqual(ai.calls, 0)

    def test_summary_respects_max_chars(self) -> None:
        ai = FakeMeshNetAI(
            self._config(),
            [AIResult(ok=True, text="Incendio forestal activo cerca de la carretera con viento fuerte", status="available")],
        )
        result = MeshNetAITasks(ai).summarize_text("Entrada", max_chars=36)
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.text), 36)
        self.assertEqual(ai.calls, 1)

    def test_summary_provider_failure_returns_fallback_signal(self) -> None:
        ai = FakeMeshNetAI(
            self._config(),
            [AIResult(ok=False, status="degraded", error="proveedor IA no accesible")],
        )
        result = MeshNetAITasks(ai).summarize_text("Entrada")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "degraded")

    def test_classification_accepts_only_allowed_label(self) -> None:
        ai = FakeMeshNetAI(
            self._config(),
            [AIResult(
                ok=True,
                status="available",
                text='{"label":"incendio","confidence":0.91,"reasoning":"menciona fuego forestal"}',
            )],
        )
        result = MeshNetAITasks(ai).classify_text(
            "Incendio forestal declarado",
            ["incendio", "inundacion", "otro"],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.label, "incendio")
        self.assertAlmostEqual(result.confidence, 0.91)

    def test_classification_rejects_invented_label(self) -> None:
        ai = FakeMeshNetAI(
            self._config(),
            [AIResult(ok=True, status="available", text='{"label":"terremoto","confidence":0.9}')],
        )
        result = MeshNetAITasks(ai).classify_text(
            "Texto cualquiera",
            ["incendio", "inundacion", "otro"],
        )
        self.assertFalse(result.ok)
        self.assertIn("fuera del catálogo", result.error)

    def test_classification_rejects_non_json(self) -> None:
        ai = FakeMeshNetAI(
            self._config(),
            [AIResult(ok=True, status="available", text="incendio")],
        )
        result = MeshNetAITasks(ai).classify_text("Fuego", ["incendio", "otro"])
        self.assertFalse(result.ok)
        self.assertIn("JSON", result.error)

    def test_classification_requires_two_labels(self) -> None:
        ai = FakeMeshNetAI(self._config(), [])
        result = MeshNetAITasks(ai).classify_text("Fuego", ["incendio"])
        self.assertFalse(result.ok)
        self.assertEqual(ai.calls, 0)

    def test_fit_text_uses_word_boundary(self) -> None:
        result = _fit_text("uno dos tres cuatro cinco", 18)
        self.assertLessEqual(len(result), 18)
        self.assertFalse(result.endswith(" "))


if __name__ == "__main__":
    unittest.main()
