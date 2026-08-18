"""Optimización visual y temporal para la vista de incidencias actuales.

Esta extensión trabaja sobre ``emergency_province_view`` sin sustituirla. Conserva
sus filtros, vista Lista/Mapa, enlaces, marcadores y manejadores existentes, y añade:

* un desplegable nativo ``details`` equivalente al usado en "Mensajes emitidos";
* un filtro temporal cuyo valor inicial es 24 horas;
* un endpoint de sólo lectura que filtra los eventos antes de serializarlos al navegador.

La fuente de verdad continúa siendo ``emergencias.storage.load_current()`` y no se
modifica ninguna lógica de recogida, deduplicación, propagación o envío.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response

try:
    from emergency_province_view import build_emergency_snapshot
except ModuleNotFoundError:
    from tools.ControlPanel.emergency_province_view import build_emergency_snapshot


DEFAULT_EMERGENCY_WINDOW_HOURS = 24


def _normalise_datetime(value: Any) -> datetime | None:
    """Convierte una marca temporal de Emergencias a ``datetime`` UTC.

    Args:
        value: valor ISO-8601, ``datetime`` o valor vacío procedente de un evento.

    Returns:
        Fecha/hora con zona UTC, o ``None`` cuando el valor no es interpretable.

    Se acepta el sufijo ``Z`` y también fechas sin zona. En este último caso se
    interpretan como UTC, coherente con el almacenamiento interno de Emergencias.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_reference_datetime(event: Any) -> datetime | None:
    """Obtiene la fecha más útil para decidir si una incidencia entra en la ventana.

    Args:
        event: objeto ``Event`` o compatible con sus atributos temporales.

    Returns:
        ``updated_at``/``last_seen``/``started_at`` normalizado, por ese orden.

    Se prioriza la última actualización porque una incidencia antigua pero todavía
    actualizada recientemente debe seguir apareciendo en la vista de 24 horas.
    """
    for attribute in ("updated_at", "last_seen", "started_at"):
        parsed = _normalise_datetime(getattr(event, attribute, None))
        if parsed is not None:
            return parsed
    return None


