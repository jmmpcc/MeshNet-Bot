# Perfiles de radio de MeshNet-Bot

## Autoridad de configuración

`RADIO_PROFILE` es la autoridad operativa. `bridge_config.json` puede aportar
mapeos y reglas compatibles, pero no debe sustituir silenciosamente el perfil
seleccionado en `.env`.

## Perfiles canónicos

| Perfil | Nodo A | Nodo B | Transporte automático |
|---|---|---|---|
| `meshcore_only` | MeshCore | Desactivado | MeshCore |
| `meshtastic_a_meshcore_embedded_b` | Meshtastic | MeshCore embebido | Meshtastic |
| `meshcore_a_meshtastic_embedded_b` | MeshCore | Meshtastic embebido | MeshCore |

Se mantienen los aliases históricos `meshcore_embedded`,
`meshtastic_a_meshcore_b` y `meshcore_a_meshtastic_b`.

En el perfil invertido, las operaciones automáticas del panel, Farmacias,
correo, APRS y tareas programadas conservan MeshCore (nodo A) como salida por
defecto. Meshtastic (nodo B) continúa disponible cuando se selecciona de forma
explícita; no se aplican las restricciones exclusivas de `meshcore_only`.

### BBS en los perfiles combinados

Con `BBS_ENABLED=1`, la misma BBS embebida atiende comandos `#BBS` recibidos
tanto por Meshtastic como por MeshCore en los dos perfiles combinados. La
respuesta vuelve siempre por la radio de entrada: un DM MeshCore recibe un DM
MeshCore y un comando de canal responde por DM o canal según `BBS_DM_ONLY`, sin
cruzar el bridge ni duplicarse en el otro nodo.

Por defecto se reutilizan `BBS_CHANNELS` / `BBS_CHANNEL` también para los
índices de canal MeshCore. Si la numeración de ambas radios no coincide, se
puede definir `BBS_MESHCORE_CHANNELS` con una lista CSV exclusiva para MeshCore.
El perfil `meshcore_only` conserva deliberadamente la BBS desactivada para no
alterar su comportamiento actual.

Ejemplo para habilitar la BBS en los canales MeshCore con `channel_idx` 2 y 4:

```dotenv
BBS_ENABLED=1
BBS_MESHCORE_CHANNELS=2,4
BBS_DM_ONLY=1
BBS_DM_CHANNEL=0
```

Los números son los índices nativos configurados en el companion MeshCore; no
son el canal Meshtastic ni la clave izquierda de `MESHCORE_CHANNEL_MAP`. Los DM
se aceptan independientemente de esta lista. Con `BBS_DM_ONLY=1`, un comando
válido recibido en uno de esos canales se procesa, pero la contestación vuelve
por DM al contacto emisor. Con `BBS_DM_ONLY=0`, la contestación permanece en el
mismo canal MeshCore.

Si ambas radios usan el mismo número de canal puede omitirse la variable nueva:

```dotenv
BBS_ENABLED=1
BBS_CHANNELS=5
# BBS_MESHCORE_CHANNELS no definida: MeshCore también escucha channel_idx 5
```

El Control Panel permite además seleccionar **Ambos nodos** para las emisiones
de Emergencias y Farmacias. En ese modo cada aviso se entrega directamente al
canal MeshCore configurado en A y al canal Meshtastic configurado en B; los
envíos incluyen `no_bridge`, por lo que no se duplican a través de la pasarela.

## Validación previa al despliegue

Desde la raíz del proyecto:

```bash
scripts/radio-profile-check --env-file .env
```

Salida estructurada:

```bash
scripts/radio-profile-check --env-file .env --json
```

El validador no conecta con las radios y no modifica `.env`. Comprueba:

- que el perfil sea conocido;
- que exista `MESHCORE_TCP_HOST` cuando MeshCore funciona por TCP;
- que exista un host Meshtastic en los perfiles combinados;
- que las capacidades derivadas coincidan con el perfil elegido.

Códigos de salida:

- `0`: configuración mínima válida;
- `1`: perfil conocido, pero configuración no operativa;
- `2`: error de lectura o sintaxis del fichero `.env`.

## Pruebas de regresión

```bash
python -m unittest discover -s tests -v
python -m compileall -q source
```

Estas pruebas cubren aliases, capacidades, overrides, conflictos con
`bridge_config.json` y requisitos mínimos de los tres perfiles.
