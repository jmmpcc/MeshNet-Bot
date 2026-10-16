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
    def test_compose_emergency_aprs_host_is_broker(self):
        """El check usa broker, sin cambiar la red del gateway APRS."""
        compose = (SCRIPT.parents[2] / "docker-compose.rpi.yml").read_text(encoding="utf-8")
        # El bloque se delimita por nombre de servicio para no confundirlo
        # con Farmacias ni con la configuración del gateway APRS.
        service = compose.split("\n  emergencias-check:\n", 1)[1]
        self.assertIn("APRS_CTRL_HOST: broker", service)
        self.assertIn("BROKER_CTRL_HOST: broker", service)
        aprs = compose.split("\n  aprs:\n", 1)[1].split("\n  bridgehub-bc:\n", 1)[0]
        self.assertIn('network_mode: "service:broker"', aprs)

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

    def test_report_with_aprs_warning_is_parsed(self):
        """La advertencia previa no invalida un informe de éxito."""
        message = "[emergencias] APRS-IS bulletin WARN: timeout\n"
        found, warnings = module.parse_check_output(message + json.dumps(report(), indent=2))
        self.assertEqual(found, report())
        self.assertEqual(warnings, message)

    def test_report_with_only_warning_is_invalid(self):
        """No se acepta una advertencia aislada como informe."""
        found, warnings = module.parse_check_output("WARN: timeout\n")
        self.assertIsNone(found)
        self.assertEqual(warnings, "WARN: timeout\n")

    @patch.object(module.subprocess, "run")
    def test_warning_and_valid_json_do_not_dump_report(self, run):
        """El wrapper conserva el aviso y silencia el JSON normal."""
        import contextlib
        import io
        run.return_value = subprocess.CompletedProcess(
            [], 0, "APRS-IS WARN: timed out\n" + json.dumps(report()), ""
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = module.main()
        self.assertEqual(code, 0)
        self.assertIn("APRS-IS WARN", stderr.getvalue())
        self.assertNotIn('"fetch"', stderr.getvalue())

    @patch.object(module.subprocess, "run")
    def test_no_json_is_failure_without_full_dump(self, run):
        """La salida inválida se limita para evitar llenar Docker logs."""
        import contextlib
        import io
        run.return_value = subprocess.CompletedProcess([], 0, "X" * 9000, "")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = module.main()
        self.assertEqual(code, 1)
        self.assertLess(len(stderr.getvalue()), 3500)

    @patch.object(module.subprocess, "run")
    def test_failure_preserves_exit_code(self, run):
        run.return_value = subprocess.CompletedProcess([], 3, "error report", "traceback")
        self.assertEqual(module.main(), 3)


if __name__ == "__main__":
    unittest.main()
