# MeshNet Intelligence — IA-2D

## Objetivo

IA-2D añade un **brief situacional en sombra** que sintetiza información ya
existente sin crear decisiones nuevas.

La fase combina el evento determinista principal con resultados IA anteriores que
ya hayan superado sus propios contratos:

- IA-2A: resumen/notas del evento;
- IA-2B: correlaciones entre fuentes;
- IA-2C: explicación de evolución.

IA-2D no modifica prioridad, severidad, verificación, fase, estado, routing,
notificaciones ni transmisiones.

## Principio de autoridad

La fase del evento procede exclusivamente de
`shared.meshnet_ai_emergencies.deterministic_phase()`.

Los componentes IA son auxiliares. Un resultado con `ok=False`, una fase
incompatible o una correlación no válida se excluyen antes de llamar al proveedor
IA-2D.

Si no existe ningún componente sombra válido, IA-2D devuelve `ok=False` y no
consume API. De este modo no duplica la función de resumen de IA-2A.

## API

```python
from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_emergency_brief import EmergencyAISituationalBriefBuilder

builder = EmergencyAISituationalBriefBuilder(MeshNetAI.from_env())
result = builder.build(
    event,
    analysis=analysis_ia2a,
    evolution=evolution_ia2c,
    correlations=correlations_ia2b,
)
```

## Snapshot mínimo

`deterministic_brief_snapshot()` incluye:

- evento saneado mediante `_safe_event_payload()`;
- fase determinista;
- IA-2A solo cuando `ok=True` y su fase no contradice la principal;
- IA-2C solo cuando `ok=True` y su fase coincide exactamente;
- hasta ocho correlaciones IA-2B `ok=True`, candidatas y con relación válida.

No se transmite metadata completa ni objetos originales.

## Contrato de respuesta

El proveedor debe devolver exclusivamente:

```json
{
  "brief": "Síntesis factual de la situación observada.",
  "uncertainties": "Incertidumbres o límites de la información disponible.",
  "confidence": 0.84
}
```

Reglas:

- `brief` y `uncertainties` deben ser cadenas JSON reales;
- `brief` no puede estar vacío;
- `confidence` debe ser número JSON real, finito y dentro de `[0,1]`;
- strings numéricos, booleanos, NaN, infinitos y valores fuera de rango se rechazan;
- el recorte de texto reutiliza `_fit_text()` de IA-2A para evitar palabras/frases
  cortadas;
- para NASA FIRMS se reutiliza la barrera determinista de IA-2C contra
  sobreafirmaciones de superficie afectada/quemada o intensidad del incendio.

## Semántica de seguridad

IA-2D no puede:

- decidir prioridad o severidad;
- confirmar incidentes;
- convertir `same_incident` de IA-2B en confirmación operativa;
- resolver incidentes;
- recomendar evacuaciones, envíos o actuaciones;
- convertir `stable` FIRMS en extinguido;
- convertir extensión FIRMS en superficie afectada/quemada;
- convertir FRP en intensidad del incendio.

## Activación

Requiere simultáneamente:

```env
MESHNET_AI_ENABLED=1
MESHNET_AI_EMERGENCIES_ENABLED=1
```

No se añade feature flag nuevo porque IA-2D sigue siendo una llamada explícita y
no está conectada automáticamente a ningún flujo.

## Fuera de alcance

IA-2D no:

- recorre `current` automáticamente;
- persiste briefs;
- modifica `Event`;
- altera engine/storage/notifier/dispatcher;
- genera mensajes MeshCore/Meshtastic/APRS/Voice RF;
- cambia `verification` a multi-source;
- ordena incidentes por prioridad;
- sustituye análisis humanos.

## Criterios de aceptación

1. IA global OFF -> cero llamadas externas.
2. Emergencias IA OFF -> cero llamadas externas.
3. Sin componentes válidos -> cero llamadas externas.
4. Fase desconocida -> cero llamadas externas.
5. Payload mínimo sin metadata privada.
6. Componentes inválidos/incompatibles quedan fuera.
7. Contrato JSON estricto.
8. Barrera FIRMS reutilizada.
9. Límites de texto exactos y sin corte bruto de palabras.
10. Evento y resultados de fases anteriores permanecen inmutables.
11. Ningún fichero operativo de Emergencias se modifica.
12. Ninguna salida de radio/mensajería consume IA-2D.
