import json
import os
import unittest
from unittest.mock import patch

from tools.intelligence.meshnet_intelligence import IntelligenceStatus


class TestMeshNetIntelligenceHealth(unittest.TestCase):
    """Comprueba que el health IA sea seguro incluso con credenciales configuradas."""

    def test_disabled_health_is_safe_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            payload = IntelligenceStatus().health()
        self.assertEqual(payload["service"], "meshnet-intelligence")
        self.assertEqual(payload["phase"], "IA-0")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["state"], "disabled")

    def test_health_never_contains_secret(self) -> None:
        secret = "credencial-privada-de-prueba"
        with patch.dict(
            os.environ,
            {
                "MESHNET_AI_ENABLED": "1",
                "MESHNET_AI_PROVIDER": "openai",
                "MESHNET_AI_MODEL": "modelo",
                "MESHNET_AI_API_KEY": secret,
            },
            clear=True,
        ):
            payload = IntelligenceStatus().health()
        raw = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret, raw)
        self.assertNotIn("api_key", raw.casefold())


if __name__ == "__main__":
    unittest.main()
