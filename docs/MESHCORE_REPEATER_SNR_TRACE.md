# PR #216 — Implementación de TRACE manual con regreso (19/10/2026)

## Nuevo procedimiento

El código del broker ahora admite `trace_contact("ruta",
manual_path_hex=..., manual_hash_width=1|2, manual_return=True|False)`,
**sin** consultar `PATH_RESPONSE` ni exigir una ruta de salida de un contacto.
Se sigue usando la misma sesión MeshCore y el mismo evento `TRACE_DATA`
filtrado por `tag`.

El bot expone, tras desplegar la rama experimental:

```text
/traza_mc ruta aa 1
/traza_mc ruta aa,bb,cc 1 volver
/traza_mc ruta 7f15,f64e,bf27 2 volver
```

Estos hashes son **ejemplos de sintaxis**, no recomendaciones de TX ni
confirmación de que esos repetidores estén enlazados en ese orden.

Con `volver`, la ruta de entrada A,B,C se convierte en
**A,B,C,B,A**. El último repetidor de ida no se duplica. Para un único
salto A, el recorrido es A (la traza terminada se recibe por radio solo
si existe cobertura de retorno, no por garantía del software).

El resultado etiqueta `[ida]` y `[vuelta]` según el orden de la ruta
reportada, y solo muestra SNR recibido en `TRACE_DATA`. El último
valor sin hash corresponde al receptor final y no se atribuye a un
repetidor.

### Límites y seguridad

- `MESHCORE_TRACE_MANUAL_ENABLED=0` por defecto: el broker no emite
  trazas manuales hasta activar expresamente esta bandera **en un
  despliegue controlado**, revisando antes el acceso al puerto 8766.
- `ADMIN_IDS` debe incluir al operador del comando de Telegram.
- Máximo ocho hashes de **ida**, de 1 o 2 bytes cada uno, sin prefijos
  ambiguos, y un máximo de quince saltos incluyendo vuelta.
- Una sola traza simultánea por bot y 60 segundos de separación entre
  intentos. Un segundo cliente del puerto de control podría hacer
  solicitudes independientes; por eso es imprescindible restringir
  ese puerto a servicios de confianza.
- La selección automática por nombre humano no está implementada en
  esta entrega: los hashes se obtienen primero del catálogo de contactos,
  se verifican manualmente y nunca se inventan a partir de un RX ambiguo.
- El resultado offline **no prueba** conectividad RF ni compatibilidad
  de repetidores antiguos con hashes de dos bytes.

### Pruebas pendientes tras el último cambio

```bash
python -m pytest -q tests/test_meshcore_manual_trace_snr.py tests/test_meshcore_rx_signal_trace_contract.py tests/test_meshcore_sdk_2317_contract.py
```

Es preciso ejecutar de nuevo las pruebas de esta revisión; los
**27 passed** anteriores corresponden a commits previos.

## Investigación del comportamiento de la aplicación oficial (19/10/2026)

**Resultado fundamental:** la app oficial tiene una herramienta **Trace Path
manual** independiente del descubrimiento de ruta de un contacto. En ella se
indican hashes de repetidores y puede añadirse automáticamente el **camino de
regreso en orden inverso**; Liam Cottle confirmó que esa mejora estaba ya
integrada en junio de 2026:
https://github.com/meshcore-dev/MeshCore/issues/995

Protocolo/fuentes verificables:

- `CMD_SEND_TRACE_PATH` (36) envía la lista de hashes, un `tag` aleatorio y
  un `auth_code`, con respuesta `PUSH_CODE_TRACE_DATA` (0x89):
  https://github.com/meshcore-dev/MeshCore/wiki/Companion-Radio-Protocol
- El firmware `Mesh::onRecvPacket()` en `src/Mesh.cpp` añade al TRACE el
  SNR de cada repetidor y lo retransmite al siguiente hash de la secuencia.
  Si llegó al último hash, `onTraceRecv()` genera los datos de retorno:
  https://github.com/meshcore-dev/MeshCore/blob/main/src/Mesh.cpp
- El companion exporta `path_hashes`, `path_snrs` y el SNR final al host:
  https://github.com/meshcore-dev/MeshCore/blob/main/examples/companion_radio/MyMesh.cpp
