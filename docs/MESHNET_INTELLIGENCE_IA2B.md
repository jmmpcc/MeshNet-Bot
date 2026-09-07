# MeshNet Intelligence — Fase IA-2B

## Objetivo

IA-2B añade **correlación entre fuentes en modo sombra** sobre eventos ya
normalizados por Emergencias. Su finalidad es estudiar si dos observaciones de
fuentes distintas pueden describir el mismo incidente, aportar contexto mutuo o no
tener relación, sin modificar ninguna decisión operativa.

Ejemplos de pares que IA-2B puede estudiar en fases posteriores de integración:

- NASA FIRMS + AEMET;
- CHE/fuentes hidrológicas + DATEX2;
- AEMET + DATEX2;
- otras fuentes normalizadas que se incorporen al sistema.

## Autoridad determinista

IA-2B **no sustituye** ninguna lógica existente. En `engine.py`, `_merge_source()`
continúa fusionando el estado únicamente por `event_id` y por fuente. IA-2B no
escribe en `current`, `history`, `state` ni ningún spool.

No modifica:

- `Event.category`;
- `Event.severity`;
- `Event.verification`;
- `Event.status`;
- fases NASA FIRMS;
- deduplicación/agregación existente;
- routing o filtros;
- `notifier.py`;
- `emergency_dispatcher.py`;
- MeshCore, Meshtastic, APRS-IS, APRS RF o Voice RF.

La fase no está conectada a ningún flujo automático.

## Activación

El correlador requiere simultáneamente:

```env
MESHNET_AI_ENABLED=1
MESHNET_AI_EMERGENCIES_ENABLED=1
MESHNET_AI_CORRELATION_ENABLED=1
```

Los flags siguen siendo opcionales y desactivados por defecto. Si cualquiera de
ellos está desactivado, no se llama al proveedor.

## Dos capas de seguridad

### 1. Prefiltrado determinista

`correlation_candidate(event_a, event_b, ...)` no usa IA. Únicamente decide si el
par merece ser comparado en sombra.

Reglas iniciales:

1. los eventos deben ser distintos;
2. las fuentes deben ser distintas;
3. no se estudian eventos terminales;
4. con coordenadas completas, la distancia debe quedar dentro del límite;
5. sin coordenadas completas se exige coincidencia geográfica por municipio o
   provincia;
6. cuando ambas fechas son válidas, la diferencia debe quedar dentro del límite
   temporal;
7. superar este filtro **no significa** que exista relación.

Valores iniciales por defecto:

- `max_distance_km = 50.0`;
- `max_time_hours = 24.0`.

Son parámetros del llamador, no reglas operativas del sistema de Emergencias.

### 2. Interpretación IA

Solo un candidato determinista puede llegar al proveedor IA. El modelo debe elegir
exactamente una de estas relaciones:

- `same_incident`: probablemente dos fuentes describen el mismo hecho;
- `contextual`: existe contexto o posible consecuencia relacionada, pero no es el
  mismo hecho;
- `unrelated`: los datos disponibles no sostienen relación;
- `uncertain`: no hay evidencia suficiente para clasificar con seguridad.

Ninguno de estos valores tiene efecto operativo en IA-2B.

## API

```python
from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergency_correlation import EmergencyAICorrelator

correlator = EmergencyAICorrelator(MeshNetAI.from_env())
result = correlator.correlate(event_a, event_b)

if result.ok:
    print(result.relation)
    print(result.explanation)
```

`result.ok=False` significa ignorar completamente el resultado.

## Payload al proveedor

IA-2B reutiliza el payload mínimo y saneado de IA-2A para cada evento:

- `event_id` y fuente;
- categoría, severidad, verificación y estado;
- fase determinista disponible;
- título y descripción;
- carretera, municipio y provincia;
- latitud/longitud normalizadas;
- fechas normalizadas.

No se transmite el diccionario `metadata` completo.

Además se remiten únicamente las señales deterministas del candidato:

- distancia calculada cuando existe;
- diferencia temporal cuando existe;
- razones de aceptación del prefiltrado.

## Contrato de respuesta

El proveedor debe devolver exclusivamente:

```json
{
  "relation": "contextual",
  "explanation": "Explicación factual basada únicamente en los dos eventos.",
  "confidence": 0.82
}
```

Validaciones:

- JSON inválido -> `ok=False`;
- objeto JSON no válido -> `ok=False`;
- `relation` fuera del conjunto permitido -> `ok=False`;
- `explanation` no textual o vacía -> `ok=False`;
- `confidence` no numérica, `NaN`, infinita u overflow -> `ok=False`;
- valores finitos de `confidence` se limitan a `0..1`;
- fallo/timeout del proveedor -> fallback seguro;
- ambos eventos permanecen inmutables.

## Qué NO hace IA-2B

IA-2B no recorre automáticamente `current`, no crea grupos persistentes, no cambia
`verification` a `confirmed_multi_source`, no resuelve eventos y no genera mensajes.
Es una capa de análisis aislada que deberá demostrarse fiable antes de cualquier
integración posterior.

## Pruebas

```bash
python3 -m py_compile \
  shared/meshnet_ai.py \
  shared/meshnet_ai_emergencies.py \
  shared/meshnet_ai_emergency_correlation.py \
  tests/test_meshnet_ai_emergency_correlation.py

python3 -m unittest \
  tests.test_meshnet_ai \
  tests.test_meshnet_intelligence_health \
  tests.test_meshnet_ai_tasks \
  tests.test_meshnet_ai_tasks_contract \
  tests.test_meshnet_ai_emergencies \
  tests.test_meshnet_ai_emergency_correlation \
  -v
```

La suite usa un proveedor falso y no consume API.

## Criterios de aceptación

1. IA global OFF -> cero llamadas externas.
2. Emergencias IA OFF -> cero llamadas externas.
3. Correlación IA OFF -> cero llamadas externas.
4. Par no candidato -> cero llamadas externas.
5. La selección de candidato es determinista y no implica identidad/causalidad.
6. Solo se comparan fuentes distintas.
7. Distancia/tiempo fuera de límites impiden la llamada IA.
8. Payload sin `metadata` completa.
9. Contrato de respuesta cerrado y validado estrictamente.
10. Ambos eventos permanecen inmutables.
11. Ningún fichero operativo de Emergencias se modifica.
12. Ninguna salida de radio/mensajería consume IA-2B.
