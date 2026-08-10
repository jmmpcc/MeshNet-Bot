# CHANGELOG v7.0.43 — 2026-08-10

## Objetivo

Ampliar `emergencias_guardia` con nuevas fuentes oficiales sin modificar el flujo de notificación validado en v7.0.42 y reflejar esas fuentes en MeshNet ControlPanel.

## Compatibilidad con el AEMET existente del broker

La revisión de v7.0.42 confirmó que MeshNet-Bot ya dispone de un subsistema AEMET maduro en `source/aemet_alerts.py`, ejecutado como tarea dinámica `task_type=aemet_alert` desde `source/broker_task.py`.

Ese flujo existente continúa siendo la **autoridad operativa de AEMET** porque ya implementa RSS/Atom/CAP, filtro territorial y por fenómeno, niveles amarillo/naranja/rojo, deduplicación persistente por ámbito y transporte, cooldown, repetición, formato MeshCore y confirmación de deduplicación únicamente después de una transmisión correcta.

Por este motivo, el conector CAP de `emergencias_guardia` **no se ejecuta en paralelo**. Su configuración incluye `disabled_if_env_enabled=AEMET_ALERTS_ENABLED`; mientras el subsistema histórico esté activo, Emergencias registra la fuente como cedida al propietario externo y no genera eventos AEMET.

El conector `aemet_cap` queda disponible únicamente como fallback o futura migración controlada si se establece explícitamente `AEMET_ALERTS_ENABLED=0`.

## AEMET CAP — fallback/migración

- Conector `aemet_cap` para mensajes CAP 1.2 mediante AEMET OpenData.
- Resuelve correctamente el segundo salto JSON `datos` de OpenData.
- Normaliza `identifier`, `msgType`, severidad, fenómeno, vigencia, área, certeza y urgencia.
- Clasifica avisos en `storm`, `snow`, `strong_wind`, `extreme_temperature` y `flood`.
- CAP `Cancel` se convierte en `resolved`.
- Requiere `AEMET_API_KEY` únicamente cuando se utiliza como fallback.
- No se ejecuta mientras `AEMET_ALERTS_ENABLED` mantenga activo el sistema AEMET del broker.

## CHE / SAIH Ebro — corrección tras validación real

La prueba real en Raspberry demostró que `https://cph.chebro.es/es/notas-de-prensa-rss`, pese a su nombre, devuelve una página HTML del portal CHE y no un feed RSS/XML apto para consumo estructurado.

Por seguridad y estabilidad:

- `che_saih` queda **no operativa** en v7.0.43;
- se elimina esa URL como endpoint de adquisición y se conserva solo como `reference_url` informativa;
- el motor bloquea cualquier intento de ejecución con `skipped=not_operational` y un motivo explícito;
- no se elimina el parser `CheRssSource`, para poder reutilizarlo si CHE publica posteriormente un feed estructurado compatible;
- no se implementa scraping HTML silencioso;
- la integración queda pendiente de un endpoint/API/feed público estructurado y estable o de una futura integración específica con datos hidrológicos SAIH correctamente definida.

La investigación oficial confirma que SAIH Ebro publica datos de nivel/caudal, series quinceminutales y predicciones hidrológicas, además de niveles de aviso amarillo/naranja/rojo, pero esto requiere un diseño específico distinto de tratar la página de comunicaciones como RSS.

## Limpieza del estado de fuentes

Se corrige un defecto detectado durante las pruebas AEMET: al pasar de `AEMET_ALERTS_ENABLED=1` a `0`, un error nuevo como `AEMET_API_KEY no configurada` podía conservar en `state.json` los campos transitorios `skipped=external_owner` y `reason` de la ejecución anterior.

Ahora cada ejecución sustituye los campos transitorios incompatibles. En caso de fallo solo se conservan datos históricos útiles de la última ejecución correcta (`last_success`, `records`, `accepted`, `not_modified`) junto con el error actual.

## ControlPanel

- `Fuentes y cobertura` mantiene AEMET CAP como fallback y CHE/SAIH como fuente conocida de la arquitectura.
- AEMET CAP no se ejecutará si el AEMET histórico del broker está activo.
- CHE/SAIH queda protegida a nivel de motor como no operativa hasta disponer de endpoint estructurado válido.
- Se conserva la configuración de categorías, provincias, radio, matriz de propagación y canales.

## Fuentes no incorporadas todavía

- CHE / SAIH Ebro: pendiente de endpoint estructurado operativo o integración hidrológica específica.
- RAN / Protección Civil: pendiente de API pública estructurada y estable.
- 112 Aragón: no se integra mediante scraping HTML; pendiente de feed/API oficial estable.

## Compatibilidad

- No se modifica `source/aemet_alerts.py` ni el scheduler AEMET existente.
- No se modifica el dispatcher APRS RF/APRS-IS validado en v7.0.42.
- No se modifica la deduplicación ni el bypass de `MIN_INTERVAL` para estados terminales.
- No se modifica el modelo `Event`.

## Validación

`tests/test_emergency_sources_v7043.py` cubre:

- normalización CAP AEMET;
- CAP `Cancel` -> `resolved`;
- segundo salto AEMET OpenData `datos`;
- filtrado territorial CAP;
- cesión automática al AEMET histórico;
- limpieza de estado transitorio entre ejecuciones;
- bloqueo explícito de CHE mientras no exista endpoint estructurado;
- parser CHE conservado para un futuro feed estructurado;
- soporte de avisos multizona.
