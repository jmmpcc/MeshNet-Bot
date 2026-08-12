# MeshNet Mobile API v1 — v7.0.54

API REST independiente para `MeshNet-Mobile`.

## Objetivo

Proporcionar un contrato estable y seguro para la aplicación Android sin modificar el comportamiento del ControlPanel ni del Web Admin existente.

La superficie actual continúa siendo de **solo lectura**. No expone acciones `systemd`, escritura de `.env`, envío de mensajes ni cambios de configuración.

## Puerto

Por defecto se utiliza `8791`. El ControlPanel conserva su puerto y rutas sin cambios.

## Versión de MeshNet-Bot

Desde v7.0.54 la API deja de estar anclada a la versión con la que fue creada. Si `MESHNET_BOT_VERSION` no está definida, detecta automáticamente el changelog numérico más reciente de `docs/`.

`MESHNET_BOT_VERSION` sigue disponible como override explícito para despliegues especiales.

## Variables

Crear un EnvironmentFile protegido, por ejemplo:

```text
/home/meshnet/MeshNet-Bot/tools/MobileAPI/.env
```

Contenido mínimo:

```env
MESHNET_MOBILE_API_TOKEN=GENERAR_UN_TOKEN_LARGO_Y_ALEATORIO
```

Override opcional:

```env
MESHNET_BOT_VERSION=v7.0.54
```

No subir `.env` al repositorio.

## Ejecución manual

```bash
python3 -m uvicorn tools.MobileAPI.mobile_api_v7054:app --host 0.0.0.0 --port 8791
```

## Comprobación desde el PC

```bash
curl http://IP_RASPBERRY:8791/api/v1/health
```

Rutas protegidas:

```bash
curl -H "Authorization: Bearer TU_TOKEN" http://IP_RASPBERRY:8791/api/v1/system/overview
curl -H "Authorization: Bearer TU_TOKEN" http://IP_RASPBERRY:8791/api/v1/capabilities
```

## Endpoints actuales

- `GET /api/v1/health`
- `GET /api/v1/capabilities`
- `GET /api/v1/system/overview`
- `GET /api/v1/services`
- `GET /api/v1/messages`
- `GET /api/v1/emergencies/overview`
- `GET /api/v1/nodes/meshcore`
- `GET /api/v1/nodes/meshtastic`

`/api/v1/capabilities` permite que la app Android descubra qué funciones están disponibles y no muestre controles de escritura contra servidores que aún sean de solo lectura.

Los endpoints de nodos conservan el contrato estable pero continúan con `available=false` hasta enlazar una fuente de datos existente y probada. No se inventan datos ni se duplica el estado de nodos.

`POST /api/v1/messages/send` y el control de servicios permanecen desactivados hasta completar las fases de lectura y autenticación por permisos.

## Seguridad

- Todas las rutas excepto `/api/v1/health` requieren token Bearer.
- Si `MESHNET_MOBILE_API_TOKEN` no está configurado, las rutas protegidas devuelven `503`.
- La API no devuelve secretos ni el contenido completo de ningún `.env`.
- No exponer el puerto 8791 directamente a Internet. Para acceso remoto usar VPN/Tailscale/WireGuard o proxy HTTPS autenticado.

## Pruebas

```bash
python3 -m pytest tools/MobileAPI/tests/test_mobile_api.py -v
```

La validación completa debe incluir además las pruebas actuales de ControlPanel:

```bash
python3 -m unittest discover -s tools/ControlPanel/tests -p 'test_*.py'
```
