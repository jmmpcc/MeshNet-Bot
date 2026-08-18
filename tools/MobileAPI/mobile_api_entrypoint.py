#!/usr/bin/env python3
"""Punto de entrada estable de MeshNet Mobile API.

Este módulo es el único nombre que deben utilizar systemd y los scripts de arranque
para levantar la Mobile API. La implementación concreta puede seguir evolucionando
en módulos versionados sin obligar a modificar el servicio cada vez.

Arquitectura actual:

    mobile_api_entrypoint.py
        -> mobile_api_v7058.py
            -> mobile_api_v7054.py
                -> mobile_api.py

``mobile_api.py`` es la API base histórica; ``mobile_api_v7054`` amplía esa base y
``mobile_api_v7058`` añade autenticación por sesiones. Se conservan las tres capas
porque forman parte de la cadena validada actual y eliminarlas o renombrarlas podría
romper compatibilidad.

Ejecución recomendada:

    python3 -m uvicorn tools.MobileAPI.mobile_api_entrypoint:app --host 0.0.0.0 --port 8791

Cuando exista una implementación posterior, sólo deberá actualizarse el import de
este archivo tras validar compatibilidad. El servicio systemd permanecerá estable.
"""

from __future__ import annotations

# Reexportamos exclusivamente la aplicación FastAPI vigente. No se duplican rutas,
# middlewares, autenticación ni configuración: toda la lógica sigue en las capas
# versionadas que ya están probadas.
from tools.MobileAPI.mobile_api_v7058 import app

__all__ = ["app"]
