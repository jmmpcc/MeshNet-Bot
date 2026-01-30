# Manual paso a paso: instalación y puesta en marcha (Raspberry Pi 2B / 3 / 4 / 5)

Este manual instala el proyecto **MeshNet** de forma repetible sobre una instalación limpia de **Raspberry Pi OS Lite** y lo deja arrancando 24/7 con Docker Compose.

---

## 0) Material necesario

- Raspberry Pi (2B / 3 / 4 / 5)
- Tarjeta microSD (mínimo 16 GB; recomendado 32 GB)
- Fuente de alimentación adecuada
- Conexión a Internet (Ethernet o Wi‑Fi)
- Un PC con Windows/macOS/Linux para preparar la SD
- (Opcional) Cable Ethernet para el primer arranque

---

## 1) Elegir el sistema operativo correcto

### Raspberry Pi 2B (recomendado)
- **Raspberry Pi OS Lite (32-bit)**

Motivo: la Pi 2B trabaja en ARMv7 y suele ser lo más compatible y ligero.

### Raspberry Pi 3 / 4 / 5 (recomendado)
- **Raspberry Pi OS Lite (64-bit)**

Motivo: mejor rendimiento, soporte moderno y mejor compatibilidad con imágenes Docker arm64.

---

## 2) Grabar la microSD con Raspberry Pi Imager (modo “sin pantalla”)

1. Instala **Raspberry Pi Imager** (oficial).
2. Inserta la microSD en el PC.
3. En Imager:
   - **Raspberry Pi Device**: el tuyo (no es crítico si no aparece).
   - **Operating System**: selecciona el OS del apartado 1.
   - **Storage**: selecciona tu microSD.
4. En **Settings / Personalización** (antes de escribir):
   - **Set hostname**: `meshnet` (o el nombre que quieras)
   - **Enable SSH**: activado
   - **Set username and password**: crea usuario y contraseña (ej. usuario `meshnet`)
   - **Configure wireless LAN** (si usarás Wi‑Fi):
     - SSID y contraseña
     - País: `ES`
   - **Time zone**: `Europe/Madrid`
   - (Opcional) **Locale**: `es-ES`
5. Pulsa **Write**.

Referencia (contexto de configuración headless y creación de usuario/SSH en versiones actuales):  
- Raspberry Pi docs / foros (sin usuario por defecto en releases modernas): https://support.pishop.us/article/58-default-raspbian-password  
- Headless con Imager (Wi‑Fi + SSH): https://www.developernation.net/blog/headless-raspberry-pi-setup-wifi-and-ssh/  
- Getting started oficial: https://www.raspberrypi.com/documentation/computers/getting-started.html

---

## 3) Primer arranque y conexión por red

1. Inserta la SD en la Raspberry Pi.
2. Conecta:
   - **Ethernet** (recomendado para el primer arranque) **o**
   - Wi‑Fi (si lo configuraste en Imager)
3. Enciende la Pi y espera 2–3 minutos.

### Obtener la IP
- Entra en el router y mira “clientes DHCP” buscando `meshnet`.
- Alternativa habitual en redes con mDNS:
  - `ssh meshnet.local` (puede funcionar sin saber la IP)

---

## 4) Entrar por SSH

Desde tu PC:

```bash
ssh meshnet@<IP_DE_LA_RPI>
```

Ejemplo:

```bash
ssh meshnet@192.168.1.50
```

---

## 5) Preparación base del sistema

Dentro de la Raspberry Pi:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Vuelve a entrar por SSH tras reiniciar.

---

## 6) Configurar red (Wi‑Fi y/o Ethernet)

### 6.1 Ver interfaces e IP (rápido)

```bash
ip a
```

### 6.2 Activar/ajustar Wi‑Fi (método simple)

En Raspberry Pi OS:

```bash
sudo raspi-config
```

Ruta típica:
- System Options / Wireless LAN (según versión)

### 6.3 Configurar Wi‑Fi por archivo (útil si no hay menú)

Edita (según sistema) y reinicia red:

```bash
sudo nano /etc/wpa_supplicant/wpa_supplicant.conf
```

Añade al final:

```conf
country=ES
network={
  ssid="TU_SSID"
  psk="TU_PASSWORD"
}
```

Reinicia:

```bash
sudo reboot
```

### 6.4 IP fija (opcional)

En Bookworm suele usarse NetworkManager. Si lo tienes, configura IP fija con `nmcli`:

