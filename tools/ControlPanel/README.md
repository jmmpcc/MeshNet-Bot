# MeshNet ControlPanel v7.0.35

Aplicación web independiente para administrar componentes auxiliares de MeshNet-Bot desde el host. Utiliza manifiestos para describir servicios, archivos de configuración y acciones permitidas.

## Ruta oficial

```text
/home/meshnet/MeshNet-Bot/tools/ControlPanel
```

## Funciones

- estado y acciones sobre servicios systemd;
- configuración controlada de aplicaciones independientes;
- formularios específicos para farmacias y emergencias;
- consulta de logs y diagnóstico;
- paneles de nodos MeshCore cuando la fuente de datos está disponible;
- protección de operaciones mediante reglas y manifiestos.

## Instalación

```bash
cd /home/meshnet/MeshNet-Bot/tools/ControlPanel
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
chmod +x install_control_panel_service.sh start_control_panel.sh
sudo ./install_control_panel_service.sh
```

Comprobación:

```bash
sudo systemctl status meshnet-control-panel.service --no-pager
curl -fsS http://127.0.0.1:8790/health
```

## Configuración

La unidad carga opcionalmente:

```text
/home/meshnet/MeshNet-Bot/tools/ControlPanel/.env
```

Valores habituales:

```env
CONTROLPANEL_HOST=0.0.0.0
CONTROLPANEL_PORT=8790
```

No exponer el panel directamente a Internet. Usar firewall, proxy autenticado o VPN cuando se acceda fuera de la LAN.

## Servicio systemd

```bash
sudo systemctl restart meshnet-control-panel.service
sudo systemctl stop meshnet-control-panel.service
sudo systemctl start meshnet-control-panel.service
journalctl -u meshnet-control-panel.service -f
```

## Reinstalación del servicio

```bash
cd /home/meshnet/MeshNet-Bot/tools/ControlPanel
sudo systemctl disable --now meshnet-control-panel.service
sudo rm -f /etc/systemd/system/meshnet-control-panel.service
sudo ./install_control_panel_service.sh
```

## Manifiestos

Directorio:

```text
tools/ControlPanel/manifests/
```

Cada manifiesto debe identificar con precisión:

- aplicación y ruta base;
- archivo `.env` administrado;
- unidades systemd permitidas;
- campos editables y validación;
- acciones de diagnóstico.

No añadir acciones arbitrarias de shell desde datos aportados por el navegador.

## Página vacía o error 500

```bash
journalctl -u meshnet-control-panel.service -n 200 --no-pager
cd /home/meshnet/MeshNet-Bot/tools/ControlPanel
.venv/bin/python -m py_compile control_panel.py web_admin.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Después de resolver conflictos Git, buscar marcadores:

```bash
grep -RInE '^(<<<<<<<|=======|>>>>>>>)' .
```

## Permisos

La unidad se ejecuta como `meshnet`. Las operaciones privilegiadas deben limitarse mediante las reglas instaladas por el proyecto. No ejecutar el servidor web completo como `root`.

## Pruebas

```bash
cd /home/meshnet/MeshNet-Bot/tools/ControlPanel
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

## Actualización

```bash
cd /home/meshnet/MeshNet-Bot
git pull --ff-only
cd tools/ControlPanel
.venv/bin/pip install -r requirements.txt
sudo ./install_control_panel_service.sh
sudo systemctl restart meshnet-control-panel.service
```


## Mensajes emitidos — v7.0.48

El ControlPanel incorpora un journal común de entregas en `bot_data/delivery_audit.db`.
La pantalla **Mensajes emitidos** agrupa por operación lógica y muestra fecha/hora,
aplicación, fuente, mensaje, transportes y resultado. Al desplegar una fila se ve
el detalle de cada entrega física. Los filtros permiten seleccionar aplicación,
fuente, medio, resultado y periodo, con exportación CSV.

La auditoría es estrictamente best-effort: una avería, bloqueo o falta de permisos
en SQLite nunca invalida ni retrasa el envío original de la aplicación.
