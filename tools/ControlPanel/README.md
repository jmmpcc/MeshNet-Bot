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
- muestra resultados JSON, texto y errores con salida y timeout limitados;
- exige confirmación para paradas, reinicios, publicaciones y notificaciones.

Los manifiestos incluidos cubren Farmacias y Emergencias: estado, actualización,
listados actuales, histórico, fuentes, áreas, categorías, diagnóstico,
publicación/avisos y control de sus API. Para añadir una aplicación futura se
instala otro JSON en `manifests/`; no es necesario modificar `web_admin.py`.
El navegador nunca puede enviar un comando, argumentos, ruta o unidad systemd.

## Instalación

```bash
cd tools/ControlPanel
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export CONTROLPANEL_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python control_panel.py
```

Escucha por defecto en `http://127.0.0.1:8790`. Variables:

- `CONTROLPANEL_HOST` y `CONTROLPANEL_PORT`: escucha;
- `CONTROLPANEL_TOKEN`: secreto obligatorio para habilitar o ejecutar acciones;
- `CONTROLPANEL_STATE`: archivo de estado;
- `CONTROLPANEL_MANIFESTS`: directorio alternativo de manifiestos.

Sin token, las consultas siguen disponibles localmente, pero cualquier escritura
devuelve `503`. Para acceso en red use HTTPS mediante un proxy autenticado. El
token se conserva solamente en `sessionStorage` del navegador.

## systemd y permisos

La unidad `systemd/meshnet-control-panel.service` inicia el panel y lee
`/home/meshnet/.config/meshnet-control-panel.env`. Guarde allí
`CONTROLPANEL_TOKEN=...` con permisos `0600`.

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

## Pruebas

```bash
python3 -m pytest -q tools/ControlPanel/tests
```