```bash
nmcli con show
```

Identifica la conexión (ej. `Wired connection 1`) y aplica IP:

```bash
sudo nmcli con mod "Wired connection 1" ipv4.method manual ipv4.addresses 192.168.1.50/24 ipv4.gateway 192.168.1.1 ipv4.dns "1.1.1.1 8.8.8.8"
sudo nmcli con up "Wired connection 1"
```

---

## 7) Instalar Docker y Docker Compose

### 7.1 Instalación recomendada (repositorio oficial)

Para Raspberry Pi OS 32-bit (Pi 2B) hay documentación específica.  
Para 64-bit se usa el método Debian/arm64. Referencias oficiales:

- Docker Engine en Raspberry Pi OS (32-bit): https://docs.docker.com/engine/install/raspberry-pi-os/  
- Docker Compose plugin: https://docs.docker.com/compose/install/linux/

Pasos (recomendados; válidos para Debian/Raspberry Pi OS modernos):

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
```

Añade repo (Debian/Bookworm; Raspberry Pi OS moderno se comporta igual a nivel apt):

```bash
echo   "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian   $(. /etc/os-release && echo "$VERSION_CODENAME") stable" |   sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
```

Instala Docker + Compose:

```bash
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 7.2 Permitir Docker sin sudo (recomendado)

```bash
sudo usermod -aG docker $USER
newgrp docker
```

### 7.3 Verificación

```bash
docker --version
docker compose version
docker ps
```

---

## 8) Descargar el proyecto MeshNet

Instala git si no está:

```bash
sudo apt-get install -y git
```

Clona el proyecto (sustituye por tu URL real):

```bash
cd ~
git clone <URL_DE_TU_REPOSITORIO> meshnet
cd meshnet
```

---

## 9) Configurar variables (.env)

1. Copia la plantilla:

```bash
cp .env.example .env
```

2. Edita `.env`:

```bash
nano .env
```

Ajusta lo mínimo imprescindible (ejemplos típicos):
- Token y admin del bot (si aplica)
- IP/host del nodo Meshtastic (si usas TCP)
- Canales APRS/Mesh, puertos, etc.

---

## 10) Arrancar el sistema con Docker Compose

Desde la carpeta del proyecto:

```bash
docker compose -f docker-compose.rpi.yml up -d
```

Comprobar estado:

```bash
docker compose -f docker-compose.rpi.yml ps
docker compose -f docker-compose.rpi.yml logs -f --tail=200
```

---

## 11) Arranque automático 24/7 (systemd)

Docker se inicia solo, pero para asegurar que el stack se levanta tras reinicios, crea un servicio systemd.

1. Crea el servicio:

```bash
sudo nano /etc/systemd/system/meshnet.service
```

2. Pega esto (ajusta `User=` y `WorkingDirectory=` si cambian):

```ini
[Unit]
Description=MeshNet (Docker Compose)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=meshnet
WorkingDirectory=/home/meshnet/meshnet
ExecStart=/usr/bin/docker compose -f docker-compose.rpi.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.rpi.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

3. Activa y arranca:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshnet.service
sudo systemctl start meshnet.service
```

4. Verificación:

```bash
systemctl status meshnet.service --no-pager
```

---

## 12) Actualizar el proyecto

```bash
cd ~/meshnet
git pull
docker compose -f docker-compose.rpi.yml pull
docker compose -f docker-compose.rpi.yml up -d
```

---

## 13) Solución rápida de problemas

### 13.1 “No puedo conectar por SSH”
- Regraba la SD y asegúrate de **activar SSH** y crear usuario en Imager.
- Verifica IP en el router.

### 13.2 “Docker no funciona / compose no existe”
- Verifica:
  ```bash
  docker --version
  docker compose version
  ```
- Repite el apartado 7 (repositorio oficial).

### 13.3 “Contenedores no levantan”
- Mira logs:
  ```bash
  docker compose -f docker-compose.rpi.yml logs -f --tail=300
  ```

---

## Nota importante sobre compatibilidad Pi 2B
Docker mantiene soporte para Raspberry Pi OS 32-bit (armhf) pero con limitaciones de versiones futuras (avisos de soporte). Recomendación: Pi 2B = OS Lite 32-bit; Pi 3/4/5 = OS Lite 64-bit.  
Referencia: https://docs.docker.com/engine/install/raspberry-pi-os/
