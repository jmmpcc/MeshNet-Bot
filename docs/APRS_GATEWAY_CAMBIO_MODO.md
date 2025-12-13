# Instrucciones de Cambio de Modo: Raspberry ↔ PC

## Introducción

Este documento describe cómo alternar entre dos configuraciones: -
**Modo A:** Raspberry como servidor principal (broker + bot) y PC con
APRS. - **Modo B:** PC ejecutando broker + bot + APRS todo junto.

------------------------------------------------------------------------

# Modo A: Raspberry = broker+bot, PC = APRS

## 1. Raspberry: configuración

En `.env` de la Raspberry:

    APRS_CTRL_HOST=192.168.1.30
    APRS_CTRL_PORT=9464

    # Raspberry,, siempre never
    BOT_PAUSE_MODE=never

Bot y broker envían los comandos APRS a `192.168.1.30:9464`.

En `docker-compose.rpi.yml`, asegurarse:

    - APRS_CTRL_HOST=${APRS_CTRL_HOST}
    - APRS_CTRL_PORT=${APRS_CTRL_PORT}

Levantar:

    docker compose -f docker-compose.rpi.yml up -d bot broker

## 2. PC: servicio APRS

En `docker-compose.yml` del PC:

    environment:
      - BROKER_HOST=192.168.1.187
      - BROKER_PORT=8765
      - BROKER_CTRL_HOST=192.168.1.187
      - BROKER_CTRL_PORT=8766

      - APRS_CTRL_HOST=0.0.0.0
      - APRS_CTRL_PORT=9464
    ports:
      - "9464:9464/udp"

Levantar:

    docker compose up -d aprs

------------------------------------------------------------------------

# Modo B: PC = broker + bot + APRS (todo en uno)

## 1. Parar todo en la Raspberry

    docker compose -f docker-compose.rpi.yml down

## 2. Ajustar `.env` en el PC

    APRS_CTRL_HOST=127.0.0.1
    APRS_CTRL_PORT=9464

    BROKER_HOST=127.0.0.1
    BROKER_PORT=8765
    BROKER_CTRL_HOST=127.0.0.1
    BROKER_CTRL_PORT=8766

## 3. PC: activar bloque local en APRS

En `docker-compose.yml` del PC:

    environment:
      - BROKER_HOST=127.0.0.1
      - BROKER_PORT=8765
      - BROKER_CTRL_HOST=127.0.0.1
      - BROKER_CTRL_PORT=8766

      - APRS_CTRL_HOST=${APRS_CTRL_HOST}
      - APRS_CTRL_PORT=${APRS_CTRL_PORT}

Opcional:

    depends_on:
      - broker

    network_mode: "service:broker"

(Desactivar `ports: "9464:9464/udp"` si bot y APRS viven en el mismo
compose.)

## 4. Arrancar todo en el PC

    docker compose down
    docker compose up -d

------------------------------------------------------------------------

# Volver al modo A (Raspberry servidor)

1.  Parar en el PC:
    ```
    docker compose down
    ```
2.  En `.env` de Raspberry:
    ```
    APRS_CTRL_HOST=192.168.1.30
    APRS_CTRL_PORT=9464
    ```
3.  En PC: 
    ```
    cambiar a bloque remoto y reactivar `ports: "9464:9464/udp"`.
    ```
4.  Iniciar Raspberry:
    ```
    docker compose -f docker-compose.rpi.yml up -d broker bot
    ```
5.  Iniciar APRS en PC:
    ```
    docker compose up -d aprs
    ```
------------------------------------------------------------------------

# Resumen

El cambio de modo se basa en modificar dos elementos: - IP del broker:
`BROKER_HOST` / `BROKER_CTRL_HOST` - Destino de control APRS:
`APRS_CTRL_HOST` / `APRS_CTRL_PORT`

Con estos dos interruptores se controla si el sistema completo vive en
la Raspberry o en el PC.
