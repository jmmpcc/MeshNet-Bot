#!/usr/bin/env python3
"""Ejecuta checks periódicos sin imprimir informes JSON correctos.

Uso: python emergencias_check_log.py
El CLI original, la persistencia y el envío de avisos permanecen intactos.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CHECK = Path(__file__).with_name("emergencias_guardia_scoped.py")


def diagnostics(report: Any) -> list[str]:
    """Analiza el resultado JSON del check y devuelve los fallos operativos.

    Parámetro report: objeto JSON de `check --notify-changes`.
    Los conectores deshabilitados u omitidos intencionadamente no son fallos.
    """
    if not isinstance(report, dict) or not isinstance(report.get("fetch"), dict):
        return ["respuesta de comprobación incompleta"]
    sources = report["fetch"].get("sources")
    if not isinstance(sources, dict):
        return ["respuesta sin estado de fuentes"]
    problems = []
    for name, status in sources.items():
        if not isinstance(status, dict):
            problems.append(f"fuente {name}: estado inválido")
        elif status.get("skipped") in ("disabled", "not_operational", "external_owner"):
            continue
        elif status.get("ok") is not True:
            reason = status.get("error") or status.get("reason") or "fallo sin detalle"
            problems.append(f"fuente {name}: {reason}")
    notifications = report.get("notifications")
    if not isinstance(notifications, dict):
        problems.append("respuesta sin estado de notificaciones")
    else:
        failures = notifications.get("failed", 0)
        if not isinstance(failures, int) or isinstance(failures, bool):
            problems.append("contador de entregas fallidas inválido")
        elif failures > 0:
            problems.append(f"notificaciones: {failures} entregas fallidas")
        if notifications.get("trimmed"):
            problems.append("cola de notificaciones recortada por límite")
    return problems


def parse_check_output(output: str) -> tuple[dict[str, Any] | None, str]:
    """Separa avisos textuales del informe JSON final del CLI.

    Args:
        output: stdout íntegro de `check --notify-changes`, que puede incluir
            advertencias APRS-IS antes de un objeto JSON multilínea.

    Returns:
        (informe, avisos). Solo acepta un objeto JSON final sin texto posterior
        salvo espacios; no silencia advertencias ni altera el CLI original.
    """
    decoder = json.JSONDecoder()
    offset = 0
    for line in output.splitlines(keepends=True):
        # El informe se imprime como objeto al comienzo de una línea.
        if not line.lstrip().startswith("{"):
            offset += len(line)
            continue
        candidate = output[offset:].lstrip()
        try:
            report, end = decoder.raw_decode(candidate)
        except ValueError:
            offset += len(line)
            continue
        if isinstance(report, dict) and not candidate[end:].strip():
            return report, output[:offset]
        offset += len(line)
    return None, output


def print_diagnostics_text(output: str, *, max_chars: int = 3000) -> None:
    """Escribe avisos o errores en stderr sin volcar un informe JSON gigante."""
    content = output.strip()
    if not content:
        return
    if len(content) > max_chars:
        content = content[:max_chars] + "\n[emergencias-check] salida truncada"
    print(content, file=sys.stderr, flush=True)


def main() -> int:
    """Ejecuta un check y registra solo stderr, fallos y respuestas inválidas.

    Llamada: desde el bucle del entrypoint Docker, una vez por intervalo.
    Retorna 0 ante éxito, o un código distinto de cero ante fallo.
    """
    command = [sys.executable, "-u", str(CHECK), "check", "--notify-changes"]
    result = subprocess.run(
        command, capture_output=True, text=True, errors="replace", check=False
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr,
              end="" if result.stderr.endswith("\n") else "\n", flush=True)
    report, warnings = parse_check_output(result.stdout)
    # Las advertencias del CLI son operativas y deben conservarse visibles.
    print_diagnostics_text(warnings)
    if result.returncode != 0:
        print(f"[emergencias-check] código de salida: {result.returncode}",
              file=sys.stderr, flush=True)
        return result.returncode
    if report is None:
        print("[emergencias-check] respuesta JSON final no válida o ausente",
              file=sys.stderr, flush=True)
        return 1
    problems = diagnostics(report)
    for problem in problems:
        print(f"[emergencias-check] {problem}", file=sys.stderr, flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
