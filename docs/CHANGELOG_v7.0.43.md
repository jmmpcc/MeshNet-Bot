# CHANGELOG v7.0.43 — 2026-08-10

## Objetivo

Ampliar `emergencias_guardia` con nuevas fuentes oficiales sin modificar el flujo de notificación validado en v7.0.42 y reflejar esas fuentes en MeshNet ControlPanel.

## Fuentes incorporadas

### AEMET CAP

- Nuevo conector `aemet_cap` para mensajes CAP 1.2.
- Normaliza `identifier`, `msgType`, severidad, fenómeno, vigencia, área, certeza y urgencia.
- Clasifica avisos en las categorías existentes: `storm`, `snow`, `strong_wind`, `extreme_temperature` y `flood`.
- Los CAP de cancelación se convierten en estado `resolved`.
- La fuente permanece desactivada por defecto.
- Requiere `AEMET_API_KEY` cuando se usa el endpoint OpenData configurado.

### CHE / SAIH Ebro

- Nuevo conector `che_rss` sobre el RSS oficial de comunicaciones de la Confederación Hidrográfica del Ebro.
- Descarta notas no operativas y conserva únicamente comunicaciones con semántica hidrológica: crecidas, avenidas, cauces, barrancos, inundaciones o vigilancia SAIH.
- Normaliza esos avisos como categoría `flood` y verificación `official`.
- La fuente permanece desactivada por defecto.

## ControlPanel

- `Fuentes y cobertura` incorpora `AEMET CAP` y `CHE / SAIH Ebro` junto a DGT, Zaragoza, IGN y FIRMS.
- Se añade gestión segura de `AEMET_API_KEY` igual que la MAP_KEY de FIRMS: la clave nunca se devuelve al navegador y un campo vacío conserva la existente.
- El resumen operativo incluye las nuevas fuentes.
- Se conserva la configuración de categorías, provincias, radio, matriz de propagación y canales.

## Fuentes no incorporadas todavía

- RAN / Protección Civil: fuente oficial de gran interés, pero no se ha localizado una API pública estructurada y estable apta para integración directa.
- 112 Aragón: la web publica alertas y avisos, pero no se integra mediante scraping HTML. Se incorporará únicamente si se confirma un feed/API oficial estable.

## Compatibilidad

- No se modifica el dispatcher APRS RF/APRS-IS validado en v7.0.42.
- No se modifica la deduplicación ni el bypass de `MIN_INTERVAL` para estados terminales.
- No se modifica el modelo `Event`; los nuevos conectores reutilizan los campos y categorías existentes.
- Todas las nuevas fuentes llegan desactivadas por defecto.

## Validación añadida

`tests/test_emergency_sources_v7043.py` cubre:

- normalización de un aviso CAP meteorológico;
- conversión CAP `Cancel` a `resolved`;
- filtrado CHE para impedir que notas no hidrológicas se conviertan en emergencias.
