# MeshNet ControlPanel

Panel web independiente para supervisar y operar las aplicaciones instaladas en
`tools/`. No depende de Docker ni modifica el panel web de Meshtastic.

## Funciones

- descubre aplicaciones mediante `manifests/*.json`;
- habilita cada aplicación en el panel con estado persistente;
- consulta su endpoint `/health`;
- inicia, detiene y reinicia sus servicios API;
- consulta servicios y temporizadores;
- ejecuta únicamente acciones CLI declaradas por el administrador;
- convierte los resultados técnicos en tarjetas, campos y listas legibles;
- configura las fuentes de Emergencias, los tipos que se recogen, una o varias
  provincias, un radio geográfico y la MAP_KEY de FIRMS sin volver a mostrarla;
- permite elegir severidad y categorías propagables de Emergencias;
- muestra y permite editar los canales MeshCore y Meshtastic de Farmacias y de
  cada ruta de Emergencias (emergencias, servicios y meteorología);
- exige confirmación para paradas, reinicios, publicaciones y notificaciones.

Los manifiestos incluidos cubren Farmacias y Emergencias: estado, actualización,
listados actuales, histórico, fuentes, áreas, categorías, diagnóstico,
publicación/avisos y control de sus API. Para añadir una aplicación futura se
instala otro JSON en `manifests/`; no es necesario modificar `web_admin.py`.
El navegador nunca puede enviar un comando, argumentos, ruta o unidad systemd.

## Instalación

```bash
cd ~/MeshNet-Bot/tools/ControlPanel
chmod +x start_control_panel.sh
./start_control_panel.sh
```

El script crea `.venv`, instala las dependencias si faltan, muestra la dirección
local y de red, y abre el navegador automáticamente cuando existe un escritorio
gráfico. En una Raspberry sin escritorio, abra desde otro equipo la dirección
indicada, por ejemplo `http://192.168.1.69:8790`.

Antes de anunciar que el panel está disponible, el script comprueba que
`web_admin.py` y `control_panel.py` no contienen marcadores de conflicto Git y
que ambos compilan. Si aparece `<<<<<<< ours`, `=======` o `>>>>>>> theirs`, la
copia local quedó a medio resolver y Python no puede ejecutarla. Si no hay
cambios locales que conservar, se recupera la versión del repositorio con:

```bash
cd ~/MeshNet-Bot
git restore tools/ControlPanel/web_admin.py tools/ControlPanel/control_panel.py
git pull
bash tools/ControlPanel/start_control_panel.sh
```

Si sí existen cambios propios, no use `git restore`: resuelva los bloques de
conflicto manualmente y ejecute `python3 -m py_compile` sobre ambos archivos.

Si `origin/main` también contiene los marcadores (es decir, fueron publicados
por error), restaurar desde `origin/main` no sirve. Primero localice una rama
remota limpia y verifíquela **antes** de copiar el archivo. Para la rama de
desarrollo de Emergencias:

```bash
cd ~/MeshNet-Bot
git fetch origin --prune
CLEAN_REF=origin/codex/investigar-implementacion-extraccion-datos-emergencias

git show "$CLEAN_REF:tools/ControlPanel/web_admin.py" |
  grep -nE '^(<<<<<<<|=======|>>>>>>>)' && {
    echo "La rama candidata también está contaminada"; exit 1;
  }

git restore --source="$CLEAN_REF" --worktree \
  tools/ControlPanel/web_admin.py \
  tools/ControlPanel/control_panel.py \
  tools/ControlPanel/start_control_panel.sh

python3 -m py_compile \
  tools/ControlPanel/web_admin.py \
  tools/ControlPanel/control_panel.py
```

Este procedimiento no toca directorios `data/` ni archivos `.env`. El archivo
restaurado aparecerá como modificado respecto a `main` hasta que la corrección
limpia se integre en esa rama; no use `git clean` para resolverlo.

### Página vacía después de reparar los conflictos

El HTML del panel se sirve ahora con `Cache-Control: no-store` y muestra un
estado inicial “Cargando aplicaciones…”. Si JavaScript falla, sustituye la
página vacía por el motivo del error y sugiere consultar `/api/tools`. Tras
actualizar una instalación que hubiera servido el JavaScript conflictivo:

```bash
sudo systemctl restart meshnet-control-panel.service
curl -s http://127.0.0.1:8790/api/tools | python3 -m json.tool
```

