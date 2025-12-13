# MeshNet “The Boss” — Instalación y Despliegue

Este proyecto incluye imágenes multi-arquitectura publicadas automáticamente en **GitHub Container Registry (GHCR)**.  
Gracias a esto, el sistema funciona tanto en **Windows** como en **Raspberry Pi** (incluida Raspberry Pi 2B – ARMv7) sin necesidad de compilar código pesado como *SciPy*.

---

# 🖥️ Instalación en Windows (Docker Desktop)

## 1. Clonar el repositorio
```powershell
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
```

## 2. Arrancar el sistema (modo desarrollo o ejecución local)
```powershell
docker compose up -d
```

## 3. Usar imágenes precompiladas desde GHCR (opcional)
```powershell
docker compose -f docker-compose.yml -f docker-compose.rpi.yml up -d
```

---

# 🍓 Instalación en Raspberry Pi

Compatible con Raspberry Pi **2B**, **3**, **4**, **5**.  
La arquitectura correcta se selecciona automáticamente (arm/v7 o arm64).

## 1. Instalar Docker + Docker Compose Plugin
```bash
curl -sSL https://get.docker.com | sh
sudo apt install -y docker-compose-plugin
```

## 2. Clonar el repositorio
```bash
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
```

## 3. Descargar imágenes multi-arch desde GHCR
```bash
docker compose -f docker-compose.yml -f docker-compose.rpi.yml pull
```

## 4. Arrancar el sistema
```bash
docker compose -f docker-compose.yml -f docker-compose.rpi.yml up -d
```

---

# 🧩 Ficheros del proyecto

- **docker-compose.yml** → Uso general en Windows.
- **docker-compose.rpi.yml** → Override para Raspberry Pi.
- **Dockerfile / Dockerfile.aprs / Dockerfile.bridge** → Construcción por servicio.
- **bot_data/** → Datos persistentes del bot.
- **.github/workflows/** → Compilación multi-arch automática.

---

# 🔄 Actualización del proyecto

## Windows
```powershell
git pull
docker compose up -d --build
```

## Raspberry Pi
```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.rpi.yml pull
docker compose -f docker-compose.yml -f docker-compose.rpi.yml up -d
```

---

# 🧪 Logs

## Broker
```bash
docker logs -f meshnet-broker
```

## Bot
```bash
docker logs -f meshnet-bot
```

## APRS
```bash
docker logs -f aprs-gateway
```

## Bridge
```bash
docker logs -f meshnet-bot-bridge
```

---

# 🐳 Cómo funcionan las imágenes multi-arch

GitHub Actions compila automáticamente para:

- `linux/amd64` (PC / Windows)
- `linux/arm/v7` (Raspberry Pi 2B / 3)
- `linux/arm64` (Raspberry Pi 4 / 5)

y publica en GHCR:

```
ghcr.io/<usuario>/meshnet-bot-broker:latest
ghcr.io/<usuario>/meshnet-bot-bot:latest
ghcr.io/<usuario>/meshnet-bot-aprs:latest
ghcr.io/<usuario>/meshnet-bot-bridge:latest
```

Docker descarga la variante correcta según tu hardware.

---

# 🛠 Detener el sistema
```bash
docker compose down
```

Con volúmenes:
```bash
docker compose down -v
```

---

# 📄 Licencia
MIT License  
Autor: **Modo Absoluto**
