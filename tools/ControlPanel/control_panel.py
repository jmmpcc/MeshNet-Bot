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
    from delivery_audit_collapsible import apply_delivery_audit_collapsible
    from emergency_current_collapsible import apply_emergency_current_collapsible
    from emergency_province_view import apply_emergency_province_view
    from message_emergency_filters import apply_message_emergency_filters
    from message_map_links import apply_message_map_links
except ModuleNotFoundError:
    from tools.ControlPanel import web_admin
    from tools.ControlPanel.aprs_category_matrix import apply_aprs_category_matrix
    from tools.ControlPanel.delivery_audit_collapsible import apply_delivery_audit_collapsible
    from tools.ControlPanel.emergency_current_collapsible import apply_emergency_current_collapsible
    from tools.ControlPanel.emergency_province_view import apply_emergency_province_view
    from tools.ControlPanel.message_emergency_filters import apply_message_emergency_filters
    from tools.ControlPanel.message_map_links import apply_message_map_links


# Las extensiones amplían la app ya creada por web_admin en lugar de duplicar el servidor
# o modificar rutas no relacionadas. Todas son idempotentes y conservan intactas las
# funciones históricas del Control Panel.
app = apply_aprs_category_matrix(web_admin.app)

# IMPORTANTE: la ventana temporal debe instalarse ANTES que la vista de incidencias.
# Sus middlewares se ejecutan de forma que el script de esta extensión queda inyectado
# antes de ``emergency_province_view`` en el HTML final. Así ``window.fetch`` ya está
# interceptado cuando la vista lanza su primera petición y nunca sale una carga inicial
# sin filtrar de ``/api/emergencias/current-view``.
app = apply_emergency_current_collapsible(app)
app = apply_emergency_province_view(app)

app = apply_message_emergency_filters(app)
app = apply_message_map_links(app)
app = apply_delivery_audit_collapsible(app)


def main() -> None:
    parser = argparse.ArgumentParser(description="Panel de aplicaciones independientes de MeshNet")
    parser.add_argument("--host", default=os.getenv("CONTROLPANEL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CONTROLPANEL_PORT", "8790")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, server_header=False, proxy_headers=True)


if __name__ == "__main__":
    main()