Después use `Ctrl+F5` o una ventana privada. Si `/api/tools` devuelve la lista de
aplicaciones pero el navegador conserva una pantalla antigua, se trata de caché;
si el endpoint falla, revise `journalctl -u meshnet-control-panel.service`.

El ejecutable directo escucha en `127.0.0.1`; el script escucha en `0.0.0.0`
para permitir acceso desde la red privada. Variables:

- `CONTROLPANEL_HOST` y `CONTROLPANEL_PORT`: escucha;
- `CONTROLPANEL_STATE`: archivo de estado;
- `CONTROLPANEL_MANIFESTS`: directorio alternativo de manifiestos.
- `CONTROLPANEL_EMERGENCIAS_CONFIG`: configuración JSON de Emergencias;
- `CONTROLPANEL_EMERGENCIAS_ENV`: fichero privado que contiene `FIRMS_MAP_KEY`.
- `CONTROLPANEL_TOKEN`: contraseña del usuario web `admin`.

El acceso se protege mediante autenticación HTTP Basic cuando se configura
`CONTROLPANEL_TOKEN`. El instalador genera un token aleatorio, lo guarda en
`tools/ControlPanel/.env` con permisos `0600` y lo muestra una sola vez. El
arranque manual también carga ese archivo; si escucha en una dirección de red y
no existe token persistente, crea uno temporal y lo muestra en la terminal.

Use el usuario `admin` y el token como contraseña. Aunque exista autenticación,
publique el panel únicamente en una red privada: HTTP Basic no cifra el tráfico
y no debe exponerse directamente a Internet sin HTTPS.

## systemd y permisos

La unidad `systemd/meshnet-control-panel.service` inicia el panel de forma
permanente.

Que `start_control_panel.sh` funcione manualmente no instala esta unidad. Si
`systemctl` responde `Unit meshnet-control-panel.service could not be found`,
instálela con el asistente, que crea el entorno virtual, adapta las rutas y el
usuario de la unidad a la instalación actual, instala la regla PolicyKit mínima
para ese mismo usuario y habilita el servicio:

```bash
cd ~/MeshNet-Bot/tools/ControlPanel
chmod +x install_control_panel_service.sh
./install_control_panel_service.sh
```

Ejecute el asistente desde la cuenta que gestiona el repositorio, **sin**
anteponer `sudo`: solicitará privilegios únicamente para copiar la unidad y la
regla. Por seguridad, rechaza instalar el proceso web como `root`.

Después, el panel queda disponible tras reinicios y se comprueba con:

```bash
systemctl status meshnet-control-panel.service --no-pager
curl -s http://127.0.0.1:8790/health | python3 -m json.tool
```

La unidad instalada escucha en la red privada (`0.0.0.0`). Desde otro equipo,
abra `http://IP_DE_LA_RASPBERRY:8790`; `127.0.0.1` solo funciona en el propio
equipo donde se ejecuta el servicio. El instalador muestra ambas direcciones al
terminar.

Si una instalación anterior continúa ligada a `127.0.0.1`, vuelva a ejecutar
`./install_control_panel_service.sh` para actualizar la unidad. Compruebe la
dirección efectiva y los últimos errores con:

```bash
systemctl show meshnet-control-panel.service -p Environment --no-pager
systemctl status meshnet-control-panel.service --no-pager
journalctl -u meshnet-control-panel.service -n 50 --no-pager
```

El instalador espera hasta diez segundos a que `/health` confirme el arranque.
Este endpoint no consulta las APIs externas: informa de la versión del panel y
del número de aplicaciones registradas y habilitadas, por lo que también puede
usarse como sonda local de monitorización.

Las acciones de lectura usan `systemctl` sin privilegios. Las mutaciones usan
PolicyKit y requieren la regla mínima incluida:

```bash
sudo install -o root -g root -m 0644 \
  systemd/50-meshnet-control-panel.rules \
  /etc/polkit-1/rules.d/50-meshnet-control-panel.rules
```

No se concede acceso general a `systemctl`, Python ni un shell. Al registrar una
aplicación futura, añada a la regla solo sus operaciones y unidades exactas.

## Formato de manifiesto

```json
{
  "id": "mi_aplicacion",
  "name": "Mi aplicación",
  "description": "Descripción",
  "url": "http://127.0.0.1:8800",
  "health_path": "/health",
  "actions": [
    {
      "id": "status",
      "name": "Estado",
      "kind": "command",
      "argv": ["${PYTHON}", "${REPO}/tools/mi_aplicacion/app.py", "status"]
    }
  ]
}
```

