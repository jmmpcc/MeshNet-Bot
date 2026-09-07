# MeshNet Intelligence — Fase IA-2A

## Objetivo

IA-2A introduce un **observador IA en sombra** para eventos de Emergencias ya
normalizados. Su finalidad es obtener un resumen auxiliar y notas de incertidumbre
sin modificar ninguna decisión operativa existente.

## Principio de compatibilidad

IA-2A no modifica ni sustituye:

- `Event.category`, `Event.severity`, `Event.verification` o `Event.status`;
- `engine.py` ni su detección `new / updated / resolved`;
- el tracking determinista NASA FIRMS (`initial / growth / stable`);
- deduplicación, filtros, routing o spool incremental;
- `formatters.py`, `notifier.py` o `emergency_dispatcher.py`;
- transmisiones MeshCore, Meshtastic, APRS-IS, APRS RF o Voice RF.

La fase no está conectada todavía a ningún flujo automático.

## Activación

El observador requiere simultáneamente:

```env
MESHNET_AI_ENABLED=1
MESHNET_AI_EMERGENCIES_ENABLED=1
```

Ambos flags siguen siendo `0` por defecto. Si cualquiera está desactivado, no se
realiza ninguna llamada al proveedor.

## API

```python
from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergencies import EmergencyAIObserver

observer = EmergencyAIObserver(MeshNetAI.from_env())
analysis = observer.analyze_event(event, change="updated")

if analysis.ok:
    print(analysis.summary)
```

`analysis.ok=False` significa ignorar el análisis por completo. La ruta clásica
continúa sin cambios.

## Fase determinista

`deterministic_phase(event, change)` deriva la fase sin IA:

1. estado terminal o `change=resolved` -> `resolved`;
2. únicamente la fuente `nasa_firms` reutiliza `metadata['firms_phase']` cuando
   contiene `initial`, `growth` o `stable`;
3. el resto de fuentes conserva `new` o `updated`, incluso si una metadata ajena
   contiene por error una clave `firms_phase`;
4. falta de datos -> `unknown`.

El modelo recibe esa fase como dato autoritativo y no puede sustituirla.

## Datos enviados al proveedor

IA-2A construye un payload mínimo con:

- `event_id`, fuente, categoría, severidad, verificación y estado;
- cambio y fase deterministas;
- título y descripción;
- carretera, municipio y provincia;
- latitud y longitud normalizadas cuando están disponibles;
- fechas de inicio y actualización.

No se envía el diccionario `metadata` completo. Esto evita transmitir campos
auxiliares innecesarios y reduce el acoplamiento con conectores concretos.

## Contrato de respuesta

El proveedor debe devolver únicamente JSON:

```json
{
  "summary": "Resumen factual del evento",
  "notes": "Incertidumbres o datos faltantes",
  "confidence": 0.82
}
```

Protecciones:

- JSON inválido -> `ok=False`;
- objeto sin resumen -> `ok=False`;
- `confidence` se limita a `0.0..1.0`;
- resumen y notas respetan límites exactos del llamador;
- cualquier timeout o fallo del proveedor mantiene fallback seguro;
- el evento original no se modifica.

## Qué NO hace IA-2A

IA-2A no correlaciona todavía eventos entre fuentes, no decide si dos eventos son
el mismo incidente y no evalúa aumento/mejora/resolución por sí misma. Esas
capacidades pertenecen a fases posteriores y deberán mantener las reglas
deterministas como autoridad.

## Pruebas

```bash
python3 -m py_compile \
  shared/meshnet_ai.py \
  shared/meshnet_ai_emergencies.py \
  tests/test_meshnet_ai_emergencies.py

python3 -m unittest \
  tests.test_meshnet_ai \
  tests.test_meshnet_intelligence_health \
  tests.test_meshnet_ai_tasks \
  tests.test_meshnet_ai_tasks_contract \
  tests.test_meshnet_ai_emergencies \
  -v
```

La suite IA-2A usa un proveedor falso: no requiere credenciales, red ni consumo de
API.

## Criterios de aceptación

1. IA global OFF -> cero llamadas externas.
2. Emergencias IA OFF -> cero llamadas externas.
3. Límites inválidos -> cero llamadas externas.
4. Proveedor caído -> `ok=False` sin excepción operativa.
5. JSON inválido o resumen vacío -> rechazo.
6. FIRMS conserva su fase determinista existente exclusivamente para `nasa_firms`.
7. Ninguna otra fuente puede heredar `initial / growth / stable` por una metadata ajena.
8. Eventos terminales conservan `resolved` como autoridad.
9. El evento recibido permanece inmutable.
10. Ningún fichero operativo de Emergencias resulta modificado en IA-2A.
11. Ninguna salida radio o de mensajería consume el resultado de IA-2A.
