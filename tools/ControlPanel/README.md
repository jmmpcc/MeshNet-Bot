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

La tarjeta de Emergencias permite seleccionar:

- severidad mínima: baja, media, alta o crítica;
- categorías propagables: incendios, tráfico, cortes, meteorología, servicios,
  seguridad pública y otras.

Guardar el filtro modifica la configuración persistente de
`emergencias_guardia`. No envía mensajes inmediatamente; se aplica a las
próximas comprobaciones y propagaciones.

## Canales de comunicación

Las tarjetas de Farmacias y Emergencias muestran el transporte activo y los
índices de canal de MeshCore y Meshtastic. El valor `-1` deja ese destino sin
configurar. Emergencias guarda los canales por separado para las rutas de
emergencias, servicios y meteorología; Farmacias actualiza únicamente esas tres
claves públicas en su `.env`, conservando el resto de variables y secretos.

Después de cambiar los canales de Farmacias, reinicie su API desde la propia
tarjeta para que el proceso vuelva a cargar el `.env`. Emergencias lee su
configuración persistente en cada ejecución programada.

## Pruebas

```bash
python3 -m pytest -q tools/ControlPanel/tests
```