- El ejemplo `meshcore_py/examples/serial_trace.py` invoca
  `send_trace(path=..., tag=...)`, espera `TRACE_DATA` filtrado por
  `tag` y resuelve cada hash contra repetidores:
  https://github.com/meshcore-dev/meshcore_py/blob/main/examples/serial_trace.py

### Implicaciones para PR #216

1. **El descubrimiento PATH_RESPONSE no es requisito previo de TRACE manual.**
   Los errores anteriores `meshcore_no_directed_path` pertenecen a una
   ruta de diagnóstico distinta de la herramienta manual de la app.
2. **La lista de ida no basta para medirla desde el emisor**; normalmente se
   exige una secuencia de retorno verificable, por ejemplo
   `R1,R2,R3,R2,R1` (representación conceptual).
3. La validación actual de hashes inequívocos **no comprueba conectividad ni
   retorno**; el comando experimental del PR todavía no reproduce por
   completo el flujo oficial y **no debe desplegarse o fusionarse**.
4. Conviene separar en el diseño un modo `manual_trace(repeater_path)`
   de las consultas de contactos `trace_contact()`; conservar el código
   antiguo del WebPanel.
5. La aplicación Flutter actual no publica su fuente íntegro; estos detalles
   están respaldados por las fuentes abiertas de protocolo/firmware y por el
   desarrollador, no por una lectura del código interno de Flutter.

# MeshCore: trazas SNR de repetidores (desarrollo experimental)

## Alcance y garantías

Este desarrollo **no modifica** la recepción pasiva MeshCore, el BBS,
Meshtastic, APRS ni la correlación de RX_LOG_DATA. Los mensajes recibidos
siguen incluyendo los hashes/nombres de los repetidores y el RSSI/SNR
**local** medido por el companion; **no contienen** SNR remoto por salto.

La única fuente admitida para SNR de repetidores es una respuesta auténtica
`TRACE_DATA` del SDK MeshCore. El comando de Telegram representa esos valores
solo cuando el broker recibe la respuesta. No inventa mediciones.

## Activación segura de rutas manuales

La petición TRACE con ruta manual está **deshabilitada por defecto** en el
broker, porque su puerto de control JSONL no es una frontera de autorización
equivalente a Telegram. Solo durante una prueba RF supervisada puede habilitarse
en el archivo `.env`:

```dotenv
MESHCORE_TRACE_MANUAL_ENABLED=1
```

Tras la prueba, restaurar `MESHCORE_TRACE_MANUAL_ENABLED=0` y recrear
únicamente los servicios necesarios durante un mantenimiento controlado. No
activar esta opción de manera permanente en instalaciones con puerto 8766
accesible desde redes no confiables.

## Comando de Telegram (exclusivo de ADMIN_IDS)

El nuevo comando **/traza_mc** (una vez desplegado, no disponible todavía en producción) genera **tráfico RF de diagnóstico** mediante
el motor MeshCore embebido, sin una segunda conexión serial/TCP/BLE al radio.

- `/traza_mc cfe25e30b3ec`: intenta `PATH_RESPONSE` y luego `TRACE_DATA`
  (requiere respuesta de descubrimiento).
- `/traza_mc cfe25e30b3ec 7f15f64e 2`: ruta dirigida **manual** con dos
  hashes de **2 bytes**, indicada explícitamente por el operador.

**La ruta manual del segundo ejemplo es ilustrativa**: los hashes por sí solos
no demuestran que esa secuencia sea un camino de TX útil para alcanzar el
destino. No se invierte automáticamente una ruta pasiva de recepción.

El broker valida la ruta hexadecimal, el ancho de hash (1 o 2 bytes), un
máximo de ocho saltos y que cada hash identifique un único contacto del
catálogo. Si el prefijo es ambiguo o desconocido, bloquea la transmisión.
El bot tiene exclusión mutua y una separación de 60 segundos entre consultas
RF aceptadas en la misma instancia.

## Comprobación previa obligatoria (sin transmitir)

El firmware MeshCore dirige un paquete TRACE siguiendo los hashes incluidos
en la ruta. El `contact_prefix` del comando actual sirve al broker para
identificar un contacto de referencia y resolver su información; **no se
codifica como dirección de destino independiente en `send_trace()`**. Por
tanto, una ruta `7f15f64e` no equivale a pedir una traza dirigida a
`RPT_URZ_SAGASTA` simplemente porque se haya indicado su prefijo.

