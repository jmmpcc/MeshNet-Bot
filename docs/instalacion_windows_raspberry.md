# Instalación y despliegue
Este proyecto incluye imágenes multi-arquitectura publicadas automáticamente en **GitHub Container Registry (GHCR)**.  
Gracias a esto, el sistema funciona tanto en **Windows** como en **Raspberry Pi** (incluida **Raspberry Pi 2B – ARMv7**) sin necesidad de compilar código pesado como *SciPy*.

# Instalación en Windows (Docker Desktop)
## 1. Clonar el repositorio
```powershell
git clone https://github.com/jmmpcc/MeshNet-Bot.git
cd MeshNet-Bot
```

## 2. Arrancar el sistema (modo desarrollo o ejecución local)
```powershell
docker compose up -d
```

## 3. (Opcional) Usar imágenes precompiladas desde GHCR
```powershell
docker compose -f docker-compose.yml -f docker-compose.rpi.yml up -d
```

# Instalación en Raspberry Pi
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

# Ficheros utilizados
- **docker-compose.yml**: para Windows.
- **docker-compose.rpi.yml**: override para Raspberry Pi.

# Flujo de despliegue automático
- Construcción multi-arch (amd64, arm/v7, arm64)
- Publicación en GHCR
- Docker selecciona la variante correcta

# Ventajas
- No compila SciPy en Raspberry.
- Un solo repositorio y un solo flujo de despliegue.
- Compatible con Windows, Raspberry Pi 2B, 3, 4, 5.
