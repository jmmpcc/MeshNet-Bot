# v7.0.46 — Integridad de URL y diagnóstico APRS-IS largo

## Objetivo

Evitar enlaces geográficos inutilizables cuando un mensaje de Emergencias se compacta y preparar una prueba controlada para medir qué longitud conserva realmente APRS-IS/aprs.fi, sin alterar los boletines `BLNx`, APRS RF ni las salidas automáticas existentes.

## Cambios

- `compact_messages()` conserva la URL de Google Maps únicamente si cabe completa dentro del presupuesto UTF-8.
- Si la URL no cabe, se elimina completa antes de recortar ubicación o texto.
- Nunca se genera deliberadamente una URL parcial como `https://maps.`.
- Los boletines públicos `BLNx` continúan usando su flujo y límites actuales.
- APRS RF permanece sin cambios.
- Se añade el modo UDP `aprsis_long_test` para una prueba manual exclusivamente APRS-IS.
- `APRSIS_LONG_TEST_ENABLED=0` por defecto: instalar la versión no genera tráfico adicional.
- `APRSIS_LONG_TEST_MAX_CHARS=400` permite probar textos superiores a 67 caracteres sin modificar la lógica automática de Emergencias.
- El modo largo reutiliza la conexión APRS-IS existente y no abre KISS ni transmite RF.

## Validación

Se han ejecutado conjuntamente las pruebas de v7.0.46 y las regresiones de las fases APRS-IS/emergencias anteriores:

- compilación Python de `formatters.py`, `emergency_dispatcher.py`, `meshtastic_to_aprs.py` y ControlPanel;
- integridad de URL cuando cabe;
- eliminación completa de URL cuando no cabe;
- diagnóstico largo desactivado por defecto;
- conservación de texto y URL de más de 67 caracteres en la línea entregada al socket APRS-IS;
- boletines `BLNx`, grupos, deduplicación y estados terminales;
- APRS RF y dispatcher;
- fuentes v7.0.43;
- georesolución provincial v7.0.45;
- `git diff --check`.

Resultado CI: **44 tests + 10 subtests superados**.

## Pendiente de validación operativa

La salida automática APRS-IS ampliada no se activa en esta fase. Primero debe ejecutarse el diagnóstico real contra APRS-IS/aprs.fi para comprobar qué parte del texto largo representa el cliente. Solo después se decidirá el formato definitivo de la segunda salida ampliada.
