"""Regresión v7.0.48.2: el CLI de emergencias debe localizar módulos compartidos.

La matriz de propagación del ControlPanel ejecuta ``emergencias_guardia.py`` como
un proceso Python independiente. Esta prueba reproduce ese arranque sin depender
de que ``PYTHONPATH`` esté configurado externamente.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
EMERGENCY_CLI = REPO_DIR / "tools/emergencias_guardia/emergencias_guardia.py"


def test_emergency_cli_direct_start_can_import_shared_modules() -> None:
    """Ejecutar el entrypoint por ruta debe importar ``shared`` correctamente.

    Se elimina ``PYTHONPATH`` deliberadamente para reproducir el escenario real
    que provocaba ``ModuleNotFoundError: No module named 'shared'`` desde el
    ControlPanel. ``--help`` es suficiente: Python debe completar todos los
    imports del CLI, notifier y dispatcher antes de mostrar la ayuda.
    """

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(EMERGENCY_CLI), "--help"],
        cwd=REPO_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "ModuleNotFoundError" not in combined
    assert "No module named 'shared'" not in combined
