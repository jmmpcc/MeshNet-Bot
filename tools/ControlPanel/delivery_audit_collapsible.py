"""Extensión visual para mostrar 'Mensajes emitidos' como bloque desplegable.

La extensión sólo modifica la presentación HTML del Control Panel. No cambia el
endpoint de auditoría, los filtros, la exportación CSV ni la función JavaScript
``loadDeliveryAudit()`` existente. Todos los elementos actuales se conservan dentro
del desplegable, por lo que sus identificadores y manejadores continúan intactos.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response


def _delivery_audit_collapsible_script() -> str:
    """Devuelve el JavaScript que convierte la auditoría existente en desplegable.

    La función trabaja sobre ``section.audit-shell``, que ya pertenece a ``web_admin``.
    No recrea filtros, estadísticas ni tabla: mueve los nodos DOM existentes dentro de
    un elemento ``details`` cerrado inicialmente. Es idempotente gracias al atributo
    ``data-meshnet-collapsible``.
    """
    return r"""
<script id="meshnet-delivery-audit-collapsible">
(() => {
  /**
   * Convierte el bloque histórico "Mensajes emitidos" en un details nativo.
   *
   * Conserva los mismos nodos DOM, IDs y manejadores del panel original. De este modo
   * ``loadDeliveryAudit()``, los filtros, el botón Actualizar y Exportar CSV siguen
   * funcionando exactamente igual al abrir el desplegable.
   */
  function ensureDeliveryAuditCollapsible() {
    const section = document.querySelector('section.audit-shell');
    if (!section || section.dataset.meshnetCollapsible === '1') return;

    const originalHeader = section.querySelector(':scope > .row');
    const headingBlock = originalHeader?.querySelector(':scope > div');

    const details = document.createElement('details');
    details.className = 'delivery-audit-details';

    const summary = document.createElement('summary');
    summary.style.cssText = 'cursor:pointer;list-style:none;display:flex;align-items:center;justify-content:space-between;gap:12px;';

    const heading = document.createElement('div');
    heading.innerHTML = headingBlock
      ? headingBlock.innerHTML
      : '<h2 style="margin:0">Mensajes emitidos</h2><p class="sub">Histórico común de entregas.</p>';

    const state = document.createElement('span');
    state.className = 'badge';
    state.textContent = 'DESPLEGAR';

    summary.appendChild(heading);
    summary.appendChild(state);

    const content = document.createElement('div');
    content.className = 'delivery-audit-collapsible-content';
    content.style.marginTop = '14px';

    if (headingBlock) headingBlock.remove();
    while (section.firstChild) content.appendChild(section.firstChild);

    details.appendChild(summary);
    details.appendChild(content);
    details.addEventListener('toggle', () => {
      state.textContent = details.open ? 'OCULTAR' : 'DESPLEGAR';
    });

    section.appendChild(details);
    section.dataset.meshnetCollapsible = '1';
  }

  ensureDeliveryAuditCollapsible();
  setInterval(ensureDeliveryAuditCollapsible, 1000);
})();
</script>
"""


def apply_delivery_audit_collapsible(app: FastAPI) -> FastAPI:
    """Añade el comportamiento desplegable al bloque existente de auditoría.

    Args:
        app: aplicación FastAPI del Control Panel ya creada por ``web_admin``.

    Returns:
        La misma instancia de FastAPI.

    Sólo se inyecta JavaScript en la respuesta HTML de ``/``. Las APIs y cualquier
    respuesta no HTML permanecen sin modificación. La instalación es idempotente.
    """
    if getattr(app.state, "delivery_audit_collapsible_installed", False):
        return app
    app.state.delivery_audit_collapsible_installed = True

    script = _delivery_audit_collapsible_script()

    @app.middleware("http")
    async def inject_delivery_audit_collapsible(request, call_next):
        response = await call_next(request)
        if request.url.path != "/":
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        html = body.decode("utf-8", errors="replace")
        if "meshnet-delivery-audit-collapsible" not in html:
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
