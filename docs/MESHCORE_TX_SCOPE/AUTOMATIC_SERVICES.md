# Scope MeshCore por transmisión en servicios automáticos

## Objetivo

Los servicios automáticos de Farmacias y Emergencias pueden añadir un flood
scope a cada `MESHCORE_SEND` de canal sin asociar permanentemente un scope al
`channel_idx`.

La semántica continúa siendo:

```text
channel_idx = por dónde sale
scope       = hasta dónde se propaga
```

El soporte del broker para scope por TX es el mismo incorporado en el PR #186.
Esta extensión no cambia DM, Meshtastic, APRS, fragmentación, filtros, retries ni
la lógica interna de las aplicaciones.

## Compatibilidad

Las variables nuevas son opcionales:

```env
FARMACIAS_MESHCORE_SCOPE=
EMERGENCIAS_MESHCORE_SCOPE=
```

Si están vacías o no existen, los launchers entregan al broker el mismo payload
histórico, sin campo `scope`.

El scope solo se añade cuando se cumplen simultáneamente estas condiciones:

- comando `MESHCORE_SEND`;
- `kind=chan`;
- existe un scope configurado;
- el payload no incluía ya un scope explícito.

Los `kind=contact` usados para respuestas DM nunca reciben este scope.

## Cliente Utebo

Para una instalación cuyo ámbito RF sea Utebo:

### Farmacias

Archivo:

```text
/home/meshnet/MeshNet-Bot/tools/farmacias_guardia/.env
```

Añadir:

```env
FARMACIAS_MESHCORE_SCOPE=#Utebo
```

Los temporizadores de Farmacias utilizan automáticamente
`farmacias_guardia_scoped.py` para:

```text
send
check --send
```

El botón de publicación manual del Control Panel utiliza también el launcher
scoped.

### Emergencias: DGT e incendios

El servicio `meshnet-emergencias-check.service` carga primero el `.env` general y
después el `.env` local de Emergencias. Añadir en:

```text
/home/meshnet/MeshNet-Bot/tools/emergencias_guardia/.env
```

```env
EMERGENCIAS_MESHCORE_SCOPE=#Utebo
```

El temporizador de Emergencias ejecuta:

```text
emergencias_guardia_scoped.py check --notify-changes
```

Por tanto, cualquier aviso automático que el motor haya autorizado para salida
MeshCore —DGT, FIRMS u otra fuente— incorpora `scope=#Utebo` en ese TX de canal.
Los filtros de fuente y cobertura continúan siendo independientes del scope RF.

Ejemplo conceptual para DGT:

```text
filtro de datos: provincia Zaragoza
canal:          SERVICIOS
scope TX:       #Utebo
```

Ejemplo conceptual para FIRMS:

```text
filtro de datos: radio/cobertura configurada
canal:          EMERGENCIAS
scope TX:       #Utebo
```

## Actualización de systemd

Después de actualizar el repositorio en una instalación existente:

```bash
cd /home/meshnet/MeshNet-Bot

sudo install -m 0644 \
  tools/farmacias_guardia/systemd/meshnet-farmacias-daily.service \
  /etc/systemd/system/

sudo install -m 0644 \
  tools/farmacias_guardia/systemd/meshnet-farmacias-check.service \
  /etc/systemd/system/

sudo install -m 0644 \
  tools/emergencias_guardia/systemd/meshnet-emergencias-check.service \
  /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl restart meshnet-farmacias-daily.timer
sudo systemctl restart meshnet-farmacias-check.timer
sudo systemctl restart meshnet-emergencias-check.timer
```

## Prueba manual Farmacias

Antes de activar tráfico periódico:

```bash
cd /home/meshnet/MeshNet-Bot/tools/farmacias_guardia
python3 farmacias_guardia.py fetch
python3 farmacias_guardia.py preview
python3 farmacias_guardia_scoped.py send --force
```

En el broker debe observarse un encolado MeshCore con el scope configurado.

## Prueba manual Emergencias

Para comprobar la recolección sin modificar su lógica:

```bash
cd /home/meshnet/MeshNet-Bot/tools/emergencias_guardia
python3 emergencias_guardia_scoped.py fetch --source dgt_datex
```

La difusión automática continúa siendo responsabilidad de
`check --notify-changes` y solo transmite eventos nuevos o actualizados que
superen los filtros existentes.

## Prueba RF recomendada

1. Configurar el repetidor local con la región `#Utebo` y flood permitido.
2. Configurar `FARMACIAS_MESHCORE_SCOPE=#Utebo`.
3. Configurar `EMERGENCIAS_MESHCORE_SCOPE=#Utebo`.
4. Realizar primero un envío manual corto con `/enviar_mc ... --scope #Utebo`.
5. Probar la publicación manual de Farmacias con el launcher scoped.
6. Comprobar que el repetidor de Utebo retransmite.
7. Comprobar que un repetidor que no pertenece a `#Utebo` no retransmite.
8. Activar después los temporizadores automáticos.

## Diseño de seguridad

No existe un mapa fijo `canal -> scope`.

Un mismo canal puede utilizar scopes diferentes desde otros productores porque
el valor sigue formando parte del item individual de transmisión. Los launchers
solo aportan el valor configurado para estas dos aplicaciones concretas.

Si el broker rechaza la aplicación del scope, permanece vigente la política
fail-closed del soporte por TX: el mensaje que requería ese scope no debe salir
como tráfico global accidentalmente.
