# Matriz de pruebas IA-1

| Caso | Configuración | Resultado esperado |
|---|---|---|
| IA global OFF | `MESHNET_AI_ENABLED=0` | No llamada externa, `ok=false`, `status=disabled` |
| Resumen OFF | IA global ON + `MESHNET_AI_SUMMARIZE_ENABLED=0` | No llamada externa, `status=feature_disabled` |
| Texto vacío | Resumen ON | No llamada externa, `ok=false` |
| Resumen correcto | Proveedor devuelve texto | `ok=true` y longitud <= `max_chars` |
| Proveedor caído | Timeout/error | `ok=false`, fallback del consumidor |
| Clasificación válida | Etiqueta dentro del catálogo | `ok=true`, etiqueta aceptada |
| Clasificación inventada | Etiqueta fuera del catálogo | `ok=false` |
| JSON inválido | Respuesta no JSON | `ok=false` |
| Catálogo insuficiente | Menos de 2 etiquetas | No llamada externa, `ok=false` |

La fase IA-1 base no conecta ninguno de estos resultados a transmisiones o decisiones operativas.