**No probar con hashes extraídos automáticamente de una ruta RX**. Tampoco
considerar una coincidencia de hash de un byte como prueba de topología.
Antes de habilitar RF, el operador debe comprobar físicamente una secuencia
dirigida de repetidores, incluido el último salto.

En la Raspberry productiva, la siguiente comprobación es de **solo lectura**;
no hace TX, no cambia el archivo `.env` y no recrea contenedores:

```bash
cd /home/meshnet/MeshNet-Bot
docker inspect meshnet-broker --format 'Estado={{.State.Status}} Reinicios={{.RestartCount}} Imagen={{.Image}}'
docker inspect meshnet-bot --format 'Estado={{.State.Status}} Reinicios={{.RestartCount}} Imagen={{.Image}}'
docker exec meshnet-broker python -m pip show meshcore | grep -E '^(Name|Version):'
git -C /tmp/meshnet-snr-pr216-test rev-parse --short HEAD
git -C /home/meshnet/MeshNet-Bot rev-parse --short HEAD
```

El worktree de pruebas **no es la imagen Docker productiva**; los cambios
del PR no estarán disponibles en Telegram hasta una publicación y un
despliegue explícitos, sujetos a revisión. No montar el worktree directamente
sobre los scripts de un contenedor activo.

## Resultado RF de referencia: 19/10/2026

En la Raspberry de producción (broker SDK 2.3.17, versión de código
`13f7d6d`) se realizó **un único intento controlado** desde el puerto
de control del broker, con contacto `RPT_ROMAREDA_RAK`
(`daa8bd546dd8`), `discover=True` y tiempo configurado de 12 s.

Resultado observado en 3,05 s:

```text
ok=False
meshcore_trace_failed: RuntimeError: meshcore_trace_failed:
RuntimeError: meshcore_no_directed_path
```

Este error se produce en `trace_contact()` cuando el descubrimiento
termina sin proporcionar **bytes de ruta no vacíos**. Se produce **antes
del envío de `TRACE_DATA`**. No demuestra un error de transmisión TRACE,
un SNR malo ni que el contacto esté desconectado.

**Importante:** una respuesta de descubrimiento de **cero saltos** puede
ser legítima para un nodo directamente alcanzable. El código actual no
distingue una ruta válida de cero saltos de la ausencia de ruta y utiliza
`meshcore_no_directed_path` en ambos casos. Además, la API
`send_trace()` no incorpora el `contact_prefix` como destinatario
independiente: no está autorizado convertir un descubrimiento vacío en
`send_trace(path=b"")` automáticamente.

### Puerta de validación

Antes de otra prueba RF, revisar el contenido real del
`PATH_RESPONSE` del SDK 2.3.17 (`out_path_len`,
`out_path_hash_len`, `out_path`) y distinguir el formato de ruta
directa válida del de ruta desconocida. Mantener el PR experimental
y la ruta manual deshabilitada hasta disponer de evidencia de TX real.

## Resultados

Cuando `TRACE_DATA` contenga la secuencia real, Telegram mostrará
el nombre del salto y su SNR, o «no disponible» si falta el valor. Las
respuestas `PATH_RESPONSE` ausentes y los timeouts `TRACE_DATA`
son errores explícitos, **no** pruebas positivas.

El descubrimiento hacia `RPT_URZ_SAGASTA` anteriormente respondió
`meshcore_path_discovery_no_response` bajo SDK 2.3.17; por eso se habilita
**una ruta manual explícita** como alternativa, no como sustituto
automático de la topología real.

## Pruebas y despliegue

Ejecutar pruebas aisladas, sin hardware radio:

```bash
python -m pytest -q tests/test_meshcore_manual_trace_snr.py
python -m pytest -q tests/test_meshcore_rx_signal_trace_contract.py
python -m pytest -q tests/test_meshcore_sdk_2317_contract.py
```

El retorno del SDK para `TRACE_DATA` debe respetar el ancho de hash
solicitado y aportar al menos un salto explícito. Si falta información o
los hashes no cuadran, el broker devuelve error antes de mostrar SNR falsos.

**No fusionar ni desplegar** hasta disponer de pruebas positivas, revisión
de seguridad del comando y una prueba RF controlada con una ruta conocida.
La investigación del HTTP 503 del WebPanel/BacklogServer continúa aplazada.
