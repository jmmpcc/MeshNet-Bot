# MeshNet ControlPanel

Aplicación web **independiente** para administrar las herramientas instaladas en
`tools/`. No forma parte del panel web de Meshtastic, no necesita Docker y no
modifica sus contenedores ni su configuración.

Esta primera fase permite:

- registrar exclusivamente aplicaciones conocidas;
- habilitar o deshabilitar su acceso desde el panel;
- conservar el estado local en `data/state.json`;
- comprobar el endpoint de salud de cada aplicación habilitada.

Incluye inicialmente `farmacias_guardia` y `emergencias_guardia`. Activar una
tarjeta permite operarla desde el panel, pero no inicia procesos ni ejecuta
`systemctl`, Docker o comandos arbitrarios.

## Instalación independiente

Requiere Python 3.10 o posterior. Desde este directorio:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python control_panel.py
```

El panel escucha de forma predeterminada en `http://127.0.0.1:8790`. Se puede
cambiar la interfaz o el puerto sin crear archivos con secretos:

```bash
CONTROLPANEL_HOST=0.0.0.0 CONTROLPANEL_PORT=8790 \
  .venv/bin/python control_panel.py
```

## Configuración

Las aplicaciones independientes se consultan por defecto en:

```text
Farmacias:  http://127.0.0.1:8788
Emergencias: http://127.0.0.1:8789
```

Se pueden cambiar con `FARMACIAS_PANEL_URL` y `EMERGENCIAS_PANEL_URL`. La ruta
del estado se puede cambiar con `CONTROLPANEL_STATE`; no contiene credenciales.

## Servicio systemd opcional

Revise primero el usuario y las rutas de la unidad incluida y después instálela:

```bash
sudo cp systemd/meshnet-control-panel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshnet-control-panel.service
```

La unidad incluida escucha solamente en `127.0.0.1`. Para publicar el panel en
la red debe configurarse conscientemente otra interfaz y aplicar autenticación o
un proxy inverso adecuado.

## Pruebas

```bash
python3 -m pytest -q tools/ControlPanel/tests
```
