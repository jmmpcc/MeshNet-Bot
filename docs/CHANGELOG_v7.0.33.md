# MeshNet-Bot v7.0.33

## Fase 2B — Emergency Dispatcher y árbitro RF base

- Se añade `emergency_dispatcher.py` para centralizar salidas secundarias de emergencias.
- APRS-IS sigue ejecutándose únicamente después de una entrega Mesh correcta.
- Se conserva `aprsis_bulletins` en la respuesta por compatibilidad.
- Se añade `secondary_outputs` con resultados normalizados de APRS-IS y voz.
- La voz RF queda declarada pero bloqueada: no accede a audio, PTT ni radio.
- Se añade `rf_manager.py` como exclusión mutua persistente para un único transmisor.
- El árbitro RF permanece desactivado y no interviene todavía en APRS.
- No se modifica el gateway APRS ni el flujo Mesh existente.
