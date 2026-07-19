# Pasarela correo ↔ malla (`email-to-mesh`)

Este documento describe el funcionamiento de la pasarela de correo integrada en
MeshNet-Bot. Incluye la funcionalidad existente **correo → malla** y la nueva
funcionalidad **malla → correo**, junto con ejemplos de configuración, uso desde
CLI, uso desde Meshtastic/MeshCore y uso desde el bot de Telegram.

## 1. Arquitectura general

La integración se apoya en tres piezas:

1. **Servicio `email-to-mesh`** (`source/email_to_mesh.py`):
   - Vigila una cuenta IMAP.
   - Convierte correos autorizados en mensajes de malla.
   - Mantiene la libreta de contactos en una BDD JSON.
   - Envía correos salientes por SMTP cuando recibe una orden `[mail]` o un
     comando CLI/bot.
2. **Broker** (`source/Meshtastic_Broker.py`):
   - Recibe mensajes de Meshtastic y MeshCore.
   - Detecta comandos `[mail] ...` independientemente de si llegan por
     Meshtastic o por MeshCore.
   - Llama a la lógica común de `email-to-mesh` y responde en la malla con el
     resultado.
3. **Bot de Telegram** (`source/Telegram_Bot_Broker.py`):
   - Permite gestionar la misma BDD de contactos.
   - Permite enviar correos desde Telegram usando `/mail`.

La BDD de contactos es un archivo JSON compartido, por defecto:

```text
/app/bot_data/email_contacts.json
```

En Docker se conserva porque el servicio `email-to-mesh` y el broker montan
`./bot_data:/app/bot_data:rw`.

## 2. Configuración

### 2.1 Variables para correo → malla (IMAP)

Estas variables ya existían para leer correos entrantes y reenviarlos a la
malla:

```env
EMAIL_TO_MESH_ENABLED=1
EMAIL_IMAP_HOST=imap.example.org
EMAIL_IMAP_PORT=993
EMAIL_IMAP_SSL=1
EMAIL_IMAP_STARTTLS=0
EMAIL_IMAP_USER=cuenta@example.org
EMAIL_IMAP_PASSWORD=token_o_contrasena_de_aplicacion
EMAIL_IMAP_FOLDER=INBOX
EMAIL_ALLOWED_SENDERS=avisos@example.org;admin@example.org
EMAIL_PROCESS_EXISTING=0
EMAIL_MARK_AS_READ=1
EMAIL_DELETE_AFTER_SEND=0
EMAIL_MESH_CHANNEL=0
EMAIL_MESH_PREFIX=[EMAIL]
EMAIL_MESH_NETWORK=
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766
```

Notas importantes:

- `EMAIL_ALLOWED_SENDERS` es obligatorio si `EMAIL_TO_MESH_ENABLED=1`; solo esos
  remitentes podrán inyectar mensajes a la malla.
- `EMAIL_PROCESS_EXISTING=0` crea una línea base en el primer arranque y solo
  procesa correos nuevos. Para procesar lo que ya esté en el buzón, usar
  `EMAIL_PROCESS_EXISTING=1` conscientemente.
- `EMAIL_MESH_NETWORK` puede dejarse vacío para modo automático, o forzarse a
  `meshtastic`/`meshcore`.

### 2.2 Variables para malla → correo (SMTP y contactos)

La nueva funcionalidad necesita configurar SMTP y la ruta de contactos:

```env
EMAIL_CONTACTS_PATH=/app/bot_data/email_contacts.json
EMAIL_SMTP_HOST=smtp.example.org
EMAIL_SMTP_PORT=587
EMAIL_SMTP_SSL=0
EMAIL_SMTP_STARTTLS=1
EMAIL_SMTP_USER=cuenta@example.org
EMAIL_SMTP_PASSWORD=token_o_contrasena_de_aplicacion
EMAIL_FROM=cuenta@example.org
EMAIL_OUT_SUBJECT_PREFIX=[Mesh]
```

Notas:

- Si el proveedor usa puerto 465, normalmente se configura:

  ```env
  EMAIL_SMTP_PORT=465
  EMAIL_SMTP_SSL=1
  EMAIL_SMTP_STARTTLS=0
  ```

- Si el proveedor usa puerto 587, normalmente se configura:

  ```env
  EMAIL_SMTP_PORT=587
  EMAIL_SMTP_SSL=0
  EMAIL_SMTP_STARTTLS=1
  ```

- `EMAIL_SMTP_USER`, `EMAIL_SMTP_PASSWORD` y `EMAIL_FROM` pueden coincidir con
  la cuenta IMAP, pero no es obligatorio.

