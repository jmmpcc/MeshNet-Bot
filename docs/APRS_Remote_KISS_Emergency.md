# 📄 APRS_Remote_KISS_Emergency_Deployment

## Guía oficial de despliegue en emergencias MeshNet The Boss • Operación APRS + Mesh vía KISS Remoto

# 1. Objetivo de este documento

En situaciones de emergencia (apagones, fallos de Internet, movilidad, inundaciones, incendios, rescates, etc.), esta guía permite levantar en pocos minutos una infraestructura de comunicaciones resiliente basada en:

> Red Meshtastic (larga distancia, malla autónoma, sin Internet)
> Pasarela APRS ↔ MESH
> Direwolf / Soundmodem en un PC remoto
> Broker + Bot + APRS Gateway en Raspberry u otro PC central

El resultado es un sistema capaz de:

> Recibir mensajes APRS RF y distribuirlos dentro de Mesh
> Enviar mensajes Mesh hacia APRS
> Mantener conectividad aun sin Internet
> Permitir supervisión desde Telegram si existe conexión eventual

# 2. Arquitectura recomendada para emergencias
             [PC Remoto / Puesto de radio]
               └── Direwolf / Soundmodem (KISS TCP)

             [Unidad central / Centro de coordinación]
               ├── Broker MeshNet The Boss
               ├── Telegram Bot (opcional)
               └── APRS Gateway (meshtastic-aprs)


## Punto clave:
> Solo el KISS-TCP (soundmodem/direwolf) está en remoto.
> El APRS Gateway, broker y bot siempre permanecen juntos.

Esto garantiza:

> Menos puntos de fallo
> Un único sistema que almacena logs, posiciones y emergencias
> Reconexiones automáticas si Internet “va y viene”

Funcionamiento completo sin red externa

# 3. Requisitos mínimos

   ## En el equipo central (Raspberry o PC)

        > Docker + docker compose
        > Carpeta del proyecto MeshNet The Boss
        > .env correctamente configurado
        > Broker, bot y aprs dentro del mismo compose

   ## En el PC remoto

        > Direwolf o Soundmodem
        > Audio configurado (entrada micro, salida altavoz si procede)
        > Puerto TCP KISS abierto hacia la red local

        No se requiere instalar MeshNet ni contenedores adicionales.

# 4. Configuración para emergencias — Equipo Central (Raspberry/PC)

Abrir el .env y asegurar solo los ajustes siguientes:

## === Conexión KISS a PC remoto ===

> KISS_HOST= IP_DEL_PC_REMOTO
> 
> KISS_PORT= 8100

Ejemplo real:

> KISS_HOST=192.168.1.30
> 
> KISS_PORT=8100

NO tocar:

> BROKER_HOST
> 
> BROKER_CTRL_HOST
> 
> APRS_CTRL_HOST

Nada del compose

Todo lo demás debe permanecer igual para garantizar estabilidad.

# 5. Configuración del PC Remoto (Soundmodem / Direwolf)

   ## 5.1. Soundmodem

    En menú Settings → KISS Server:

    KISS over TCP → ✓ activado

    Address: 0.0.0.0

    Port: 8100

    Guardar y reiniciar soundmodem.

   ## 5.2 Direwolf

    Comando de arranque típico:

     direwolf -t 0 -p -r 48000 -D 1

    Y en direwolf.conf:

    KISSHOST 0.0.0.0
    KISSPORT 8100

# 6. Comprobación de conectividad (muy importante)

   ## En la Raspberry/PC central:

    telnet IP_DEL_PC_REMOTO 8100

    Si aparece:

        Connected

    el enlace está operativo.

    Si falla:

        Revisar firewall del PC
        Revisar soundmodem/direwolf en ejecución
        Revisar que se use la IP correcta
        Revisar que el puerto 8100 está libre

# 7. Arranque del sistema de emergencia

   ## En la máquina central:

    docker compose -f docker-compose.rpi.yml down
    docker compose -f docker-compose.rpi.yml up -d

   ## Ó, para arrancar solo APRS:

    docker restart meshtastic-aprs


   > El APRS Gateway se reconecta automáticamente al KISS remoto al arrancar.

# 8. Qué debe aparecer si todo está bien

   ## En los logs:

    docker logs -f meshtastic-aprs

### Debe verse:

   > [aprs] KISS=192.168.1.30:8100 CALL=EB2XXX-11 PATH=WIDE1-1,WIDE2-1
   > [aprs] Conectado a KISS TCP remoto

### Y también:

   > [broker→aprs] Conectado. Esperando líneas…

El broker seguirá mostrando actividad normal de la red Mesh.

# 9. Flujo operativo en emergencia

  ## 9.1 Yo mando un mensaje APRS desde un walkie

    → Direwolf lo recibe
    → KISS TCP lo pasa al APRS Gateway
    → El APRS Gateway lo analiza
    → El broker inyecta el mensaje en la malla Mesh
    → El bot (si activo) lo reenvía a Telegram

  ## 9.2 Un nodo Mesh envía emergencias

    → El APRS Gateway decide si debe publicarlo en APRS
    → Lo entrega a direwolf vía KISS TCP
    → Sale por RF APRS hacia estaciones externas

  ## 9.3 Internet cae

    → El bot se detiene parcialmente (no crítico)
    → Broker + APRS siguen operativos
    → Soundmodem remoto sigue enlazado por LAN
    → Toda la red Mesh + APRS funciona offline

# 10. Ventajas operativas en entornos críticos

    No requiere Internet
    No requiere APRS remoto
    Un solo punto de control (broker)
    Permite operación desde múltiples PCS con soundmodem
    Ideal en refugios, vehículos, Puestos Avanzados, Protección Civil
    No hay puertos Docker expuestos hacia exterior
    Facilita funcionamiento 24/7 con panel solar / batería

# 11. Resumen táctico (para imprimir y pegar en la maleta)

   ## En la Raspberry / PC central:
  
    Editar .env:
    KISS_HOST=IP_DEL_PC_REMOTO
    KISS_PORT=8100

    docker compose up -d

  ## En el PC remoto:
    
    Soundmodem:
    
    Host: 0.0.0.0
    Port: 8100

    Direwolf:
    
    KISSHOST 0.0.0.0
    KISSPORT 8100

  ## Prueba:
    
    telnet IP_DEL_PC_REMOTO 8100

  ## Logs:
    
    docker logs -f meshtastic-aprs

# 12. Fin del documento — Versión Emergencias v1.0

Preparado para integrarse en el repositorio oficial MeshNet The Boss.