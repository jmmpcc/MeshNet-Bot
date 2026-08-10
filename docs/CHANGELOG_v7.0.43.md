# CHANGELOG v7.0.43 — 2026-08-10

## Objetivo

Ampliar `emergencias_guardia` con nuevas fuentes oficiales sin modificar el flujo de notificación validado en v7.0.42 y reflejar esas fuentes en MeshNet ControlPanel.

## Compatibilidad con el AEMET existente del broker

La revisión de v7.0.42 confirmó que MeshNet-Bot ya dispone de un subsistema AEMET maduro en `source/aemet_alerts.py`, ejecutado como tarea dinámica `task_type=aemet_alert` desde `source/broker_task.py`.

Ese flujo existente continúa siendo la **autoridad operativa de AEMET** porque ya implementa:

- RSS/Atom/CAP;
- filtro por zona, provincia y comunidad;
- niveles amarillo, naranja y rojo;
- filtro por fenómeno;
- deduplicación persistente por ámbito y transporte;
- `cooldown`, repetición y máximo de avisos por ejecución;
- confirmación de deduplicación únicamente después de una transmisión correcta;
- formato compacto específico para MeshCore;
- conversión opcional de nivel rojo a `EMERGENCIA AEMET`;
- envío mediante los transportes existentes del scheduler.

Por este motivo, el nuevo conector CAP de `emergencias_guardia` **no se ejecuta en paralelo**. Su configuración incluye `disabled_if_env_enabled=AEMET_ALERTS_ENABLED` y, mientras el subsistema histórico esté activo —incluido el comportamiento por defecto—, el recolector de Emergencias registra la fuente como cedida al propietario externo y no descarga ni genera eventos AEMET.

El conector `aemet_cap` queda disponible únicamente como fallback o futura migración controlada si se establece explícitamente `AEMET_ALERTS_ENABLED=0`.

## Fuentes incorporadas

### AEMET CAP — fallback/migración

- Nuevo conector `aemet_cap` para mensajes CAP 1.2.
- Normaliza `identifier`, `msgType`, severidad, fenómeno, vigencia, área, certeza y urgencia.
- Clasifica avisos en las categorías existentes: `storm`, `snow`, `strong_wind`, `extreme_temperature` y `flood`.
- Los CAP de cancelación se convierten en estado `resolved`.
- La fuente permanece desactivada por defecto.
- Requiere `AEMET_API_KEY` cuando se usa el endpoint OpenData configurado.
- No se ejecuta mientras `AEMET_ALERTS_ENABLED` mantenga activo el sistema AEMET del broker.

### CHE / SAIH Ebro

- Nuevo conector `che_rss` sobre el RSS oficial de comunicaciones de la Confederación Hidrográfica del Ebro.
- Descarta notas no operativas y conserva únicamente comunicaciones con semántica hidrológica: crecidas, avenidas, cauces, barrancos, inundaciones o vigilancia SAIH.
- Normaliza esos avisos como categoría `flood` y verificación `official`.
- Conserva todas las provincias detectadas en avisos multizona para que el filtrado por cobertura no dependa únicamente de la provincia principal.
- La fuente permanece desactivada por defecto.

## ControlPanel

- `Fuentes y cobertura` incorpora `AEMET CAP` y `CHE / SAIH Ebro` junto a DGT, Zaragoza, IGN y FIRMS.
- AEMET CAP se considera una fuente de fallback: aunque se seleccione, el motor no la ejecutará si el AEMET histórico del broker está activo.
- Se añade gestión segura de `AEMET_API_KEY` igual que la MAP_KEY de FIRMS: la clave nunca se devuelve al navegador y un campo vacío conserva la existente.
- El resumen operativo incluye las nuevas fuentes.
- Se conserva la configuración de categorías, provincias, radio, matriz de propagación y canales.

## Fuentes no incorporadas todavía

- RAN / Protección Civil: fuente oficial de gran interés, pero no se ha localizado una API pública estructurada y estable apta para integración directa.
- 112 Aragón: la web publica alertas y avisos, pero no se integra mediante scraping HTML. Se incorporará únicamente si se confirma un feed/API oficial estable.

## Compatibilidad

- No se modifica `source/aemet_alerts.py` ni el scheduler AEMET existente.
- No se modifica el dispatcher APRS RF/APRS-IS validado en v7.0.42.
- No se modifica la deduplicación ni el bypass de `MIN_INTERVAL` para estados terminales.
- No se modifica el modelo `Event`; los nuevos conectores reutilizan los campos y categorías existentes.
- Todas las nuevas fuentes llegan desactivadas por defecto.

## Validación añadida

`tests/test_emergency_sources_v7043.py` cubre:

- normalización de un aviso CAP meteorológico;
- conversión CAP `Cancel` a `resolved`;
- segundo salto AEMET OpenData `datos`;
- filtrado territorial CAP;
- bloqueo automático de AEMET CAP mientras el AEMET histórico está activo;
- filtrado CHE para impedir que notas no hidrológicas se conviertan en emergencias;
- avisos CHE que afectan a varias provincias.
