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
