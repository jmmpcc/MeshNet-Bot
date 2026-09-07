# MeshNet Intelligence — IA-2C

## Objetivo

IA-2C añade una explicación IA **en sombra** de la evolución de un incidente ya
clasificada por la lógica determinista de MeshNet-Bot.

La IA no decide fases, no detecta crecimiento, no confirma incendios, no resuelve
incidentes y no modifica ninguna salida operativa.

## Autoridad determinista

La fase procede de `shared.meshnet_ai_emergencies.deterministic_phase()`.

Para NASA FIRMS, esa función reutiliza exclusivamente `metadata['firms_phase']`
cuando la fuente es `nasa_firms`. Las fases `initial`, `growth` y `stable` siguen
siendo decididas por `FirmsTrackedSource`; `resolved` continúa dependiendo del
estado/cambio determinista existente.

IA-2C solo explica esa fase.

## Señales FIRMS utilizadas

El snapshot mínimo puede incluir únicamente:

- `firms_phase`;
- `growth_reasons`;
- primera/última detección;
- número de pasadas;
- detecciones previous/latest/peak;
- FRP previous/latest/peak;
- extensión observada previous/latest/peak.

La metadata completa nunca se envía al proveedor.

## Semántica de seguridad

- `growth_reasons` ya ha sido calculado determinísticamente.
- `stable` significa que esa pasada no presenta crecimiento significativo según
  los umbrales del tracker; **no significa incendio extinguido**.
- `cluster_extent_km`/`latest_extent_km` es extensión observada de detecciones
  satelitales; **no es superficie quemada ni afectada**.
- `resolved` solo puede explicarse si la fase determinista recibida es `resolved`.
- La explicación no puede contener órdenes operativas.

## Activación

Requiere:

```text
MESHNET_AI_ENABLED=1
MESHNET_AI_EMERGENCIES_ENABLED=1
```

No se añade un nuevo feature flag porque IA-2C sigue siendo una llamada explícita
del módulo de Emergencias y no está conectada automáticamente a `engine`,
`notifier`, `dispatcher`, broker ni radio.

## API

```python
from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergency_evolution import EmergencyAIEvolutionExplainer

result = EmergencyAIEvolutionExplainer(MeshNetAI.from_env()).explain(event)
```

Resultado:

- `ok`
- `phase` — fase determinista, nunca generada por IA
- `explanation`
- `confidence`
- `status`
- `error`
- `duration_ms`

## Contrato del proveedor

El proveedor debe devolver exclusivamente JSON:

```json
{
  "explanation": "...",
  "confidence": 0.82
}
```

`confidence` debe ser un número JSON real, finito y dentro de `[0, 1]`.
Strings numéricos, booleanos, NaN, infinitos y valores fuera de rango se rechazan.

## Fuera de alcance

IA-2C no:

- modifica `Event`;
- recalcula `firms_phase`;
- altera `growth_reasons`;
- fusiona o separa incidentes;
- modifica severidad/verificación/categoría/estado;
- escribe en storage;
- genera notificaciones;
- transmite por MeshCore, Meshtastic, APRS o Voice RF;
- recorre automáticamente los eventos actuales.

## Pruebas

La suite verifica:

- reutilización de la fase determinista;
- exclusión de metadata privada;
- aislamiento de `firms_phase` a NASA FIRMS;
- flags OFF sin consumo del proveedor;
- fase desconocida sin consumo del proveedor;
- contrato JSON cerrado;
- tipos estrictos de `confidence`;
- rechazo de NaN/infinito/fuera de rango;
- semántica `stable != extinguido`;
- inmutabilidad del evento.
