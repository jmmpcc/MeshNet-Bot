# MeshNet Intelligence

## Principio de diseño

MeshNet Intelligence es una extensión opcional y desacoplada. La ausencia,
desactivación o fallo de cualquier proveedor de IA no debe impedir las funciones
tradicionales de MeshNet.

La variable global es:

```env
MESHNET_AI_ENABLED=0
```

Si la variable no existe se interpreta igualmente como `0`.

## Fase IA-0

Esta fase incorpora exclusivamente infraestructura común. No conecta todavía IA
con broker, MeshCore, Meshtastic, APRS, BBS, correo, Telegram ni Emergencias.

El módulo `shared/meshnet_ai.py` proporciona:

- interruptor global de IA;
- feature flags independientes por módulo;
- configuración de proveedor sin secretos en estado público;
- proveedores OpenAI/Responses API, Ollama y endpoint compatible;
- timeout obligatorio;
- circuit breaker;
- resultado uniforme `AIResult` para aplicar fallback;
- estado público `disabled`, `available`, `degraded` o `error`;
- cero dependencias Python adicionales.

## Configuración

Configuración base recomendada para instalaciones existentes:

```env
MESHNET_AI_ENABLED=0
```

Configuración completa disponible:

```env
# Interruptor maestro. Ausente = 0.
MESHNET_AI_ENABLED=0

# openai | ollama | compatible
MESHNET_AI_PROVIDER=openai
MESHNET_AI_MODEL=
MESHNET_AI_API_KEY=
MESHNET_AI_BASE_URL=

# Protección de red.
MESHNET_AI_TIMEOUT_SEC=10
MESHNET_AI_FAILURE_THRESHOLD=3
MESHNET_AI_COOLDOWN_SEC=300

# Módulos. Todos son 0 por defecto aunque la IA global esté activa.
MESHNET_AI_EMERGENCIES_ENABLED=0
MESHNET_AI_SUMMARIZE_ENABLED=0
MESHNET_AI_CORRELATION_ENABLED=0
MESHNET_AI_NETWORK_ENABLED=0
MESHNET_AI_LOG_ANALYSIS_ENABLED=0
MESHNET_AI_KNOWLEDGE_ENABLED=0
MESHNET_AI_RADIO_ENABLED=0
MESHNET_AI_PYTHIA_ENABLED=0
MESHNET_AI_AGENT_ENABLED=0
```

### OpenAI

```env
MESHNET_AI_ENABLED=1
MESHNET_AI_PROVIDER=openai
MESHNET_AI_MODEL=<modelo-autorizado>
MESHNET_AI_API_KEY=<secreto-local>
```

No debe añadirse la clave API al repositorio.

### Ollama

```env
MESHNET_AI_ENABLED=1
MESHNET_AI_PROVIDER=ollama
MESHNET_AI_MODEL=<modelo-local>
MESHNET_AI_BASE_URL=http://127.0.0.1:11434
```

Ollama no requiere `MESHNET_AI_API_KEY`.

### Endpoint compatible

```env
MESHNET_AI_ENABLED=1
MESHNET_AI_PROVIDER=compatible
MESHNET_AI_MODEL=<modelo>
MESHNET_AI_BASE_URL=https://servidor-ejemplo
MESHNET_AI_API_KEY=<secreto-local>
```

El proveedor `compatible` debe exponer una API de Responses en
`<MESHNET_AI_BASE_URL>/v1/responses`.

## Feature flags

El interruptor global tiene prioridad sobre todos los demás. Ejemplo:

```env
MESHNET_AI_ENABLED=0
MESHNET_AI_SUMMARIZE_ENABLED=1
```

El resumen IA sigue desactivado porque el interruptor maestro está a `0`.

Cuando se active globalmente, cada integración futura deberá comprobar su propio
flag antes de usar IA.

## Fallback obligatorio

Toda integración futura debe conservar primero su comportamiento clásico y usar
el resultado IA solo cuando `result.ok` sea verdadero:

```python
ai_result = ai.generate_text(prompt)
if ai_result.ok:
    return ai_result.text
return comportamiento_actual()
```

No se admite que una función existente dependa exclusivamente de una respuesta IA.

## Circuit breaker

`MESHNET_AI_FAILURE_THRESHOLD` establece el número de fallos consecutivos que
abren el circuito. Mientras esté abierto no se realizan llamadas externas.

Tras `MESHNET_AI_COOLDOWN_SEC` se permite una nueva prueba. Un éxito reinicia el
contador; un nuevo fallo vuelve a aplicar la protección.

## Estado seguro

`MeshNetAI.public_status()` nunca devuelve la clave API. Los estados son:

- `disabled`: IA desactivada por el administrador.
- `available`: configuración válida y sin fallo activo conocido.
- `degraded`: último intento falló o circuit breaker abierto.
- `error`: IA activada pero configuración inválida/incompleta.

El estado `available` en esta fase significa que la configuración está preparada;
la disponibilidad real del proveedor se confirma al realizar una operación.

## Seguridad

- Nunca registrar `MESHNET_AI_API_KEY`.
- No almacenar secretos en el repositorio.
- Los módulos consumidores deben anonimizar datos sensibles antes de enviarlos a
  un proveedor externo.
- La IA no puede transmitir RF, reiniciar servicios, modificar configuración ni
  ejecutar acciones administrativas durante IA-0.
- La lógica crítica y las reglas existentes siguen siendo la autoridad.

## Pruebas de IA-0

Ejecutar:

```bash
python3 -m unittest tests.test_meshnet_ai -v
```

Y posteriormente la batería general del proyecto:

```bash
python3 -m compileall -q source shared tools
python3 -m unittest discover -s tests -p 'test_*.py'
```

Criterios mínimos:

1. Sin `MESHNET_AI_ENABLED`, no se realiza ninguna llamada de red.
2. Los módulos IA están desactivados por defecto.
3. Una configuración incompleta no impide arrancar MeshNet.
4. Un fallo del proveedor devuelve `ok=False`, nunca rompe el flujo llamador.
5. El circuit breaker abre y recupera correctamente.
6. El estado público no contiene secretos.
