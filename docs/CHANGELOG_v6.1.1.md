# 🧭 CHANGELOG — Versión 6.1.1  
📅 *Octubre 2025*

## 📋 Índice de cambios

1. [Resumen general](#resumen-general)  
2. [Componentes principales](#componentes-principales)  
3. [Cambios detallados por módulo](#cambios-detallados-por-módulo)  
   - [Meshtastic_Broker.py](#meshtastic_brokerpy)  
   - [Telegram_Bot_Broker.py](#telegram_bot_brokerpy)  
   - [meshtastic_to_aprs.py](#meshtastic_to_aprspy)  
   - [mesh_preset_bridge.py](#mesh_preset_bridgepy)  
   - [meshtastic_api_adapter.py](#meshtastic_api_adapterpy)  
   - [positions_store.py](#positions_storepy)  
   - [broker_resilience.py](#broker_resiliencepy-nuevo)  
   - [coverage_backlog.py](#coverage_backlogpy)  
4. [Compatibilidad y migración](#compatibilidad-y-migración)  
5. [Recomendación de actualización](#recomendación-de-actualización)  
6. [Checklist post-upgrade](#checklist-post-upgrade)

---

## 🧠 Resumen general

Versión enfocada en **resiliencia**, **telemetría** y **operación estable** en entornos Docker, Raspberry Pi y Windows.  
Incluye integración ampliada con APRS, mecanismos de reconexión automáticos, y una arquitectura más robusta para el broker y el bot de Telegram.

🔹 Mejora drástica de estabilidad TCP y reducción de errores `BrokenPipe`.  
🔹 Notificaciones persistentes y configurables de tareas ejecutadas.  
🔹 Bridge interno más eficiente con control de tasa y antidupe.  
🔹 Exportación avanzada de cobertura y posiciones en KML/GPX.

---

## ⚙️ Componentes principales

| Módulo | Versión | Cambios clave |
|---------|----------|---------------|
| **Meshtastic_Broker.py** | v6.1.1 | Guards anti-error, cooldown coordinado, telemetría persistente |
| **Telegram_Bot_Broker.py** | v6.1.1 | Notificaciones persistentes, `/reconectar`, control de cooldown |
| **meshtastic_to_aprs.py** | v6.1.1 | Doble pasarela Mesh↔APRS, dedup TTL, soporte APRS-IS |
| **mesh_preset_bridge.py** | v6.1.1 | Bridge bidireccional A↔B con anti-loop, rate-limit y tagging |
| **meshtastic_api_adapter.py** | v6.1.1 | API híbrida con fallback CLI, métricas y envío resiliente |
| **positions_store.py** | v6.1.1 | Compactación y exportación KML/GPX, `TelemetryStore` integrada |
| **broker_resilience.py** | v6.1.1 | 🆕 CircuitBreaker, Watchdog, SendQueue |
| **coverage_backlog.py** | v6.1.1 | Generación avanzada de heatmaps y KML con alias y ponderación |
| **README.MD** | Actualizado | Nuevas instrucciones y ejemplos de comandos `/diario`, `/enviar`, `/reconectar` |

---

## 🔍 Cambios detallados por módulo

### 🧠 Meshtastic_Broker.py
- Guards anti-error (`sendHeartbeat`, `_sendToRadio()`), detección BrokenPipe y cooldown coordinado.
- TelemetryStore integrado para `TELEMETRY_APP`.
- Bridge embebido (`bridge_in_broker`) activado.
- Persistencia mejorada (logs JSONL rotativos).
- Integración con `broker_resilience` (Watchdog + CircuitBreaker).
- Logs unificados con `_print_with_ts()`.

### 🤖 Telegram_Bot_Broker.py
- Control de cooldown (`_abort_if_cooldown`), comando `/reconectar` con confirmación.
- Sistema de notificaciones persistentes (`_notify_executed_tasks_job`).
- Envío resiliente con `send_text_respecting_cooldown()` y `_broker_rpc()`.
- Conversión APRS y troceo (`_aprs_split_broadcast`, `_aprs_split_directed`).
- Alias cacheados (`_resolve_alias_and_cache`) y nombres legibles (`_friendly_node`).
- Seguridad mejorada (`_safe_reply_html`, `_validate_len_or_block`).

### 🛰️ meshtastic_to_aprs.py
- Pasarela Mesh↔APRS con soporte KISS TCP y APRS-IS.
- Ventana antidupe configurable (`DEDUP_TTL`).
- Sanitización ASCII-7 y filtros RFONLY/NOGATE.

### 🌉 mesh_preset_bridge.py
- Bridge A↔B configurable con `A2B_CH_MAP` / `B2A_CH_MAP`.
- Etiquetas dinámicas (`TAG_BRIDGE_A2B`, etc.).
- DedupWindow + RateLimiter.
- Conexión dual TCP (`A_HOST`, `B_HOST`).

### 🧩 meshtastic_api_adapter.py
- API/CLI híbrido con `TCPInterfacePool` persistente.
- Nuevas funciones (`api_get_neighbors`, `api_list_nodes`).
- Comunicación JSONL vía `send_via_broker_iface`.
- Normalización UTF-8, RSSI/SNR/hops.

### 📍 positions_store.py
- Compactación automática y exportación `KML`/`GPX`.
- `TelemetryStore` con métricas (temperatura, voltaje, altitud).
- Filtros rápidos (`read_positions_recent`).

### ⚡ broker_resilience.py (nuevo)
- CircuitBreaker con reintentos limitados y reapertura automática.
- Watchdog para reconexión ante inactividad.
- SendQueue con coalescing y control de congestión.

### 🗺️ coverage_backlog.py
- Mapas KML y HTML (folium.HeatMap).
- Ponderación RSSI/SNR y alias legibles.
- Compatible con backlog del broker (`broker_backlog.jsonl`).

---

## 🧩 Compatibilidad y migración

| Área | Estado | Comentarios |
|------|---------|-------------|
| Docker Compose | ✅ | Añadido puerto 8766 (BacklogServer). |
| `.env` | ✅ | Variables ampliadas, sin romper compatibilidad. |
| Datos persistentes | ✅ | Mantiene `scheduled_tasks.jsonl` y `positions.jsonl`. |
| CLI Meshtastic | ✅ | Fallback activo. |

---

## 🚀 Recomendación de actualización

```bash
git pull
docker compose pull
docker compose up -d
```

> No es necesario eliminar volúmenes ni reconstruir imágenes.  
> Los nuevos ficheros (`telemetry_log.jsonl`, `broker_traceroute_log.jsonl`) se crean automáticamente.

---

## ✅ Checklist post-upgrade

- [ ] Verificar conexión con `/estado`  
- [ ] Probar `/reconectar`  
- [ ] Revisar notificaciones `/tareas`  
- [ ] Confirmar APRS gateway  
- [ ] Exportar `/kml` o `/gpx`  

---

## 📦 Etiquetas Docker

| Imagen | Descripción | Tag |
|--------|--------------|-----|
| `ghcr.io/jmmpcc/the-boss-broker` | Broker Meshtastic + BacklogServer | `v6.1.1` |
| `ghcr.io/jmmpcc/the-boss-bot` | Bot de Telegram + API Meshtastic | `v6.1.1` |
| `ghcr.io/jmmpcc/the-boss-aprs` | Gateway APRS bidireccional | `v6.1.1` |
| `ghcr.io/jmmpcc/the-boss-bridge` | Pasarela A↔B presets | `v6.1.1` |
