# MeshNet Mobile API v1 — v7.0.58

API REST independiente para `MeshNet-Mobile`.

## Objetivo

Proporcionar un contrato estable y seguro para la aplicación Android sin modificar el comportamiento del ControlPanel ni del Web Admin existente.

La superficie funcional continúa siendo de **solo lectura**. No expone acciones `systemd`, escritura de `.env`, envío de mensajes ni cambios de configuración.

Desde v7.0.58 se añade autenticación por **usuario/contraseña + sesiones** en paralelo al Bearer fijo histórico. El Bearer existente no se elimina en esta fase para conservar compatibilidad con la app Android y herramientas ya desplegadas.

## Puerto

Por defecto se utiliza `8791`. El ControlPanel conserva su puerto y rutas sin cambios.

## Versión de MeshNet-Bot

La API detecta automáticamente el changelog numérico más reciente de `docs/` cuando `MESHNET_BOT_VERSION` no está definida.

`MESHNET_BOT_VERSION` sigue disponible como override explícito para despliegues especiales.

## Variables

EnvironmentFile protegido, por ejemplo:

```text
/home/meshnet/MeshNet-Bot/tools/MobileAPI/.env
```

Contenido mínimo durante la migración v7.0.58:

```env
MESHNET_MOBILE_API_TOKEN=GENERAR_UN_TOKEN_LARGO_Y_ALEATORIO
```

El Bearer fijo debe mantenerse configurado durante la Fase 2.6.1 porque la nueva capa de sesiones delega internamente en la API histórica ya validada.

Variables opcionales de sesión:

```env
# Por defecto: tools/MobileAPI/data/mobile_users.json
MESHNET_MOBILE_AUTH_USERS_FILE=/home/meshnet/MeshNet-Bot/tools/MobileAPI/data/mobile_users.json

# Por defecto: tools/MobileAPI/data/mobile_auth.db
MESHNET_MOBILE_AUTH_DB=/home/meshnet/MeshNet-Bot/tools/MobileAPI/data/mobile_auth.db

# Por defecto: 3600 segundos
MESHNET_MOBILE_ACCESS_TTL_SECONDS=3600

# Por defecto: 30 días
MESHNET_MOBILE_REFRESH_TTL_SECONDS=2592000
```

No subir `.env`, `mobile_users.json` ni `mobile_auth.db` al repositorio.

## Crear el primer usuario

La contraseña nunca se escribe en línea de comandos ni se guarda en claro.

```bash
cd /home/meshnet/MeshNet-Bot
python3 -m tools.MobileAPI.mobile_auth user-set jmmol --role admin
```

La utilidad solicitará dos veces la contraseña y almacenará únicamente un hash `scrypt` con salt aleatorio.

Roles admitidos:

- `viewer`
- `operator`
- `admin`

En v7.0.58 el rol es informativo y prepara autorización futura; los endpoints existentes continúan en modo de solo lectura.

Otros comandos:

```bash
python3 -m tools.MobileAPI.mobile_auth user-list
python3 -m tools.MobileAPI.mobile_auth user-disable jmmol
python3 -m tools.MobileAPI.mobile_auth user-enable jmmol
```

Deshabilitar un usuario revoca también sus sesiones activas.

## Ejecución manual v7.0.58

```bash
python3 -m uvicorn tools.MobileAPI.mobile_api_v7058:app --host 0.0.0.0 --port 8791
```

La v7.0.58 monta la aplicación v7.0.54 existente detrás de una capa de autenticación. Los endpoints históricos no se reescriben.

## Flujo de autenticación

### 1. Login

```bash
curl -X POST http://IP_RASPBERRY:8791/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"jmmol","password":"TU_PASSWORD"}'
```

Respuesta:

```json
{
  "ok": true,
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "bearer",
  "expires_in": 3600,
  "refresh_expires_in": 2592000,
  "username": "jmmol",
  "role": "admin"
}
```

### 2. Usar access token

```bash
curl -H "Authorization: Bearer ACCESS_TOKEN" \
  http://IP_RASPBERRY:8791/api/v1/capabilities
```

### 3. Renovar sesión

```bash
curl -X POST http://IP_RASPBERRY:8791/api/v1/auth/refresh \
  -H 'Content-Type: application/json' \
  -d '{"refresh_token":"REFRESH_TOKEN"}'
```

El refresh token se rota. Después de una renovación, los tokens de la familia anterior quedan revocados.

### 4. Identidad de la sesión

```bash
curl -H "Authorization: Bearer ACCESS_TOKEN" \
  http://IP_RASPBERRY:8791/api/v1/auth/me
```

### 5. Logout

```bash
curl -X POST http://IP_RASPBERRY:8791/api/v1/auth/logout \
  -H 'Content-Type: application/json' \
  -d '{"token":"REFRESH_TOKEN"}'
```

Logout revoca la familia completa `access + refresh`.

## Compatibilidad Bearer histórica

El método anterior sigue funcionando sin cambios:

```bash
curl -H "Authorization: Bearer TU_TOKEN_FIJO" \
  http://IP_RASPBERRY:8791/api/v1/capabilities
```

No retirar `MESHNET_MOBILE_API_TOKEN` hasta completar y validar la migración Android.

## Endpoints actuales

Autenticación v7.0.58:

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/me`

Contrato existente, conservado:

- `GET /api/v1/health`
- `GET /api/v1/capabilities`
- `GET /api/v1/system/overview`
- `GET /api/v1/services`
- `GET /api/v1/messages`
- `GET /api/v1/emergencies/overview`
- `GET /api/v1/emergencies`
- `GET /api/v1/nodes/meshcore`
- `GET /api/v1/nodes/meshtastic`

`/api/v1/capabilities` continúa permitiendo que Android active u oculte funciones según el backend real.

## Persistencia y seguridad

- Contraseñas: `scrypt` + salt aleatorio; nunca texto plano ni cifrado reversible.
- Access token: valor opaco aleatorio; la base sólo conserva `SHA-256(token)`.
- Refresh token: valor opaco aleatorio; la base sólo conserva `SHA-256(token)`.
- Sesiones: SQLite local no versionado.
- Refresh: rotación obligatoria de la familia anterior.
- Logout: revocación de la familia completa.
- Bearer histórico: continúa temporalmente para compatibilidad.
- Puerto `8791`: no exponer directamente a Internet; para acceso remoto usar Tailscale/WireGuard o HTTPS autenticado.

## Pruebas

Contrato histórico:

```bash
python3 -m pytest tools/MobileAPI/tests/test_mobile_api.py -v
```

Autenticación de sesión:

```bash
python3 -m pytest tools/MobileAPI/tests/test_mobile_auth.py -v
```

Prueba conjunta recomendada:

```bash
python3 -m pytest tools/MobileAPI/tests -v
```

La validación completa debe conservar además las pruebas actuales del ControlPanel:

```bash
python3 -m unittest discover -s tools/ControlPanel/tests -p 'test_*.py'
```
