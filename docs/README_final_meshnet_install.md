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

---

# ✔ Formas de ejecutar el proyecto en Windows

Existen **dos modos diferentes** de arrancar el sistema. Ambos funcionan correctamente, pero sirven para distintos casos.

---

# 🅰 Opción A — Construir localmente (modo recomendado para desarrollo)

Esta opción usa tu ordenador para construir las imágenes Docker con los Dockerfile del proyecto.

```powershell
docker compose up -d
```

### Ventajas:
- Perfecto si vas a modificar código Python o Dockerfiles.  
- Permite reconstruir rápidamente mientras desarrollas.  
- No dependes de internet para reconstrucciones posteriores.

### Inconvenientes:
- Construye las imágenes en tu PC.  
- No garantiza usar exactamente la misma imagen que en Raspberry.

---

# 🅱 Opción B — Usar imágenes oficiales precompiladas desde GHCR (modo “sin compilación”)

Aquí Windows **no construye nada**.  
Descarga directamente las imágenes multi-arch ya generadas por GitHub Actions:

```powershell
docker compose -f docker-compose.yml -f docker-compose.rpi.yml up -d
```

### Ventajas:
- Mucho más rápido.  
- Usa exactamente las mismas imágenes que Raspberry Pi.  
- No compila nada en tu ordenador.

### Inconvenientes:
- No recomendado si vas a modificar el código.  
- Depende de que el repositorio GHCR esté actualizado.

---

# ¿Qué opción elegir?

| Situación | Opción recomendada |
|----------|--------------------|
| Quieres modificar código o desarrollar | **Opción A (build local)** |
| Quieres instalar y usar sin complicaciones | **Opción B (GHCR)** |
| Notas que tu PC va justo de recursos | **Opción B (GHCR)** |
| Quieres que Windows use la misma imagen que Raspberry | **Opción B (GHCR)** |

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
