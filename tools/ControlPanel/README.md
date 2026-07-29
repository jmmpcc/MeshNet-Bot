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

El ejecutable directo escucha en `127.0.0.1`; el script escucha en `0.0.0.0`
para permitir acceso desde la red privada. Variables:

- `CONTROLPANEL_HOST` y `CONTROLPANEL_PORT`: escucha;
- `CONTROLPANEL_STATE`: archivo de estado;
- `CONTROLPANEL_MANIFESTS`: directorio alternativo de manifiestos.
- `CONTROLPANEL_EMERGENCIAS_CONFIG`: configuración JSON de Emergencias;
- `CONTROLPANEL_EMERGENCIAS_ENV`: fichero privado que contiene `FIRMS_MAP_KEY`.

El panel no solicita token. Debe publicarse únicamente en una red privada y no
exponerse directamente a Internet, ya que contiene acciones operativas.

## systemd y permisos

La unidad `systemd/meshnet-control-panel.service` inicia el panel de forma
permanente.

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
