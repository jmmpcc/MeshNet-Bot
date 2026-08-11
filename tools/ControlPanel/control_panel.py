#!/usr/bin/env python3
"""Punto de entrada independiente de MeshNet ControlPanel."""

from __future__ import annotations

import argparse
import os

import uvicorn

# Importación compatible tanto con la ejecución oficial desde este directorio
# como con la importación del módulo durante pruebas automatizadas.
try:
    import web_admin
    from aprs_category_matrix import apply_aprs_category_matrix
except ModuleNotFoundError:
    from tools.ControlPanel import web_admin
    from tools.ControlPanel.aprs_category_matrix import apply_aprs_category_matrix


# v7.0.50 amplía la app ya creada por web_admin en lugar de duplicar el servidor
# o modificar sus rutas no relacionadas. La función es idempotente.
app = apply_aprs_category_matrix(web_admin.app)


def main() -> None:
    parser = argparse.ArgumentParser(description="Panel de aplicaciones independientes de MeshNet")
    parser.add_argument("--host", default=os.getenv("CONTROLPANEL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CONTROLPANEL_PORT", "8790")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, server_header=False, proxy_headers=True)


if __name__ == "__main__":
    main()
