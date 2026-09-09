# MeshNet Intelligence — IA-2E

## Objetivo

IA-2E añade un **orquestador de Emergencias en sombra** que coordina las fases
IA-2A, IA-2B, IA-2C e IA-2D sobre eventos ya normalizados, exclusivamente en
memoria y sin modificar la operativa determinista.

No introduce nuevas decisiones IA. Su función es ejecutar las capacidades ya
validadas de forma coordinada y devolver una fotografía auxiliar única que pueda
ser consumida posteriormente por IA-2F, IA-2G e IA-2H.

## Punto de partida

IA-2E parte del `main` posterior al merge de IA-2D. Reutiliza sin modificar:

- `EmergencyAIObserver` — IA-2A;
- `EmergencyAICorrelator` y `correlation_candidate` — IA-2B;
- `EmergencyAIEvolutionExplainer` — IA-2C;
- `EmergencyAISituationalBriefBuilder` — IA-2D;
- `deterministic_phase()` como única autoridad de fase.

## Activación

Requiere simultáneamente:

```env
MESHNET_AI_ENABLED=1
MESHNET_AI_EMERGENCIES_ENABLED=1
MESHNET_AI_EMERGENCY_ORCHESTRATOR_ENABLED=1
```

El flag IA-2E es opt-in y permanece `0` por defecto. Si está desactivado el
orquestador no ejecuta IA-2A/B/C/D y no puede producir llamadas externas.

IA-2B conserva además su flag propio:

```env
MESHNET_AI_CORRELATION_ENABLED=1
```

Si está desactivado, IA-2E omite completamente el tramo de correlación y continúa
con IA-2A, IA-2C e IA-2D.

## API

```python
from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergency_orchestrator import EmergencyAIShadowOrchestrator

orchestrator = EmergencyAIShadowOrchestrator(MeshNetAI.from_env())
result = orchestrator.analyze(
    event,
    change="updated",
    peer_events=current_events,
)
```

`peer_events` es únicamente una colección de eventos ya normalizados. IA-2E no
lee directamente `current`, `history`, storage ni el engine.

## Flujo

1. comprueba IA global;
2. comprueba IA de Emergencias;
3. comprueba el flag propio IA-2E;
4. obtiene la fase mediante `deterministic_phase()`;
5. ejecuta IA-2A;
6. ejecuta IA-2C;
7. aplica `correlation_candidate()` a los peers sin consumir IA;
8. ordena candidatos de forma estable y limita el máximo a 8;
9. ejecuta IA-2B solo sobre esos candidatos;
10. entrega a IA-2D los resultados obtenidos;
11. devuelve `EmergencyAIShadowResult` en memoria.

## Límite de correlaciones

Aunque el llamador solicite un valor superior, IA-2E limita los candidatos a 8.
Este límite coincide con la capacidad máxima que IA-2D incorpora a su snapshot.

El límite se aplica **antes** de llamar a `EmergencyAICorrelator`, por lo que evita
un crecimiento accidental de consumo de API cuando existen muchos eventos
actuales.

No se correlacionan automáticamente:

- el mismo objeto;
- el mismo `event_id` cuando ambos están disponibles;
- pares rechazados por `correlation_candidate()`;
- pares cuando `MESHNET_AI_CORRELATION_ENABLED=0`.

## Aislamiento de fallos

Cada fase se ejecuta de forma aislada. Una excepción de IA-2A, una correlación
individual o IA-2C no cancela necesariamente el resto del ciclo.

Estados agregados:

- `ok`: existe resultado válido y no se ha detectado fallo de componente;
- `partial`: existe al menos un resultado válido, pero otro componente ha fallado;
- `disabled`: un interruptor requerido está desactivado;
- `no_analysis`: ninguna fase produjo un resultado válido y no hubo fallo técnico;
- `error`: configuración inválida o ningún resultado válido con fallo técnico.

Los resultados `ok=False` de fases anteriores no se convierten en autoridad ni se
usan para modificar el evento.

## Resultado agregado

`EmergencyAIShadowResult` contiene:

- `event_id`;
- `deterministic_phase`;
- resultado IA-2A;
- resultado IA-2C;
- tuple de resultados IA-2B;
- resultado IA-2D;
- `status`, `error` y `duration_ms`.

El objeto es `frozen=True` y no dispone de métodos de escritura o transmisión.

## Autoridad determinista

IA-2E no puede cambiar:

- categoría;
- severidad;
- verificación;
- estado;
- fase;
- fases FIRMS `initial/growth/stable`;
- resolución;
- deduplicación/agregación;
- routing;
- prioridad;
- mensajes o salidas radio.

Para NASA FIRMS sigue siendo válido:

- `stable` no significa extinguido;
- extensión de detecciones no significa superficie afectada/quemada;
- FRP no significa intensidad del incendio;
- la fase procede exclusivamente de la lógica determinista existente.

## Fuera de alcance

IA-2E no:

- modifica `engine.py`;
- modifica `storage.py`;
- modifica `notifier.py`;
- modifica `emergency_dispatcher.py`;
- modifica `formatters.py`;
- escribe en `current`, `history` o spool;
- persiste resultados IA;
- genera notificaciones;
- transmite por MeshCore, Meshtastic, APRS o Voice RF;
- sustituye decisiones humanas o deterministas.

La persistencia separada pertenece a IA-2F.

## Pruebas

```bash
python3 -m py_compile \
  shared/meshnet_ai.py \
  shared/meshnet_ai_emergencies.py \
  shared/meshnet_ai_emergency_correlation.py \
  shared/meshnet_ai_emergency_evolution.py \
  shared/meshnet_ai_emergency_brief.py \
  shared/meshnet_ai_emergency_orchestrator.py \
  tests/test_meshnet_ai_emergency_orchestrator.py

python3 -m unittest \
  tests.test_meshnet_ai \
  tests.test_meshnet_intelligence_health \
  tests.test_meshnet_ai_tasks \
  tests.test_meshnet_ai_tasks_contract \
  tests.test_meshnet_ai_emergencies \
  tests.test_meshnet_ai_emergency_correlation \
  tests.test_meshnet_ai_emergency_correlation_contract \
  tests.test_meshnet_ai_emergency_evolution \
  tests.test_meshnet_ai_emergency_brief \
  tests.test_meshnet_ai_emergency_orchestrator \
  -v
```

La suite IA-2E usa dobles inyectables. No requiere credenciales, red ni consumo de
API.

## Criterios de aceptación

1. IA global OFF -> cero fases coordinadas y cero llamadas externas.
2. Emergencias IA OFF -> cero fases coordinadas y cero llamadas externas.
3. IA-2E OFF -> cero fases coordinadas y cero llamadas externas.
4. Correlación OFF -> IA-2B omitida sin impedir IA-2A/C/D.
5. El prefiltrado IA-2B ocurre antes de cualquier correlación IA.
6. Máximo absoluto de 8 candidatos IA-2B por ciclo.
7. Orden de candidatos estable por `event_id`.
8. Un fallo parcial no invalida otros resultados correctos.
9. El evento principal y todos los peers permanecen inmutables.
10. La fase agregada es exclusivamente determinista.
11. Ningún fichero operativo de Emergencias se modifica.
12. No existe persistencia en IA-2E.
13. Ninguna salida radio/mensajería consume el resultado.
14. Tests acumulados IA-0/1/2A/2B/2C/2D/2E superados antes de merge.
