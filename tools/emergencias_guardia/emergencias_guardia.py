#!/usr/bin/env python3
"""Punto de entrada de la aplicación independiente emergencias_guardia.

Este archivo puede ejecutarse directamente desde cualquier directorio, por ejemplo::

    python3 /home/meshnet/MeshNet-Bot/tools/emergencias_guardia/emergencias_guardia.py filters show

Al ejecutarse un archivo Python mediante una ruta, Python coloca el directorio del
script en ``sys.path`` pero no garantiza que la raíz del repositorio esté incluida.
La aplicación utiliza módulos compartidos de MeshNet-Bot (por ejemplo
``shared.delivery_audit``), por lo que añadimos explícitamente la raíz del proyecto
antes de importar el CLI de emergencias.
"""

from __future__ import annotations

import sys
from pathlib import Path


# Ruta estable del repositorio:
#   tools/emergencias_guardia/emergencias_guardia.py
#   ^ parent[0] = emergencias_guardia
#   ^ parent[1] = tools
#   ^ parent[2] = raíz de MeshNet-Bot
REPO_DIR = Path(__file__).resolve().parents[2]

# Se inserta al principio para que los módulos propios del repositorio tengan
# prioridad sin eliminar ni modificar las rutas que Python ya haya configurado.
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from emergencias.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
