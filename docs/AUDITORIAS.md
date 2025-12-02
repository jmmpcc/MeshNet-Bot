# 🛰️ Auditorías MeshNet — v6.1.3

Este documento describe las dos auditorías integradas en MeshNet:

- **Auditoría de Red (`auditoria_red`)**
- **Auditoría Integral (`auditoria_integral`)**

Ambas funciones analizan la información del backlog, nodos escuchados, métricas SNR/RSSI, distancias y rutas para generar un informe claro del estado real de la malla.

---

# 📡 1. Auditoría de Red — `auditoria_red`

Evalúa en tiempo real:

- **Calidad de enlace por nodo**
  - SNR mínimo / máximo / promedio
  - Clasificación por colores
  - Porcentaje de paquetes recibidos
- **Distancia a HOME**
- **Vecinos conocidos**
- **Última vez escuchado (minutos)**
- **Rutas y hops**
- **Geolocalización offline**
- Comparativa entre nodos
- Ranking por calidad

### ✔️ Datos analizados:

- `POSITION_APP`
- `TELEMETRY_APP`
- `ROUTING_APP`
- Tabla de nodos del broker
- Backlog local JSONL
- Coordenadas de HOME

### ✔️ Salida generada:

- Resumen de nodos fuertes, medios y débiles
- Nodos sin posición o sin métricas
- Distancias calculadas por Haversine
- Provincia/ciudad por reverse-geocoder
- Tabla comparativa por SNR

---

# 🌐 2. Auditoría Integral — `auditoria_integral`

Versión extendida que analiza:

- **Cobertura total de la red**
- **Mapa KML integrado**
- **Heatmap de posiciones**
- **Detección de agujeros de cobertura**
- Análisis temporal:
  - Últimas 24h
  - Últimas 72h
  - Últimos 7 días
- Estadísticas por nodo:
  - Mensajes enviados/recibidos
  - Saltos promedio
  - SNR promedio
  - Máximas distancias alcanzadas
- Detección de rutas poco eficientes
- Detección de nodos “centrales” (mayor conectividad)

### ✔️ Salida generada:

- Informe HTML opcional
- KML actualizado
- Resumen por nodo
- Histograma básico de calidad
- Ranking de cobertura

---

# 📊 Clasificación de Enlaces (usada en ambas auditorías)

| Calidad | SNR (dB) | Indicador |
|--------|----------|-----------|
| Muy fuerte | ≥ +5 | 🟢 |
| Fuerte | 0 a +5 | 🟢 |
| Óptimo | 0 a –10 | 🟡 |
| Utilizable | –10 a –15 | 🟠 |
| Crítico | –15 a –20 | 🔴 |
| Casi perdido | < –20 | ⚫ |

---

# 📁 Archivos generados por las auditorías

- `coverage.kml` — cobertura en Google Earth
- `coverage_24h.kml` — últimas 24h
- `coverage_backlog.jsonl` — datos crudos
- `auditoria_red.txt`
- `auditoria_integral.txt`
- `heatmap_positions.json`

---

# 🛠️ Uso desde Telegram

### Auditoría de red:
```
/auditoria_red
```

### Auditoría integral:
```
/auditoria_integral
```

---

# 📌 Notas finales

Las auditorías combinan datos de:

- API Meshtastic
- CLI Meshtastic
- Backlog del broker
- Tabla nodos.txt
- Reverse geocoder offline
- HOME_LAT / HOME_LON

Todo esto permite un análisis completo sin internet.
