# MeshNet Mobile API v1 — fase A1

API REST independiente para `MeshNet-Mobile`.

## Objetivo

Proporcionar un contrato estable y seguro para la aplicación Android sin modificar el comportamiento del ControlPanel ni del Web Admin existente.

Esta fase es de **solo lectura**. No expone acciones `systemd`, escritura de `.env`, envío de mensajes ni cambios de configuración.

## Puerto

Por defecto se propone `8791`. El ControlPanel actual conserva su puerto y rutas sin cambios.

## Variables

Crear un EnvironmentFile protegido, por ejemplo:

```text
/home/meshnet/MeshNet-Bot/tools/MobileAPI/.env
```

Contenido mínimo:

```env
MESHNET_MOBILE_API_TOKEN=GENERAR_UN_TOKEN_LARGO_Y_ALEATORIO
MESHNET_BOT_VERSION=v7.0.49
```

No subir `.env` al repositorio.

## Ejecución manual

```bash
python3 -m uvicorn tools.MobileAPI.mobile_api:app --host 0.0.0.0 --port 8791
```

## Comprobación desde el PC

```bash
curl http://IP_RASPBERRY:8791/api/v1/health
```

Rutas protegidas:

```bash
curl -H "Authorization: Bearer TU_TOKEN" http://IP_RASPBERRY:8791/api/v1/system/overview
```

## Endpoints A1

- `GET /api/v1/health`
- `GET /api/v1/system/overview`
- `GET /api/v1/services`
- `GET /api/v1/messages`
- `GET /api/v1/emergencies/overview`
- `GET /api/v1/nodes/meshcore`
- `GET /api/v1/nodes/meshtastic`

Los endpoints de nodos publican ya el contrato estable, pero en A1 devuelven `available=false`. No se inventan datos ni se enlaza todavía un proveedor hasta identificar y probar la fuente existente correcta.

`POST /api/v1/messages/send` queda expresamente fuera de A1. Se incorporará después de validar esta primera superficie de lectura.

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
