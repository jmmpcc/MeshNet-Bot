# COMMANDS.md — Guía completa de comandos del bot

> Todos los comandos funcionan en chats privados con el bot y en grupos donde esté presente.
> Cuando aplica, el bot **pausa** el broker, ejecuta CLI y **reanuda** para evitar doble sesión al 4403.
> Los ejemplos muestran el texto a enviar en Telegram.

## Índice rápido
/start • /menu • /ayuda • /estado • /ver_nodos • /vecinos • /traceroute • /telemetria •
/enviar • /enviar_ack • /programar • /en • /manana • /tareas • /cancelar_tarea •
/escuchar • /parar_escucha • /canales • /position • /position_mapa • /cobertura •
/reconectar • /estadistica • /lora • (APRS) /aprs /aprs_on /aprs_off /aprs_status

---

## 🧭 /menu y /start
Muestra el menú contextual oficial (SetMyCommands) según tu rol (admin/usuario) y un resumen del sistema.
- **Ejemplo:** `/start`

## 🆘 /ayuda
Ayuda corta con comandos frecuentes y enlaces útiles.

## 🛰️ /estado
Resumen del sistema: latencia del nodo, estado del broker/bot/APRS.
- **Ejemplo:** `/estado`

## 📡 /ver_nodos [max_n] [timeout]
Lee **últimos nodos** del pool (recientes). Orden por recencia.
- **Ejemplos:** `/ver_nodos` | `/ver_nodos 30 4`

## 🤝 /vecinos [max_n] [hops_max]
Lista vecinos con **hops**, SNR y recencia (sin abrir TCP nuevo).
- **Ejemplos:** `/vecinos` | `/vecinos 20 2`

## 🛰️🍞 /traceroute <!id|alias> [timeout]
Traza saltos hacia un nodo. Pausa broker → CLI → reanuda.
- **Ejemplos:** `/traceroute !06c756f0` | `/traceroute Zgz_Romareda 35`

## 📶 /telemetria [!id|alias] [mins|max_n] [timeout]
- Sin destino: métricas **en vivo** (top recientes).
- Con destino: **en vivo + histórico** (ventana `mins`, p. ej. 20–30).
- **Ejemplos:** `/telemetria` | `/telemetria !06c756f0 20 4`

## ✉️ /enviar canal <n> <texto>  •  /enviar <número|!id|alias> <texto>
Envía por **canal** (broadcast) o **unicast** por `!id/alias`.
- **Ejemplos:** `/enviar canal 0 Hola red` | `/enviar !ea0a8638 Prueba`

## ✅ /enviar_ack <número|!id|alias> <texto>
Unicast solicitando **ACK** de aplicación.

## ⏱️ /programar  •  /en <min> canal <n> <texto>  •  /manana <hora> canal <n> <texto>
Planifica envíos y gestiona tareas.
- **Ejemplos:** `/en 5 canal 0 Recordatorio` | `/manana 09:30 canal 0 Buenos días` | `/tareas` | `/cancelar_tarea <uuid>`

## 👂 /escuchar  •  /parar_escucha
Activa escucha controlada y reporta nodos entrantes.

## 🌐 /canales
Muestra/gestiona canal lógico por defecto y uso de etiquetas **[CHx]**.

## 📍 /position  •  /position_mapa
Muestra última posición conocida y genera mapa (HTML/KML).

## 🗺️ /cobertura [opciones]
Genera mapas de cobertura y guarda en `./bot_data/maps/`.

## 🔌 /reconectar
Fuerza reconexión del broker con el nodo.

## 📊 /estadistica (solo admin)
Estadísticas de uso por usuarios/fecha.

## 🪪 /lora
Resumen de parámetros de enlace LoRa.

---

## 📡 APRS
### /aprs  •  /aprs_on  •  /aprs_off  •  /aprs_status
**Puente APRS ⇄ Mesh** con **etiqueta obligatoria** para inyección a malla.
- **Formatos:**

  - `/aprs canal N <texto>` → broadcast a canal N por KISS.

  - `/aprs N <texto>` → atajo.

  - `/aprs <CALL|broadcast>: <texto> [canal N]` → dirigido o broadcast.

- **Troceo:** si payload > ~67 caracteres, se divide en varias tramas.

- **Reinyección a malla:** solo si el comentario contiene `[CHx]` o `[CANAL x]`.

- **APRS‑IS:** si defines `APRSIS_USER`+`APRSIS_PASSCODE`, se suben **posiciones** etiquetadas.

- **Ejemplos:**

  - `/aprs canal 0 [CH0] Saludo` | `/aprs EB2EAS-11: Mensaje`

  - `/aprs_status` | `/aprs_on` | `/aprs_off`