def build_windowed_emergency_snapshot(
    events: Iterable[Any],
    *,
    hours: int = DEFAULT_EMERGENCY_WINDOW_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Construye la instantánea de incidencias limitada a una ventana temporal.

    Args:
        events: iterable de eventos actuales leído desde ``load_current()``.
        hours: número de horas hacia atrás. ``0`` significa mostrar todas.
        now: instante de referencia opcional para pruebas; por defecto, UTC actual.

    Returns:
        La misma estructura producida por ``build_emergency_snapshot`` pero sólo con
        los eventos de la ventana solicitada.

    Los eventos sin fecha interpretable se conservan. Es una decisión de compatibilidad:
    ocultarlos por no disponer de fecha podría hacer desaparecer incidencias válidas de
    fuentes históricas o incompletas. El valor ``hours`` se limita a valores no negativos.
    """
    safe_hours = max(0, int(hours))
    event_list = list(events)
    if safe_hours == 0:
        return build_emergency_snapshot(event_list)

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)
    cutoff = reference - timedelta(hours=safe_hours)

    selected = []
    for event in event_list:
        event_time = _event_reference_datetime(event)
        if event_time is None or event_time >= cutoff:
            selected.append(event)

    return build_emergency_snapshot(selected)


def _emergency_current_collapsible_script() -> str:
    """Devuelve el JavaScript que añade periodo y plegado sin recrear la vista.

    Esta extensión debe instalarse antes de ``emergency_province_view`` en
    ``control_panel.py``. De ese modo su script aparece primero en el HTML final e
    intercepta la primera petición de incidencias antes de que la vista la lance.

    El script puede ejecutarse antes de que exista el panel porque espera a que
    ``emergency_province_view`` cree su sección. Después mueve los mismos nodos DOM
    dentro de un ``details`` cerrado inicialmente y añade el selector de periodo.
    """
    return r"""
<script id="meshnet-emergency-current-collapsible">
(() => {
  const DEFAULT_HOURS = '24';
  let selectedHours = DEFAULT_HOURS;

  /**
   * Redirige únicamente la lectura de incidencias actuales al endpoint optimizado.
   * El resto de llamadas fetch del Control Panel mantienen su comportamiento original.
   *
   * Este interceptor se instala antes de que ``emergency_province_view`` ejecute su
   * primera carga, por lo que no se produce ninguna petición histórica sin filtrar.
   */
  if (!window.__meshnetEmergencyWindowFetchInstalled) {
    const originalFetch = window.fetch.bind(window);
    window.fetch = function(input, init) {
      let url = typeof input === 'string' ? input : input?.url;
      if (url && (url === '/api/emergencias/current-view' || url.startsWith('/api/emergencias/current-view?'))) {
        const separator = url.includes('?') ? '&' : '?';
        url = `${url}${separator}hours=${encodeURIComponent(selectedHours)}`;
        if (typeof input === 'string') {
          input = url.replace('/api/emergencias/current-view', '/api/emergencias/current-view-window');
        } else {
          input = new Request(
            url.replace('/api/emergencias/current-view', '/api/emergencias/current-view-window'),
            input
          );
        }
      }
      return originalFetch(input, init);
    };
    window.__meshnetEmergencyWindowFetchInstalled = true;
  }

  /** Solicita una recarga usando el manejador histórico ya instalado por la vista. */
  function refreshEmergencyCurrentView() {
    document.querySelector('#emergency-province-refresh')?.click();
  }

  /** Añade el selector temporal dentro de la cuadrícula de filtros existente. */
  function ensureEmergencyWindowSelector(section) {
    if (section.querySelector('#emergency-window-hours')) return;
    const grid = section.querySelector('.channel-grid');
    if (!grid) return;

    const label = document.createElement('label');
    label.textContent = 'Periodo';
    const select = document.createElement('select');
    select.id = 'emergency-window-hours';
    select.innerHTML = `
      <option value="24" selected>Últimas 24 horas</option>
      <option value="48">Últimas 48 horas</option>
      <option value="72">Últimas 72 horas</option>
      <option value="168">Últimos 7 días</option>
      <option value="0">Todas</option>`;
    select.value = selectedHours;
    select.addEventListener('change', () => {
      selectedHours = select.value || DEFAULT_HOURS;
      refreshEmergencyCurrentView();
    });
    label.appendChild(select);
    grid.appendChild(label);
  }

  /**
   * Convierte "Incidencias actuales" en un desplegable nativo conservando todos los
   * nodos, IDs y listeners creados por ``emergency_province_view``.
   */
  function ensureEmergencyCurrentCollapsible() {
    const section = document.querySelector('#emergency-province-view');
    if (!section || section.dataset.meshnetCollapsible === '1') return;

    ensureEmergencyWindowSelector(section);

    const originalHeader = section.querySelector(':scope > .row');
    const headingBlock = originalHeader?.querySelector(':scope > div');

    const details = document.createElement('details');
    details.className = 'emergency-current-details';

    const summary = document.createElement('summary');
    summary.style.cssText = 'cursor:pointer;list-style:none;display:flex;align-items:center;justify-content:space-between;gap:12px;';

    const heading = document.createElement('div');
    heading.innerHTML = headingBlock
      ? headingBlock.innerHTML
      : '<strong>Incidencias actuales</strong><div class="hint">Últimas 24 horas</div>';

    const state = document.createElement('span');
    state.className = 'badge';
    state.textContent = 'DESPLEGAR';

    summary.appendChild(heading);
    summary.appendChild(state);

    const content = document.createElement('div');
    content.className = 'emergency-current-collapsible-content';
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

  ensureEmergencyCurrentCollapsible();
  setInterval(ensureEmergencyCurrentCollapsible, 1000);
})();
</script>
"""


def apply_emergency_current_collapsible(app: FastAPI) -> FastAPI:
    """Añade ventana temporal y plegado a la vista actual de Emergencias.

    Args:
        app: aplicación FastAPI del Control Panel. Debe aplicarse antes de
            ``apply_emergency_province_view`` para que el interceptor JavaScript quede
            disponible antes de la primera carga de incidencias.

    Returns:
        La misma instancia FastAPI, ampliada de forma idempotente.

    Registra un endpoint GET adicional y un middleware limitado al HTML de ``/``.
    No sustituye ni modifica rutas mutantes ni la ruta histórica de incidencias.
    """
    if getattr(app.state, "emergency_current_collapsible_installed", False):
        return app
    app.state.emergency_current_collapsible_installed = True

    @app.get("/api/emergencias/current-view-window")
    def emergency_current_view_window(
        hours: int = DEFAULT_EMERGENCY_WINDOW_HOURS,
    ) -> dict[str, Any]:
        """Devuelve sólo las incidencias comprendidas en el periodo solicitado."""
        from tools.emergencias_guardia.emergencias.storage import load_current

        return build_windowed_emergency_snapshot(
            load_current().values(),
            hours=hours,
        )

    script = _emergency_current_collapsible_script()

    @app.middleware("http")
    async def inject_emergency_current_collapsible(request, call_next):
        """Inyecta la mejora sólo en la página HTML principal del Control Panel."""
        response = await call_next(request)
        if request.url.path != "/":
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        html = body.decode("utf-8", errors="replace")
        if "meshnet-emergency-current-collapsible" not in html:
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