Tipos permitidos: `command` con un `argv` fijo, y `systemd` con una operación
entre `status`, `start`, `stop`, `restart`, `enable` y `disable`. Los ids,
unidades, timeout (1–300 s) y duplicados se validan al arrancar.

## Filtros de Emergencias

La tarjeta de Emergencias se organiza en pestañas para no mezclar operación y
configuración:

- **Resumen**: disponibilidad de API, estado del recolector, última recogida,
  fuentes activas, pendientes, áreas y estado individual de cada fuente;
- **Fuentes y cobertura**: orígenes, tipos recogidos, MAP_KEY, provincias y radio;
- **Propagación**: matriz categoría×severidad y canales de comunicación.

El selector de provincias incluye búsqueda normalizada (ignora acentos) y chips
con la selección actual. La matriz utiliza colores por severidad y mantiene
fijas sus cabeceras durante el desplazamiento. Los guardados importantes se
confirman mediante avisos breves en pantalla, conservando el detalle técnico en
el resultado de la tarjeta.

La sección **Recogida de emergencias** permite habilitar independientemente
Ayuntamiento de Zaragoza, DGT, terremotos IGN y focos térmicos NASA FIRMS. Se
pueden seleccionar los tipos conservados, una o varias provincias y un radio.
Las provincias se usan cuando la fuente informa el límite administrativo; IGN
y FIRMS necesitan el radio porque publican coordenadas. Si FIRMS está activo,
el panel exige una MAP_KEY, la guarda con permisos privados y solo devuelve al
navegador si existe, nunca su valor.

Esta configuración es distinta del filtro de propagación: la primera decide qué
se descarga y conserva; el segundo decide qué puede enviarse por radio.

La tarjeta muestra una matriz de propagación con una fila por categoría y una
columna por severidad (`baja`, `media`, `alta` y `crítica`). Cada casilla decide
una combinación exacta. Por ejemplo, se puede habilitar Protección Civil en
media y Terremoto en alta sin que Protección Civil alta ni Terremoto medio se
propaguen. Los botones de columna seleccionan toda una severidad y **Limpiar
matriz** bloquea todas las combinaciones.

Guardar el filtro modifica la configuración persistente de
`emergencias_guardia`. No envía mensajes inmediatamente; se aplica a las
próximas comprobaciones y propagaciones.

## Canales de comunicación

La **API de consultas** y el **recolector programado** son unidades distintas.
La primera atiende consultas DM y el botón *Comprobar salud*; el temporizador
descarga fuentes y puede difundir cambios a la malla aunque la API no esté
instalada. Por eso los botones systemd se denominan explícitamente “API de
consultas”. Si falta su unidad, el panel muestra una explicación y el paso de
instalación en vez de presentar únicamente el error bruto de systemd.

Las tarjetas de Farmacias y Emergencias muestran el transporte activo y los
índices de canal de MeshCore y Meshtastic. El valor `-1` deja ese destino sin
configurar. Emergencias guarda los canales por separado para las rutas de
emergencias, servicios y meteorología, mientras que el transporte es único para
las tres rutas. Farmacias actualiza únicamente esas tres claves públicas en su
`.env`, conservando el resto de variables y secretos.

Los controles de canales y filtros solo aparecen cuando la aplicación está
habilitada. La API aplica la misma comprobación y rechaza cualquier lectura o
escritura de esta configuración mientras la tarjeta permanezca deshabilitada.

Después de cambiar los canales de Farmacias, reinicie su API desde la propia
tarjeta para que el proceso vuelva a cargar el `.env`. Emergencias lee su
configuración persistente en cada ejecución programada.

El panel muestra también el perfil de radio y la salida efectiva. Con
`RADIO_PROFILE=meshcore_only` no permite seleccionar Meshtastic; si una
configuración antigua todavía lo solicita, Farmacias cambia de forma segura a
MeshCore al publicar para evitar el error `meshtastic_disabled_by_radio_profile`.
Si `RADIO_PROFILE` no existe en el `.env` independiente de Farmacias, el panel
lo indica como no definido en vez de mostrar un perfil predeterminado que no
está realmente configurado. En ese caso, la respuesta del broker decide el
respaldo de transporte durante la publicación.

## Pruebas

```bash
python3 -m pytest -q tools/ControlPanel/tests
```
