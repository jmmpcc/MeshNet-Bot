"""Pestaña integrada del mapa público de emergencias en el Control Panel.

La extensión no modifica ``web_admin.py`` ni duplica el visor de incidencias. Reutiliza
la barra de pestañas existente de ``emergencias_guardia`` y añade un iframe aislado que
carga exclusivamente la página pública. La sesión, las cookies y los endpoints privados
del Control Panel no se transmiten al dominio público.
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.responses import Response


def _extension_script(public_url: str) -> str:
    """Genera el JavaScript idempotente que añade la pestaña pública integrada.

    Uso:
        script = _extension_script("https://ciberforense.com.es/emergencias/")

    Parámetros:
        public_url: URL HTTPS pública que se cargará en el iframe.

    Funcionalidad:
        Espera a que la tarjeta dinámica de ``emergencias_guardia`` exista, localiza
        su navegación de pestañas y añade ``Mapa público`` junto a las pestañas ya
        existentes. No sustituye funciones originales ni modifica otros módulos.
    """
    encoded_url = json.dumps(public_url, ensure_ascii=False)
    return f'''
<script id="meshnet-public-emergency-map-link">
(() => {{
  if (window.__meshnetPublicEmergencyMapLinkInstalled) return;
  window.__meshnetPublicEmergencyMapLinkInstalled = true;

  const PUBLIC_MAP_URL = {encoded_url};

  function installPublicMapTab() {{
    const overview = document.querySelector('#overview-emergencias_guardia');
    if (!overview) return false;

    const card = overview.closest('article.card');
    if (!card) return false;

    const tabs = card.querySelector('nav.tabs');
    if (!tabs) return false;

    if (!card.querySelector('#meshnet-public-emergency-map-tab')) {{
      const button = document.createElement('button');
      button.id = 'meshnet-public-emergency-map-tab';
      button.className = 'tab';
      button.type = 'button';
      button.textContent = 'Mapa público';
      button.addEventListener('click', () => {{
        if (typeof window.openEmergencyTab === 'function') {{
          window.openEmergencyTab('public-map', button);
        }} else {{
          card.querySelectorAll('[data-emtab]').forEach(panel =>
            panel.classList.toggle('active', panel.dataset.emtab === 'public-map')
          );
          tabs.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
          button.classList.add('active');
        }}
      }});
      tabs.appendChild(button);
    }}

    if (!card.querySelector('#meshnet-public-emergency-map-panel')) {{
      const panel = document.createElement('section');
      panel.id = 'meshnet-public-emergency-map-panel';
      panel.className = 'filterbox tab-panel';
      panel.dataset.emtab = 'public-map';
      panel.innerHTML = `
        <div class="row">
          <div>
            <h3>Mapa público de emergencias</h3>
            <p class="muted">Misma información pública publicada en el dominio MeshNet.</p>
          </div>
          <a href="${{PUBLIC_MAP_URL}}" target="_blank" rel="noopener noreferrer"
             style="color:inherit">Abrir aparte</a>
        </div>
        <iframe
          id="meshnet-public-emergency-map-frame"
          title="Mapa público de emergencias MeshNet"
          src="${{PUBLIC_MAP_URL}}"
          loading="lazy"
          referrerpolicy="no-referrer"
          sandbox="allow-scripts allow-same-origin allow-popups"
          style="width:100%;height:min(72vh,760px);min-height:520px;border:1px solid #294a65;border-radius:14px;background:#0b1520"
        ></iframe>`;

      const result = card.querySelector('.result');
      if (result) card.insertBefore(panel, result);
      else card.appendChild(panel);
    }}

    return true;
  }}

  if (!installPublicMapTab()) {{
    const observer = new MutationObserver(() => {{
      if (installPublicMapTab()) observer.disconnect();
    }});
    observer.observe(document.documentElement, {{childList:true, subtree:true}});
  }}
}})();
</script>
'''


def apply_public_emergency_map_link(app: FastAPI) -> FastAPI:
    """Añade la pestaña pública integrada sin registrar endpoints nuevos.

    Uso:
        app = apply_public_emergency_map_link(app)

    Parámetros:
        app: instancia FastAPI existente del Control Panel.

    Returns:
        La misma aplicación FastAPI, conservando todos sus endpoints y middleware.

    Configuración:
        ``CONTROLPANEL_PUBLIC_EMERGENCY_MAP_URL`` permite cambiar la URL publicada
        sin modificar código. La instalación es idempotente para evitar duplicados.
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
        """Inyecta la extensión únicamente en la página HTML principal del panel."""
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
            key: value
            for key, value in response.headers.items()
            if key.lower() != "content-length"
        }
        return Response(
            content=html,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )

    return app
