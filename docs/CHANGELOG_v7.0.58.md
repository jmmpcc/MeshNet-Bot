# MeshNet-Bot v7.0.58

## MeshNet Mobile API — Fase 2.6.1

Se incorpora autenticación por usuario/contraseña y sesiones persistentes para preparar la eliminación del token Bearer visible en MeshNet-Mobile, manteniendo intacta la compatibilidad con el Bearer fijo actual.

### Añadido

- `tools/MobileAPI/mobile_auth.py`:
  - usuarios locales con hash `scrypt` y salt aleatorio;
  - CLI para crear, habilitar, deshabilitar y listar usuarios;
  - access tokens opacos;
  - refresh tokens opacos;
  - persistencia SQLite de sesiones mediante huellas SHA-256;
  - rotación de refresh tokens;
  - revocación por familia y por usuario;
  - roles `viewer`, `operator`, `admin` preparados para autorización futura.

- `tools/MobileAPI/mobile_api_v7058.py`:
  - `POST /api/v1/auth/login`;
  - `POST /api/v1/auth/refresh`;
  - `POST /api/v1/auth/logout`;
  - `GET /api/v1/auth/me`;
  - traducción interna de access token de sesión al Bearer fijo ya validado antes de delegar en la API histórica.

### Compatibilidad

- `mobile_api.py` no se modifica.
- `mobile_api_v7054.py` no se modifica.
- `MESHNET_MOBILE_API_TOKEN` continúa siendo válido y obligatorio durante esta fase de migración.
- No cambian los endpoints de sistema, servicios, mensajes, emergencias o nodos.
- No se añaden operaciones mutantes a la Mobile API.
- ControlPanel, Web Admin, dispatchers, radio y `.env` existentes no se modifican.

### Seguridad

- Las contraseñas no se almacenan en claro ni mediante cifrado reversible.
- `mobile_users.json` y `mobile_auth.db` están excluidos de Git.
- La base de sesiones no almacena access/refresh tokens en claro.
- Un refresh utilizado rota y revoca su familia anterior.
- Deshabilitar un usuario revoca sus sesiones activas.

### Pruebas

Se añade `tools/MobileAPI/tests/test_mobile_auth.py` para verificar:

- hashing y verificación scrypt;
- ausencia de contraseña en claro en el almacén;
- login correcto e incorrecto;
- uso de access token contra `/api/v1/capabilities` existente;
- compatibilidad del Bearer fijo histórico;
- rotación de refresh token;
- `/auth/me`;
- logout y revocación.

### Despliegue

El nuevo punto de entrada será:

```bash
python3 -m uvicorn tools.MobileAPI.mobile_api_v7058:app --host 0.0.0.0 --port 8791
```

Antes de cambiar el servicio permanente se debe crear al menos un usuario con:

```bash
python3 -m tools.MobileAPI.mobile_auth user-set USUARIO --role admin
```

La app Android seguirá usando temporalmente el Bearer fijo hasta completar la Fase 2.6.2.
