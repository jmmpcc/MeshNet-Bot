# MeshNet Intelligence — Fase IA-1

## Objetivo

IA-1 incorpora dos capacidades reutilizables sobre la infraestructura IA-0:

- resumen semántico con límite estricto de longitud;
- clasificación contra un catálogo cerrado de etiquetas.

Esta fase no conecta todavía IA con broker, radio, APRS, BBS, correo, Telegram ni
Emergencias. Su finalidad es validar el motor antes de introducirlo en funciones
operativas.

## Compatibilidad

La regla sigue siendo:

```env
MESHNET_AI_ENABLED=0
```

Con IA desactivada no se realizan llamadas externas. Las funciones IA-1 devuelven
`ok=False` y el consumidor debe mantener la ruta clásica.

El resumen requiere además:

```env
MESHNET_AI_SUMMARIZE_ENABLED=1
```

La clasificación de IA-1 se expone solo como operación manual/programática y no
está conectada todavía a ningún flujo automático. Cualquier integración posterior
deberá incorporar su autorización funcional específica.

## Resumen

API Python:

```python
from shared.meshnet_ai import MeshNetAI
from shared.meshnet_ai_tasks import MeshNetAITasks

ai = MeshNetAI.from_env()
tasks = MeshNetAITasks(ai)

result = tasks.summarize_text(
    texto,
    max_chars=140,
    context="mensaje para red LoRa",
    preserve=["tipo", "localidad", "estado"],
)

if result.ok:
    resumen = result.text
else:
    resumen = texto_clasico
```

Propiedades:

- nunca se llama al proveedor si IA o resumen están desactivados;
- el texto vacío se rechaza sin llamada externa;
- la respuesta se normaliza a una sola línea;
- se fuerza el presupuesto máximo de caracteres;
- se solicita explícitamente no inventar información;
- un fallo, timeout o respuesta vacía produce `ok=False`.

## Clasificación

API Python:

```python
result = tasks.classify_text(
    texto,
    labels=["incendio", "inundacion", "terremoto", "otro"],
    context="clasificación preliminar",
)

if result.ok:
    categoria = result.label
    confianza = result.confidence
else:
    categoria = categoria_clasica
```

La salida del modelo debe ser JSON:

```json
{
  "label": "incendio",
  "confidence": 0.91,
  "reasoning": "menciona fuego forestal"
}
```

Protecciones:

- se requieren al menos dos etiquetas;
- las etiquetas se normalizan y deduplican;
- no se admite ninguna etiqueta fuera del catálogo suministrado;
- JSON inválido produce fallback;
- `confidence` se limita a `0.0..1.0`;
- el razonamiento se limita a 240 caracteres;
- la clasificación no sustituye ninguna severidad, categoría o regla existente.

## CLI de prueba

Resumen:

```bash
MESHNET_AI_ENABLED=1 \
MESHNET_AI_SUMMARIZE_ENABLED=1 \
MESHNET_AI_PROVIDER=ollama \
MESHNET_AI_MODEL=<modelo-local> \
python3 tools/intelligence/meshnet_ai_tasks_cli.py summarize \
  --max-chars 140 \
  "Texto de prueba"
```

Clasificación:

```bash
MESHNET_AI_ENABLED=1 \
MESHNET_AI_PROVIDER=ollama \
MESHNET_AI_MODEL=<modelo-local> \
python3 tools/intelligence/meshnet_ai_tasks_cli.py classify \
  --labels incendio,inundacion,otro \
  "Se observa fuego forestal"
```

La CLI solo imprime JSON. No transmite, no modifica configuración y no persiste
el texto.

## Pruebas automatizadas

```bash
python3 -m py_compile \
  shared/meshnet_ai_tasks.py \
  tools/intelligence/meshnet_ai_tasks_cli.py \
  tests/test_meshnet_ai_tasks.py

python3 -m unittest tests.test_meshnet_ai_tasks -v
```

También debe mantenerse válida IA-0:

```bash
python3 -m unittest \
  tests.test_meshnet_ai \
  tests.test_meshnet_intelligence_health \
  tests.test_meshnet_ai_tasks \
  -v
```

## Criterios para pasar a integración operativa

1. IA desactivada: cero llamadas externas.
2. Flag de resumen desactivado: cero llamadas externas.
3. Timeout/fallo: `ok=False`, sin excepción propagada.
4. Resumen: longitud nunca superior al presupuesto solicitado.
5. Clasificación: solo etiquetas permitidas.
6. JSON inválido o categoría inventada: rechazo y fallback.
7. Ningún fichero operativo existente modificado en IA-1 base.
8. Ninguna transmisión RF o publicación automática generada por IA-1.
