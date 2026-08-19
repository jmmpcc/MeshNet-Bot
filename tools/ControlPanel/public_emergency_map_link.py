"""Acceso seguro al mapa público de emergencias desde el Control Panel.

La extensión no modifica web_admin ni duplica el visor de incidencias. Añade un único
enlace visual en la barra de pestañas existente y abre el mapa público en otra pestaña,
por lo que un fallo o indisponibilidad del dominio nunca afecta al Control Panel.
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.responses import Response


def _extension_script(public_url: str) -> str:
    """Genera el JavaScript idempotente que inserta la pestaña de mapa público."""
    encoded_url = json.dumps(public_url, ensure_ascii=False)
    return f'''
<script id="meshnet-public-emergency-map-link">
(() => {{
  if (window.__meshnetPublicEmergencyMapLinkInstalled) return;
  window.__meshnetPublicEmergencyMapLinkInstalled = true;

  function installPublicMapLink() {{
    const tabs = document.querySelector('.tabs');
    if (!tabs || document.querySelector('#meshnet-public-emergency-map-tab')) return;
    const link = document.createElement('a');
    link.id = 'meshnet-public-emergency-map-tab';
    link.className = 'tab';
    link.href = {encoded_url};
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'Mapa público';
    link.style.textDecoration = 'none';
    link.style.display = 'inline-flex';
    link.style.alignItems = 'center';
    tabs.appendChild(link);
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', installPublicMapLink, {{once:true}});
  }} else {{
    installPublicMapLink();
  }}
}})();
</script>
'''


def apply_public_emergency_map_link(app: FastAPI) -> FastAPI:
    """Añade la pestaña del mapa público sin registrar endpoints ni tocar datos.

    Args:
        app: aplicación FastAPI existente del Control Panel.

    Returns:
        La misma aplicación. La instalación es idempotente.

    Configuración:
        CONTROLPANEL_PUBLIC_EMERGENCY_MAP_URL permite cambiar la URL publicada sin
        modificar código. Por defecto usa el dominio operativo actual.
    """
    if getattr(app.state, "public_emergency_map_link_installed", False):
        return app
    app.state.public_emergency_map_link_installed = True

    public_url = os.getenv(
        "CONTROLPANEL_PUBLIC_EMERGENCY_MAP_URL",
        "https://ciberforense.com.es/emergencias/",
    ).strip()
    script = _extension_script(public_url)

    @app.middleware("http")
    async def inject_public_emergency_map_link(request, call_next):
        response = await call_next(request)
        if request.url.path != "/":
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        html = body.decode("utf-8", errors="replace")
        if "meshnet-public-emergency-map-link" not in html:
            html = html.replace("</body>", script + "</body>")

        headers = {
            key: value for key, value in response.headers.items()
            if key.lower() != "content-length"
        }
        return Response(
            content=html,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )

    return app
