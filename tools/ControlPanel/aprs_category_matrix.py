"""Extensión v7.0.50 de la matriz de propagación del ControlPanel.

Este módulo amplía exclusivamente la configuración visual de Emergencias con dos
columnas de autorización secundaria: APRS-IS y APRS RF. Se aplica sobre la app
FastAPI ya creada por ``web_admin`` para reutilizar toda su autenticación,
validación, CLI de filtros y persistencia existente.

Diseño de compatibilidad:

* La matriz Baja/Media/Alta/Crítica continúa siendo gestionada por el endpoint
  histórico y por la CLI de Emergencias, sin modificar su formato.
* Las nuevas listas se almacenan en ``tools/emergencias_guardia/.env`` mediante
  el helper atómico existente ``update_env_values``.
* Si las variables nuevas no existen, se considera que todas las categorías
  están autorizadas, reproduciendo el comportamiento anterior a v7.0.50.
* El dispatcher mantiene además sus interruptores generales y ``MIN_LEVEL``;
  estas columnas nunca los sustituyen ni los rebajan.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

try:  # Ejecución oficial desde tools/ControlPanel/control_panel.py
    import web_admin
except ModuleNotFoundError:  # Importación como paquete durante pruebas
    from tools.ControlPanel import web_admin


APRS_CATEGORY_ENV_KEYS = {
    "aprsis": "EMERGENCIAS_APRSIS_CATEGORIES",
    "aprs_rf": "EMERGENCIAS_APRS_RF_CATEGORIES",
}
SECONDARY_TRANSPORTS = tuple(APRS_CATEGORY_ENV_KEYS)


class EmergencyFiltersV7050Payload(BaseModel):
    """Payload compatible con la matriz histórica y sus dos columnas APRS.

    ``rules`` conserva exactamente la estructura que ya consume
    ``EmergencyFiltersPayload`` de ``web_admin``. ``secondary_transports`` es
    opcional para que clientes antiguos que solo envían ``rules`` continúen
    funcionando sin modificar las listas APRS existentes.
    """

    severities: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    rules: dict[str, list[str]] | None = None
    secondary_transports: dict[str, list[str]] | None = None


def _normalise_category_list(values: list[str]) -> list[str]:
    """Normaliza una lista de categorías manteniendo solo valores válidos.

    La validación estricta de valores no permitidos se realiza antes de llamar a
    esta función. Aquí únicamente se eliminan duplicados y se ordena el resultado
    que se escribirá en el ``.env``.
    """
    return sorted({str(value).strip().lower() for value in values if str(value).strip()})


def _read_secondary_transports() -> dict[str, list[str]]:
    """Devuelve las listas APRS efectivas sin alterar el ``.env``.

    Ausencia de una variable = comportamiento histórico = todas las categorías
    elegibles. Variable presente y vacía = ninguna categoría autorizada para ese
    transporte.
    """
    keys = set(APRS_CATEGORY_ENV_KEYS.values())
    env_values = web_admin.read_env_values(web_admin.EMERGENCIAS_ENV_FILE, keys)
    all_categories = sorted(web_admin.EMERGENCY_CATEGORIES)
    result: dict[str, list[str]] = {}
    for transport, env_name in APRS_CATEGORY_ENV_KEYS.items():
        if env_name not in env_values:
            result[transport] = list(all_categories)
            continue
        result[transport] = sorted({
            item.strip().lower()
            for item in env_values.get(env_name, "").split(",")
            if item.strip().lower() in web_admin.EMERGENCY_CATEGORIES
        })
    return result


def _validate_secondary_transports(value: dict[str, list[str]]) -> dict[str, list[str]]:
    """Valida que solo se configuren APRS-IS/APRS RF y categorías conocidas."""
    unknown_transports = set(value) - set(SECONDARY_TRANSPORTS)
    if unknown_transports:
        raise HTTPException(status_code=422, detail="Salida secundaria de emergencias no válida")

    normalised: dict[str, list[str]] = {}
    for transport in SECONDARY_TRANSPORTS:
        items = value.get(transport, [])
        if not set(items).issubset(web_admin.EMERGENCY_CATEGORIES):
            raise HTTPException(status_code=422, detail="Categoría APRS de emergencias no válida")
        normalised[transport] = _normalise_category_list(items)
    return normalised


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    """Sustituye un ancla UI una sola vez y falla si cambia el HTML base.

    Este guard evita que una futura modificación de ``web_admin.DASHBOARD``
    aplique parcialmente la extensión sin que lo detectemos durante el arranque.
    """
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"v7.0.50: ancla ControlPanel inesperada para {label}: {count}")
    return text.replace(old, new, 1)


def transform_dashboard(source: str) -> str:
    """Añade las columnas APRS-IS/APRS RF sin reescribir el dashboard completo.

    Parámetros:
        source: HTML/JavaScript original de ``web_admin.DASHBOARD``.

    Devuelve una nueva cadena conservando el resto del panel byte a byte salvo
    las anclas explícitas documentadas en esta función.
    """
    dashboard = _replace_once(
        source,
        "UI 2 · v7.0.48",
        "UI 2 · v7.0.50",
        "versión visible",
    )
    dashboard = _replace_once(
        dashboard,
        ".matrix input{width:18px;height:18px}",
        ".matrix input{width:18px;height:18px}.matrix .secondary-start{border-left:2px solid #6a4218}.matrix th.aprs-col{color:#ffd39a}",
        "estilo de columnas APRS",
    )
    dashboard = _replace_once(
        dashboard,
        "function filterHtml(t){return !t.enabled||t.id!=='emergencias_guardia'?'':`<section class=\"filterbox tab-panel\" data-emtab=\"propagation\"><h3>Matriz de propagación</h3><p class=\"muted\">Elige las combinaciones exactas que podrán enviarse.</p><div id=\"filters-${t.id}\" class=\"empty\">Cargando filtros…</div></section>`}",
        "function filterHtml(t){return !t.enabled||t.id!=='emergencias_guardia'?'':`<section class=\"filterbox tab-panel\" data-emtab=\"propagation\"><h3>Matriz de propagación</h3><p class=\"muted\">Define la propagación Mesh por severidad y autoriza, por categoría, las salidas secundarias APRS-IS y APRS RF.</p><div id=\"filters-${t.id}\" class=\"empty\">Cargando filtros…</div></section>`}",
        "descripción de matriz",
    )

    old_load = "async function loadFilters(){const box=document.querySelector('#filters-emergencias_guardia');try{const d=await request('/api/emergencias/filters'),levels=['low','medium','high','critical'];const head=levels.map(s=>`<th class=\"sev-${s}\">${severityLabels[s]}<br><button class=\"secondary\" onclick=\"toggleChecks('.rule-'+s,true)\">Todo</button></th>`).join('');const rows=d.categories.map(c=>`<tr><td>${esc(catLabels[c.name]||c.name)}</td>${levels.map(s=>`<td><input class=\"prop-rule rule-${s}\" data-severity=\"${s}\" data-category=\"${c.name}\" type=\"checkbox\" ${(d.rules[s]||[]).includes(c.name)?'checked':''}></td>`).join('')}</tr>`).join('');box.innerHTML=`<p class=\"hint\">Marca exactamente qué categoría puede propagarse en cada severidad. Una casilla vacía bloquea esa combinación.</p><div class=\"matrix-wrap\"><table class=\"matrix\"><thead><tr><th>Tipo</th>${head}</tr></thead><tbody>${rows}</tbody></table></div><div class=\"toolbar\"><button class=\"secondary\" onclick=\"toggleChecks('.prop-rule',false)\">Limpiar matriz</button><button onclick=\"saveFilters()\">Guardar matriz</button></div>`}catch(e){box.textContent=e.message}}"
    new_load = "async function loadFilters(){const box=document.querySelector('#filters-emergencias_guardia');try{const d=await request('/api/emergencias/filters'),levels=['low','medium','high','critical'],secondary=d.secondary_transports||{aprsis:d.categories.map(c=>c.name),aprs_rf:d.categories.map(c=>c.name)};const head=levels.map(s=>`<th class=\"sev-${s}\">${severityLabels[s]}<br><button class=\"secondary\" onclick=\"toggleChecks('.rule-'+s,true)\">Todo</button></th>`).join('');const secondaryHead=`<th class=\"aprs-col secondary-start\">APRS-IS<br><button class=\"secondary\" onclick=\"toggleChecks('.secondary-aprsis',true)\">Todo</button></th><th class=\"aprs-col\">APRS RF<br><button class=\"secondary\" onclick=\"toggleChecks('.secondary-aprs-rf',true)\">Todo</button></th>`;const rows=d.categories.map(c=>`<tr><td>${esc(catLabels[c.name]||c.name)}</td>${levels.map(s=>`<td><input class=\"prop-rule rule-${s}\" data-severity=\"${s}\" data-category=\"${c.name}\" type=\"checkbox\" ${(d.rules[s]||[]).includes(c.name)?'checked':''}></td>`).join('')}<td class=\"secondary-start\"><input class=\"secondary-rule secondary-aprsis\" data-transport=\"aprsis\" data-category=\"${c.name}\" type=\"checkbox\" ${(secondary.aprsis||[]).includes(c.name)?'checked':''}></td><td><input class=\"secondary-rule secondary-aprs-rf\" data-transport=\"aprs_rf\" data-category=\"${c.name}\" type=\"checkbox\" ${(secondary.aprs_rf||[]).includes(c.name)?'checked':''}></td></tr>`).join('');box.innerHTML=`<p class=\"hint\">Baja/Media/Alta/Crítica controlan la propagación Mesh. APRS-IS y APRS RF solo autorizan la categoría cuando el evento se clasifica como EMERG; siguen siendo obligatorios sus interruptores generales y sus niveles mínimos configurados.</p><div class=\"matrix-wrap\"><table class=\"matrix\"><thead><tr><th>Tipo</th>${head}${secondaryHead}</tr></thead><tbody>${rows}</tbody></table></div><div class=\"toolbar\"><button class=\"secondary\" onclick=\"toggleChecks('.prop-rule',false)\">Limpiar Mesh</button><button class=\"secondary\" onclick=\"toggleChecks('.secondary-rule',false)\">Limpiar APRS</button><button onclick=\"saveFilters()\">Guardar matriz</button></div>`}catch(e){box.textContent=e.message}}"
    dashboard = _replace_once(dashboard, old_load, new_load, "render de matriz")

    old_save = "async function saveFilters(){const rules={low:[],medium:[],high:[],critical:[]};document.querySelectorAll('.prop-rule:checked').forEach(x=>rules[x.dataset.severity].push(x.dataset.category));try{const d=await request('/api/emergencias/filters',{method:'PUT',body:JSON.stringify({rules})});show('emergencias_guardia',render({correcto:true,matriz:d.rules,nota:d.note}));toast('Matriz de propagación guardada');loadFilters()}catch(e){toast(e.message,true);show('emergencias_guardia',render({error:e.message}))}}"
    new_save = "async function saveFilters(){const rules={low:[],medium:[],high:[],critical:[]},secondary_transports={aprsis:[],aprs_rf:[]};document.querySelectorAll('.prop-rule:checked').forEach(x=>rules[x.dataset.severity].push(x.dataset.category));document.querySelectorAll('.secondary-rule:checked').forEach(x=>secondary_transports[x.dataset.transport].push(x.dataset.category));try{const d=await request('/api/emergencias/filters',{method:'PUT',body:JSON.stringify({rules,secondary_transports})});show('emergencias_guardia',render({correcto:true,matriz:d.rules,salidas_aprs:d.secondary_transports,nota:d.note}));toast('Matriz de propagación y salidas APRS guardada');loadFilters()}catch(e){toast(e.message,true);show('emergencias_guardia',render({error:e.message}))}}"
    return _replace_once(dashboard, old_save, new_save, "guardado de matriz")


def _route_for(app: FastAPI, path: str, method: str):
    """Localiza una ruta FastAPI existente por path y método HTTP."""
    for route in app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set()):
            return route
    raise RuntimeError(f"v7.0.50: no existe ruta {method} {path}")


def apply_aprs_category_matrix(app: FastAPI) -> FastAPI:
    """Aplica una sola vez la extensión sobre la app oficial del ControlPanel.

    Reutiliza los endpoints históricos de filtros como funciones internas. De
    este modo no se replica la lógica de validación/CLI que ya funciona: el PUT
    original sigue siendo quien modifica la matriz Baja/Media/Alta/Crítica.
    """
    if getattr(app.state, "v7050_aprs_category_matrix", False):
        return app

    get_route = _route_for(app, "/api/emergencias/filters", "GET")
    put_route = _route_for(app, "/api/emergencias/filters", "PUT")
    original_get = get_route.endpoint
    original_put = put_route.endpoint

    # El dashboard original se conserva y solo se sustituyen anclas verificadas.
    web_admin.DASHBOARD = transform_dashboard(web_admin.DASHBOARD)

    # Sustituimos únicamente las dos rutas de filtros. Middleware, autenticación,
    # resto de endpoints y estado de la app permanecen sin cambios.
    app.routes.remove(get_route)
    app.routes.remove(put_route)

    @app.get("/api/emergencias/filters")
    def get_emergency_filters_v7050() -> dict[str, Any]:
        """Devuelve la matriz histórica más las autorizaciones APRS efectivas."""
        data = dict(original_get() or {})
        data["secondary_transports"] = _read_secondary_transports()
        return data

    @app.put("/api/emergencias/filters")
    def set_emergency_filters_v7050(payload: EmergencyFiltersV7050Payload) -> dict[str, Any]:
        """Guarda matriz Mesh y, opcionalmente, las dos listas APRS.

        Clientes antiguos que no envían ``secondary_transports`` pasan
        directamente al endpoint original y no alteran las nuevas variables.
        """
        normalised_secondary = None
        previous_rules = None
        if payload.secondary_transports is not None:
            normalised_secondary = _validate_secondary_transports(payload.secondary_transports)
            previous = dict(original_get() or {})
            raw_previous_rules = previous.get("rules")
            if isinstance(raw_previous_rules, dict):
                previous_rules = {
                    severity: list(raw_previous_rules.get(severity, []))
                    for severity in ("low", "medium", "high", "critical")
                }

        base_payload = web_admin.EmergencyFiltersPayload(
            severities=payload.severities,
            categories=payload.categories,
            rules=payload.rules,
        )
        data = dict(original_put(base_payload) or {})

        if normalised_secondary is not None:
            updates = {
                APRS_CATEGORY_ENV_KEYS[transport]: ",".join(normalised_secondary[transport])
                for transport in SECONDARY_TRANSPORTS
            }
            try:
                web_admin.update_env_values(web_admin.EMERGENCIAS_ENV_FILE, updates)
            except OSError as exc:
                # La matriz Mesh ya fue guardada por el endpoint histórico. Si
                # el .env falla, intentamos restaurar inmediatamente sus reglas
                # anteriores para evitar una configuración a medias.
                rollback_error = ""
                if previous_rules is not None:
                    try:
                        original_put(web_admin.EmergencyFiltersPayload(rules=previous_rules))
                    except Exception as rollback_exc:  # noqa: BLE001 - solo diagnóstico de rollback
                        rollback_error = f"; además falló rollback Mesh: {type(rollback_exc).__name__}: {rollback_exc}"
                raise HTTPException(
                    status_code=500,
                    detail=f"No se pudieron guardar las categorías APRS: {exc}{rollback_error}",
                ) from exc
            data["secondary_transports"] = normalised_secondary
        else:
            data["secondary_transports"] = _read_secondary_transports()

        data["restart_required"] = False
        return data

    app.openapi_schema = None
    app.state.v7050_aprs_category_matrix = True
    return app
