"""Regresiones de reintento sin red, sin systemd y sin archivos productivos."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools.emergencias_guardia.emergencias.public_map import (
    build_public_payload,
    publish_if_changed,
)

MODULE = "tools.emergencias_guardia.emergencias.public_map"


class PublicMapRetryTests(unittest.TestCase):
    """Comprueba el enfriamiento sin conexiones ni escrituras remotas."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.current = Path(self.tmp.name) / "current.json"
        self.state = Path(self.tmp.name) / "state.json"
        self.current.write_text(
            json.dumps({"events": [{"event_id": "one", "status": "active",
                                    "latitude": 41.6, "longitude": -0.8}]}),
            encoding="utf-8",
        )
        self.env = patch.dict(
            "os.environ", {"EMERGENCIAS_PUBLIC_MAP_ENABLED": "1"}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_failure_persists_cooldown_and_keeps_prior_revision(self) -> None:
        """Timeout conserva la revisión publicada y fija diez minutos de espera."""
        self.state.write_text(
            json.dumps({"revision": "previous", "published_at": "earlier"}),
            encoding="utf-8",
        )
        before = datetime.now(timezone.utc)
        with patch(f"{MODULE}.publish_public_map", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                publish_if_changed(self.current, self.state)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["revision"], "previous")
        self.assertEqual(saved["last_error_type"], "TimeoutError")
        due = datetime.fromisoformat(saved["retry_after"])
        self.assertGreaterEqual(due, before + timedelta(minutes=10))
        self.assertLess(due, datetime.now(timezone.utc) + timedelta(minutes=10, seconds=5))

    def test_changes_during_cooldown_never_connect(self) -> None:
        """Un cambio de incidencias no evita el bloqueo de diez minutos."""
        self.state.write_text(
            json.dumps({"revision": "prior", "retry_after": (
                datetime.now(timezone.utc) + timedelta(minutes=9)
            ).isoformat()}), encoding="utf-8"
        )
        with patch(f"{MODULE}.publish_public_map") as publish:
            outcome = publish_if_changed(self.current, self.state)
            self.assertTrue(outcome["deferred"])
            self.current.write_text(
                json.dumps({"events": [{"event_id": "new", "status": "active",
                                        "latitude": 41.7, "longitude": -0.9}]}),
                encoding="utf-8",
            )
            outcome = publish_if_changed(self.current, self.state)
            self.assertTrue(outcome["deferred"])
            publish.assert_not_called()

    def test_expired_cooldown_retries_and_clears_error(self) -> None:
        """Tras vencer la espera, éxito guarda revisión y elimina el bloqueo."""
        self.state.write_text(
            json.dumps({"revision": "prior", "retry_after": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(), "last_error_type": "TimeoutError"}), encoding="utf-8"
        )
        payload = build_public_payload(self.current)
        with patch(f"{MODULE}.publish_public_map", return_value={
            "published": True, "revision": payload["revision"],
            "generated_at": payload["generated_at"]
        }) as publish:
            outcome = publish_if_changed(self.current, self.state)
        self.assertTrue(outcome["published"])
        publish.assert_called_once_with(self.current)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["revision"], payload["revision"])
        self.assertNotIn("retry_after", saved)
        self.assertNotIn("last_error_type", saved)

    def test_unchanged_or_disabled_does_not_connect(self) -> None:
        """Revisión sin cambios y modo apagado no abren conexiones."""
        revision = build_public_payload(self.current)["revision"]
        self.state.write_text(json.dumps({"revision": revision}), encoding="utf-8")
        with patch(f"{MODULE}.publish_public_map") as publish:
            self.assertTrue(publish_if_changed(self.current, self.state)["unchanged"])
            publish.assert_not_called()
        self.state.unlink()
        with patch.dict("os.environ", {"EMERGENCIAS_PUBLIC_MAP_ENABLED": "0"}):
            with patch(f"{MODULE}.publish_public_map") as publish:
                self.assertFalse(publish_if_changed(self.current, self.state)["enabled"])
                publish.assert_not_called()
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
