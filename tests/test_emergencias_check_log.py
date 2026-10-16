"""Pruebas del filtro de logs de comprobaciones periódicas de Emergencias."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/emergencias_guardia/emergencias_check_log.py"
spec = importlib.util.spec_from_file_location("emergencias_check_log_test", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def report(sources=None, **notify):
    """Construye un informe mínimo del CLI de Emergencias."""
    if sources is None:
        sources = {
            "dgt_datex": {"ok": True},
            "aemet_cap": {"ok": False, "skipped": "disabled"},
        }
    return {"fetch": {"sources": sources}, "notifications": {"failed": 0, **notify}}


class CheckLogTests(unittest.TestCase):
    def test_success_and_disabled_are_silent(self):
        self.assertEqual(module.diagnostics(report()), [])

    def test_intentional_skips_are_silent(self):
        sources = {
            "firms": {"ok": False, "skipped": "external_owner"},
            "che": {"ok": False, "skipped": "not_operational"},
        }
        self.assertEqual(module.diagnostics(report(sources)), [])

    def test_source_failure_is_reported(self):
        result = module.diagnostics(report({"dgt": {"ok": False, "error": "timeout"}}))
        self.assertEqual(result, ["fuente dgt: timeout"])

    def test_notification_failure_is_reported(self):
        self.assertIn("notificaciones: 2 entregas fallidas",
                      module.diagnostics(report(failed=2)))

    def test_trimmed_queue_is_reported(self):
        self.assertIn("cola de notificaciones recortada por límite",
                      module.diagnostics(report(trimmed=True)))

    def test_invalid_report_is_reported(self):
        self.assertTrue(module.diagnostics({"fetch": {}}))

    @patch.object(module.subprocess, "run")
    def test_success_process_exit_without_error(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps(report()), ""
        )
        self.assertEqual(module.main(), 0)

    @patch.object(module.subprocess, "run")
    def test_failure_preserves_exit_code(self, run):
        run.return_value = subprocess.CompletedProcess([], 3, "error report", "traceback")
        self.assertEqual(module.main(), 3)


if __name__ == "__main__":
    unittest.main()
