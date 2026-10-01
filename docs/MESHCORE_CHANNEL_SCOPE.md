# MeshCore — flood scope por canal

## Objetivo

MeshNet-Bot puede aplicar un flood scope específico antes de cada transmisión de canal MeshCore sin modificar la cola, la fragmentación, los reintentos ni los DM existentes.

`meshcore_py` requiere actualmente dos órdenes separadas para un TX scoped:

```python
await mc.commands.set_flood_scope(scope)
await mc.commands.send_chan_msg(channel_idx, text)
```

Por ese motivo el scope se configura explícitamente en MeshNet mediante una relación `channel_idx -> scope`.

## Configuración

Añadir al `.env`:

```dotenv
MESHCORE_CHANNEL_SCOPE_MAP=5:#zaragoza
```

Para varios canales:

```dotenv
MESHCORE_CHANNEL_SCOPE_MAP=5:#zaragoza,2:#otra-region
```

Las claves son **índices nativos de canal MeshCore**, no índices Meshtastic.

El valor se pasa sin transformación a `meshcore_py`. Debe coincidir exactamente con el scope utilizado por la aplicación MeshCore.

Valores especiales admitidos por MeshCore:

- `0`: limpia el override y usa el default scope del nodo.
- `*`: fuerza tráfico unscoped.

Si un `channel_idx` no aparece en el mapa, MeshNet ejecuta `set_flood_scope("0")` antes de transmitir por él. Así un scope aplicado a un TX anterior no puede filtrarse accidentalmente al siguiente canal.

## Seguridad del TX

Si `set_flood_scope()` devuelve ERROR, MeshNet no ejecuta `send_chan_msg()`. Esto evita que un fallo al establecer la región convierta silenciosamente un mensaje regional en tráfico global/unscoped.

La funcionalidad queda completamente desactivada cuando `MESHCORE_CHANNEL_SCOPE_MAP` está vacío o no existe; en ese caso el comportamiento anterior permanece intacto.

## Telegram

Cuando existe un scope configurado:

- TX `/enviar_mc`: muestra `Scope TX: <scope>`.
- RX de ese canal: muestra `scope canal configurado: <scope>`.

En RX se utiliza deliberadamente la expresión **scope canal configurado**. La versión actual de `meshcore_py` no incluye todavía el scope real en el evento `CHANNEL_MSG_RECV`, por lo que MeshNet no debe presentarlo como un dato medido del paquete recibido.

## Prueba funcional recomendada

Con un canal regional configurado:

1. Enviar un mensaje desde `/enviar_mc`.
2. Confirmar que Telegram muestra el scope esperado.
3. Comprobar la traza MeshCore.
4. Verificar que sólo retransmiten repetidores pertenecientes a esa región.
5. Enviar después por un canal no incluido en `MESHCORE_CHANNEL_SCOPE_MAP` y comprobar que conserva el comportamiento default del nodo.