- Gmail devuelve `535 5.7.8 Username and Password not accepted` cuando rechaza
  las credenciales SMTP. Si ya se usa una **contraseña de aplicación**, comprobar
  que `EMAIL_SMTP_USER` es la misma cuenta que generó esa contraseña, que el
  contenedor del bot/broker ha cargado la `.env` actualizada y que
  `EMAIL_SMTP_PASSWORD` no incluye espacios ni saltos de línea al copiarla.

## 3. Correo → malla: funcionamiento existente

El servicio `email-to-mesh` abre la cuenta IMAP, consulta los correos nuevos y
procesa únicamente los remitentes autorizados. El asunto del correo se convierte
en el texto que se envía a la malla.

### 3.1 Asuntos sin encaminamiento explícito

Correo recibido:

```text
From: avisos@example.org
Subject: Repetidor operativo en zona norte
```

Mensaje enviado a la malla:

```text
[EMAIL] Repetidor operativo en zona norte
```

El canal será `EMAIL_MESH_CHANNEL` y la red será la configurada por
`EMAIL_MESH_NETWORK` o la inferida por `RADIO_PROFILE`.

### 3.2 Enviar a canal Meshtastic concreto

Correo recibido:

```text
Subject: [ch3] Reunión hoy a las 20:00
```

Resultado:

```text
[EMAIL] Reunión hoy a las 20:00
```

en el canal Meshtastic `3`.

### 3.3 Enviar a canal MeshCore concreto

Correo recibido:

```text
Subject: [ch2]M Mensaje para canal MeshCore 2
```

Resultado:

```text
[EMAIL] Mensaje para canal MeshCore 2
```

en el canal MeshCore `2`.

La `M` debe ir pegada al cierre del corchete: `[ch2]M`.

### 3.4 Control de duplicados y persistencia

El servicio mantiene un estado persistente en:

```text
/app/bot_data/email_to_mesh_state.json
```

Ese estado guarda:

- `UIDVALIDITY` del buzón.
- Último UID procesado.
- Lista limitada de `Message-ID` recientes.

Así se evitan reenvíos normales tras reinicios del contenedor.

## 4. Malla → correo: nueva funcionalidad

La nueva funcionalidad permite enviar un mensaje desde la malla a un contacto de
correo guardado.

### 4.1 Formato desde Meshtastic o MeshCore

Formato principal:

```text
[mail] contacto texto mensaje
```

También se aceptan:

```text
/mail contacto texto mensaje
mail contacto texto mensaje
```

Ejemplos:

```text
[mail] eb2eas Estoy en cobertura por la zona norte
[mail] 1 Mensaje enviado usando el primer contacto de la lista
/mail soporte El nodo remoto vuelve a responder
mail casa Llegaré tarde
```

El broker recibe el mensaje por Meshtastic o MeshCore, busca el contacto en la
BDD compartida y envía el correo por SMTP.

### 4.2 Consultar contactos desde la malla

Para listar contactos desde Meshtastic o MeshCore:

```text
[mail] lista
```

También sirven:

```text
[mail] contactos
[mail] ls
/mail lista
```

Respuesta esperada en la malla:

```text
Contactos de correo:
1. eb2eas <eb2eas@example.org> [eb2eas]
2. soporte <soporte@example.org> [soporte]
```

### 4.3 Envío por número o por nombre

Si la lista devuelve:

```text
1. eb2eas <eb2eas@example.org> [eb2eas]
2. soporte <soporte@example.org> [soporte]
```

Estos dos comandos son equivalentes para el primer contacto:

```text
[mail] 1 Prueba por número
[mail] eb2eas Prueba por nombre
```

También se permite resolver por prefijo si no hay ambigüedad. Por ejemplo,
`[mail] eb ...` puede resolver `eb2eas` si no existe otro contacto que empiece
por `eb`.

## 5. Gestión de contactos por CLI en Docker

Los comandos se ejecutan contra el contenedor `email-to-mesh`. Ejemplos con
`docker compose`:

### 5.1 Añadir o actualizar contacto

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py contact-add eb2eas eb2eas@example.org
```

Alias corto:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py add eb2eas eb2eas@example.org
```

### 5.2 Editar correo de un contacto

Por nombre:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py contact-edit eb2eas nuevo@example.org
```

Por número:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py contact-edit 1 nuevo@example.org
```

Alias corto:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py edit 1 nuevo@example.org
```

### 5.3 Eliminar contacto

Por nombre:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py contact-del eb2eas
```

