"""Enlaces cartográficos para el detalle existente de Mensajes enviados.

Esta extensión no crea un visor nuevo ni sustituye el detalle del journal. Envuelve la
función JavaScript ``auditDetailHtml`` ya existente y añade, únicamente cuando el
mensaje auditado contiene una URL cartográfica con coordenadas válidas, un enlace a
Google Maps que se abre en una pestaña nueva.

La detección se limita deliberadamente a URLs de Google Maps ya presentes en el texto.
No intenta interpretar números libres como coordenadas y no participa en el envío,
propagación, deduplicación ni persistencia de mensajes.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response


def _extension_script() -> str:
    """Devuelve el JavaScript que amplía el detalle actual sin duplicar su HTML.

    La función conserva una referencia a ``auditDetailHtml`` y delega siempre en ella.
    Después inserta un pequeño bloque de enlace antes de la cuadrícula de entregas si
    encuentra coordenadas válidas en el mensaje lógico o en alguna entrega física.
    """
    return r'''
<script id="meshnet-message-map-links">
(() => {
  if (typeof auditDetailHtml !== 'function') return;
  if (window.__meshnetMessageMapLinksInstalled) return;
  window.__meshnetMessageMapLinksInstalled = true;

  const originalAuditDetailHtml = auditDetailHtml;

  function extractAuditMapCoordinates(text) {
    const value = String(text || '');
    if (!value) return null;

    const pattern = /(?:maps\.google\.com\/\?q=|google\.com\/maps\?q=)([-+]?\d{1,2}(?:\.\d+)?),([-+]?\d{1,3}(?:\.\d+)?)/i;
    const match = value.match(pattern);
    if (!match) return null;

    const latitude = Number(match[1]);
    const longitude = Number(match[2]);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
    if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
    return [latitude, longitude];
  }

  function operationMapCoordinates(operation) {
    const direct = extractAuditMapCoordinates(operation?.message);
    if (direct) return direct;

    for (const delivery of operation?.deliveries || []) {
      const coordinates = extractAuditMapCoordinates(delivery?.message);
      if (coordinates) return coordinates;
    }
    return null;
  }

  auditDetailHtml = function(operation, index) {
    const html = originalAuditDetailHtml(operation, index);
    const coordinates = operationMapCoordinates(operation);
    if (!coordinates) return html;

    const [latitude, longitude] = coordinates;
    const lat = String(latitude);
    const lon = String(longitude);
    const url = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(lat + ',' + lon)}`;
    const mapBlock = `<div class="audit-meta" style="margin-top:8px">📍 <a href="${url}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${esc(lat)}, ${esc(lon)} · Ver en Google Maps</a></div>`;
    const anchor = '<div class="delivery-grid" style="margin-top:10px">';
    return html.includes(anchor) ? html.replace(anchor, mapBlock + anchor) : html;
  };
})();
</script>
'''


def apply_message_map_links(app: FastAPI) -> FastAPI:
    """Añade enlaces cartográficos al detalle de Mensajes de una app existente.

    Args:
        app: instancia FastAPI ya creada por ``web_admin`` y ampliada por las
            extensiones previas del Control Panel.

    Returns:
        La misma instancia FastAPI. La función es idempotente.

    Sólo inyecta JavaScript en la página HTML raíz. No registra endpoints nuevos y no
    modifica respuestas JSON, operaciones del journal ni acciones mutantes.
    """
    if getattr(app.state, "message_map_links_installed", False):
        return app
    app.state.message_map_links_installed = True

    script = _extension_script()

    @app.middleware("http")
    async def inject_message_map_links(request, call_next):
        response = await call_next(request)
        if request.url.path != "/":
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        html = body.decode("utf-8", errors="replace")
        if "meshnet-message-map-links" not in html:
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
