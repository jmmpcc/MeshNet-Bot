import json
import unittest
from unittest.mock import patch

from shared.meshnet_ai import AIConfig, CircuitBreaker, MeshNetAI


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TestAIConfig(unittest.TestCase):
    """Verifica que la IA sea opcional y segura por defecto."""

    def test_missing_enabled_flag_means_disabled(self) -> None:
        cfg = AIConfig.from_env({})
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.validation_error(), "")

    def test_modules_remain_disabled_by_default(self) -> None:
        cfg = AIConfig.from_env({"MESHNET_AI_ENABLED": "1", "MESHNET_AI_MODEL": "modelo", "MESHNET_AI_API_KEY": "secreto"})
        self.assertTrue(cfg.enabled)
        self.assertFalse(any(cfg.features.as_dict().values()))

    def test_enabled_openai_requires_model_and_key(self) -> None:
        cfg = AIConfig.from_env({"MESHNET_AI_ENABLED": "1"})
        self.assertEqual(cfg.validation_error(), "MESHNET_AI_MODEL no configurado")

        cfg = AIConfig.from_env({"MESHNET_AI_ENABLED": "1", "MESHNET_AI_MODEL": "modelo"})
        self.assertEqual(cfg.validation_error(), "MESHNET_AI_API_KEY no configurada")

    def test_public_status_never_exposes_api_key(self) -> None:
        secret = "valor-secreto-no-mostrar"
        ai = MeshNetAI.from_env(
            {
                "MESHNET_AI_ENABLED": "1",
                "MESHNET_AI_PROVIDER": "openai",
                "MESHNET_AI_MODEL": "modelo",
                "MESHNET_AI_API_KEY": secret,
            }
        )
        serialized = json.dumps(ai.public_status(), ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("api_key", serialized.casefold())


class TestCircuitBreaker(unittest.TestCase):
    """Comprueba apertura, bloqueo temporal y recuperación automática."""

    def test_opens_after_threshold_and_recovers_after_cooldown(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(threshold=2, cooldown_sec=30, clock=clock)

        self.assertTrue(breaker.allow_request())
        breaker.record_failure()
        self.assertTrue(breaker.allow_request())
        breaker.record_failure()
        self.assertFalse(breaker.allow_request())
        self.assertTrue(breaker.is_open)

        clock.advance(31)
        self.assertTrue(breaker.allow_request())
        self.assertFalse(breaker.is_open)
        self.assertEqual(breaker.failures, 0)


class TestMeshNetAI(unittest.TestCase):
    """Verifica aislamiento: sin IA o con fallo siempre existe fallback seguro."""

    def test_disabled_never_calls_network(self) -> None:
        ai = MeshNetAI.from_env({})
        with patch("urllib.request.urlopen") as mocked:
            result = ai.generate_text("texto")
        mocked.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")

    def test_feature_requires_global_and_local_flag(self) -> None:
        ai = MeshNetAI.from_env({"MESHNET_AI_SUMMARIZE_ENABLED": "1"})
        self.assertFalse(ai.feature_enabled("summarize"))

        ai = MeshNetAI.from_env(
            {
                "MESHNET_AI_ENABLED": "1",
                "MESHNET_AI_SUMMARIZE_ENABLED": "1",
                "MESHNET_AI_MODEL": "modelo",
                "MESHNET_AI_API_KEY": "secreto",
            }
        )
        self.assertTrue(ai.feature_enabled("summarize"))
        self.assertFalse(ai.feature_enabled("network"))

    def test_provider_failure_returns_degraded_instead_of_raising(self) -> None:
        ai = MeshNetAI.from_env(
            {
                "MESHNET_AI_ENABLED": "1",
                "MESHNET_AI_PROVIDER": "openai",
                "MESHNET_AI_MODEL": "modelo",
                "MESHNET_AI_API_KEY": "secreto",
                "MESHNET_AI_FAILURE_THRESHOLD": "1",
            }
        )
        with patch.object(ai, "_call_openai_compatible", side_effect=RuntimeError("fallo controlado")):
            result = ai.generate_text("texto")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "degraded")
        self.assertTrue(ai.breaker.is_open)

    def test_success_resets_failures(self) -> None:
        ai = MeshNetAI.from_env(
            {
                "MESHNET_AI_ENABLED": "1",
                "MESHNET_AI_PROVIDER": "openai",
                "MESHNET_AI_MODEL": "modelo",
                "MESHNET_AI_API_KEY": "secreto",
            }
        )
        ai.breaker.record_failure()
        with patch.object(ai, "_call_openai_compatible", return_value="respuesta correcta"):
            result = ai.generate_text("texto")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "respuesta correcta")
        self.assertEqual(ai.breaker.failures, 0)


if __name__ == "__main__":
    unittest.main()