Por número:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py contact-del 1
```

Alias cortos:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py del eb2eas
docker compose exec email-to-mesh python /app/source/email_to_mesh.py rm eb2eas
```

### 5.4 Listar contactos

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py contacts
```

Alias:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py list
docker compose exec email-to-mesh python /app/source/email_to_mesh.py ls
```

### 5.5 Enviar correo desde CLI

Por nombre:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py send eb2eas "Mensaje desde CLI"
```

Por número:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py send 1 "Mensaje desde CLI por número"
```

## 6. Gestión desde el bot de Telegram

Si el bot está activo, se añaden comandos que usan la misma BDD JSON:

### 6.1 Listar contactos

```text
/mail_contactos
```

### 6.2 Añadir contacto

```text
/mail_add eb2eas eb2eas@example.org
```

### 6.3 Editar contacto

```text
/mail_edit eb2eas nuevo@example.org
/mail_edit 1 nuevo@example.org
```

### 6.4 Eliminar contacto

```text
/mail_del eb2eas
/mail_del 1
```

### 6.5 Enviar correo desde Telegram

```text
/mail eb2eas Mensaje enviado desde Telegram
/mail 1 Mensaje enviado al primer contacto
```

## 7. Validación operativa recomendada

Después de configurar `.env`, se recomienda validar en este orden:

1. Levantar broker y `email-to-mesh`.
2. Crear un contacto de prueba:

   ```bash
   docker compose exec email-to-mesh python /app/source/email_to_mesh.py contact-add test test@example.org
   ```

3. Listar contactos:

   ```bash
   docker compose exec email-to-mesh python /app/source/email_to_mesh.py contacts
   ```

4. Probar SMTP desde CLI:

   ```bash
   docker compose exec email-to-mesh python /app/source/email_to_mesh.py send test "Prueba SMTP desde MeshNet"
   ```

5. Probar desde Meshtastic o MeshCore:

   ```text
   [mail] test Prueba enviada desde la malla
   ```

6. Probar consulta desde Meshtastic o MeshCore:

   ```text
   [mail] lista
   ```

7. Enviar un correo entrante autorizado a la cuenta IMAP y comprobar que aparece
   en la malla con el prefijo `EMAIL_MESH_PREFIX`.

## 8. Errores habituales

### 8.1 `faltan variables SMTP`

El envío malla→correo requiere, como mínimo:

```env
EMAIL_SMTP_HOST=
EMAIL_SMTP_USER=
EMAIL_SMTP_PASSWORD=
EMAIL_FROM=
```

### 8.2 `contacto no encontrado`

El nombre no existe o el número está fuera de rango. Primero consultar:

```text
[mail] lista
```

o por CLI:

```bash
docker compose exec email-to-mesh python /app/source/email_to_mesh.py contacts
```

### 8.3 `contacto ambiguo`

El prefijo escrito coincide con varios contactos. Usar el nombre completo o el
número de la lista.

### 8.4 El correo entrante no se reenvía a la malla

Comprobar:

- `EMAIL_TO_MESH_ENABLED=1`.
- Credenciales IMAP.
- Que el remitente está incluido exactamente en `EMAIL_ALLOWED_SENDERS`.
- Logs del contenedor `email-to-mesh`.

### 8.5 El correo saliente no llega

Comprobar:

- Credenciales SMTP.
- Si el proveedor necesita contraseña de aplicación.
- Si corresponde usar puerto 587 con STARTTLS o 465 con SSL.
- Carpeta de spam del destinatario.
- Logs del contenedor.

## 9. Resumen rápido de comandos

| Origen | Comando | Acción |
| --- | --- | --- |
| Malla | `[mail] lista` | Lista contactos |
| Malla | `[mail] contacto texto` | Envía correo al contacto |
| Malla | `[mail] 1 texto` | Envía correo al contacto número 1 |
| CLI | `email_to_mesh.py contacts` | Lista contactos |
| CLI | `email_to_mesh.py contact-add nombre correo` | Añade/actualiza contacto |
| CLI | `email_to_mesh.py contact-edit nombre_o_num correo` | Edita contacto |
| CLI | `email_to_mesh.py contact-del nombre_o_num` | Elimina contacto |
| CLI | `email_to_mesh.py send nombre_o_num "texto"` | Envía correo |
| Bot | `/mail_contactos` | Lista contactos |
| Bot | `/mail_add nombre correo` | Añade/actualiza contacto |
| Bot | `/mail_edit nombre_o_num correo` | Edita contacto |
| Bot | `/mail_del nombre_o_num` | Elimina contacto |
| Bot | `/mail nombre_o_num texto` | Envía correo |
