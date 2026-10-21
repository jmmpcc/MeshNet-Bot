# Informe técnico — MeshNet Control Center / WebPanel Next

**Proyecto:** MeshNet-Bot  
**Componente:** renovación paralela del WebPanel  
**Rama de desarrollo:** `feature/webpanel-next-control-center`  
**Inicio del documento:** 2026-10-21  
**Estado:** desarrollo paralelo; no sustituye el WebPanel productivo  
**WebPanel productivo declarado operativo:** v7.0.39  
**Base Git de la rama:** `main@1c0d7186ac4c1f139cf38a93b0dd5cfb36c1b388`

---

## 1. Objetivo del informe

Este documento es el registro técnico vivo de la renovación del WebPanel de MeshNet-Bot hacia **MeshNet Control Center**.

Su actualización forma parte obligatoria de cada commit funcional de esta rama. El objetivo es mantener trazabilidad suficiente para:

- saber qué se ha modificado y por qué;
- identificar funciones existentes reutilizadas o preservadas;
- documentar decisiones de arquitectura;
- registrar pruebas automáticas y pruebas reales;
- controlar riesgos y regresiones;
- conocer el estado de paridad con el WebPanel productivo;
- documentar nuevas funciones MeshCore, Meshtastic, MQTT, traceroute y administración;
- facilitar revisión, rollback, auditoría y continuidad del desarrollo.

Este informe **no sustituye** a los manuales operativos ni al historial de versiones general. Describe exclusivamente el desarrollo y validación del nuevo WebPanel.

---

## 2. Regla de mantenimiento por commit

Todo commit perteneciente a la renovación del WebPanel deberá actualizar este archivo en el mismo commit.

Cada entrada incluirá, como mínimo:

1. fecha;
2. identificador/fase;
3. mensaje o propósito del commit;
4. archivos añadidos, modificados o eliminados;
5. funciones, endpoints o componentes afectados;
6. comportamiento existente que se conserva;
7. nuevas capacidades;
8. decisiones técnicas;
9. pruebas ejecutadas y resultado;
10. pruebas reales pendientes, cuando proceda;
11. riesgos conocidos;
12. impacto sobre producción;
13. estado de paridad funcional;
14. siguiente punto de trabajo.

El SHA concreto queda registrado por Git; cuando sea útil para referencias cruzadas se añadirá también al texto en una actualización posterior.

### Regla de aceptación

No se considerará completo un commit funcional de esta rama si:

- modifica código sin actualizar este informe;
- introduce una función ya existente bajo otro nombre sin justificar la duplicidad;
- cambia comportamiento productivo no relacionado con el objetivo del commit;
- no incluye o no documenta las pruebas razonablemente aplicables;
- mezcla cambios independientes que dificulten rollback o revisión.

---

## 3. Principios de desarrollo

### 3.1. No afectar producción

La renovación se desarrolla de forma paralela.

- El WebPanel productivo v7.0.39 continúa operativo.
- La nueva interfaz no lo sustituirá durante el desarrollo.
- La sustitución sólo se realizará tras alcanzar paridad funcional, superar pruebas automáticas y validar pruebas reales.
- Debe existir un procedimiento de rollback probado antes del cambio definitivo.

### 3.2. Un único propietario de la conexión MeshCore

La nueva WebUI **no abrirá una segunda conexión independiente** al Companion MeshCore.

Arquitectura objetivo:

```text
MeshCore Companion
        │
        ▼
MeshCoreEmbeddedBridge / Broker
        │
        ├──────────────► WebPanel actual
        │
        └──────────────► nueva API / MeshNet Control Center
```

Esto evita conflictos sobre serial, BLE o TCP y preserva el comportamiento actual del broker.

### 3.3. Separación backend/frontend

Objetivo arquitectónico:

```text
Radio / servicios / datos
           │
           ▼
      MeshNet-Bot
      Broker / FastAPI
           │
      REST / WebSocket
           │
           ▼
React + TypeScript + Vite
MeshNet Control Center
```

El frontend no debe contener lógica de radio que corresponda al broker.

### 3.4. Cambios incrementales y reversibles

La renovación se realizará en una rama independiente y mediante commits funcionales de alcance controlado.

No se utilizará un único commit masivo para agrupar toda la migración.

### 3.5. Preservación funcional

Antes de crear helpers, funciones o endpoints nuevos se comprobará si ya existe una función equivalente o reutilizable.

Cuando se modifique código existente:

- se preservará el comportamiento que ya funciona;
- se documentará expresamente la función afectada;
- se añadirá cobertura de regresión cuando sea viable;
- no se realizarán refactorizaciones laterales sin necesidad funcional.

---

## 4. Arquitectura funcional prevista

### 4.1. Áreas principales de la nueva interfaz

```text
MeshNet Control Center
│
├── Resumen
├── Radio
│   ├── MeshCore
│   ├── Meshtastic
│   └── APRS
├── Red
│   ├── Topología
│   ├── Nodos
│   ├── Repetidores
│   ├── Traceroute
│   └── Paquetes
├── Mensajería
│   ├── Canales
│   ├── Mensajes directos
│   ├── BBS
│   └── Email ↔ Mesh
├── Servicios
│   ├── Emergencias
│   ├── Farmacias
│   ├── APRS Gateway
│   ├── MQTT
│   └── Integraciones
├── Telemetría
├── Sistema
└── Auditoría
```

### 4.2. Tipos de operación

Se separarán visual y técnicamente tres clases de función:

**Lectura segura**
- estado;
- contactos;
- canales;
- telemetría;
- logs;
- rutas conocidas;
- servicios;
- Docker.

**Operación controlada**
- traceroute;
- descubrimiento de ruta;
- mensajería;
- solicitud de telemetría;
- MQTT.

**Administración**
- configuración remota;
- parámetros de radio;
- ajustes de repetidor;
- operaciones con confirmación y auditoría.

---

## 5. Hoja de ruta técnica

### WEB-0 — Auditoría funcional

Objetivo:
- inventariar todas las funciones del WebPanel actual;
- identificar dependencias directas con el broker;
- localizar generación HTML, JavaScript embebido y endpoints existentes;
- establecer matriz inicial de paridad.

Salida:
- inventario funcional;
- mapa de dependencias;
- lista de funciones reutilizables;
- riesgos de migración.

### WEB-1 — Contrato API

Objetivo:
- separar acceso a datos y operaciones de la presentación;
- definir endpoints REST;
- definir eventos WebSocket/SSE cuando correspondan;
- mantener compatibilidad con funciones existentes.

### WEB-2 — Shell React + TypeScript + Vite

Objetivo:
- crear frontend independiente;
- navegación;
- layout;
- sistema visual;
- temas claro/oscuro;
- responsive;
- gestión de errores y estados de carga.

### WEB-3 — Paridad funcional base

Migración progresiva de funciones productivas sin cambiar su comportamiento.

### MC-1 — Traceroute MeshCore

Objetivo:
- reproducir el flujo correcto utilizado por MeshCore;
- distinguir ruta conocida, advert path, path discovery y trace;
- correlacionar TRACE_DATA;
- conservar compatibilidad con la implementación actual cuando sea válida.

### MC-2 — Diagnóstico visual de rutas

Objetivo:
- etapas de descubrimiento;
- representación de saltos;
- SNR;
- errores comprensibles;
- mapa y grafo lógico.

### MC-3 — MQTT MeshCore

Objetivo:
- gateway MQTT del lado host;
- una sola conexión al Companion;
- topics documentados;
- TLS/ACL;
- publicación/recepción controlada;
- integración con el broker.

### MC-4 — Administración remota

Objetivo:
- lectura de configuración;
- telemetría;
- vecinos;
- estado de repetidores;
- modificaciones protegidas por autorización, confirmación y auditoría.

### MC-5 — Network Explorer

Objetivo:
- topología;
- relaciones entre nodos;
- calidad de enlaces;
- historial;
- mapa geográfico y grafo lógico.

### WEB-4 — Telemetría y Packet Analyzer

Objetivo:
- eventos decodificados;
- datos raw sólo para diagnóstico;
- filtros;
- métricas;
- histórico.

### WEB-5 — Paridad final, UX y regresión

Objetivo:
- matriz funcional 100 %;
- responsive;
- accesibilidad;
- rendimiento;
- validación real;
- procedimiento de sustitución;
- rollback.

---

## 6. Agrupación prevista de entregas

```text
Entrega A
WEB-0 + WEB-1 + WEB-2

Entrega B
WEB-3 + MC-1 + MC-2

Entrega C
MC-3 + MC-4

Entrega D
MC-5 + WEB-4 + WEB-5
```

La agrupación no elimina la trazabilidad individual de cada fase.

---

## 7. Estrategia de ejecución paralela

Configuración conceptual durante desarrollo:

```text
WebPanel actual            MeshNet Control Center
producción                 desarrollo
:8081                      :8082
      \                    /
       \                  /
        └────── Broker ───┘
```

La asignación definitiva de puertos se documentará cuando se implemente el servicio de desarrollo.

### Reglas

- ningún puerto productivo se sustituirá durante desarrollo;
- ningún servicio de desarrollo debe impedir el arranque del productivo;
- los fallos de la nueva UI no deben afectar al broker;
- las funciones de escritura se habilitarán progresivamente;
- las pruebas destructivas o de configuración se realizarán sólo cuando exista control explícito.

---

## 8. Matriz inicial de paridad

Esta tabla se ampliará durante WEB-0.

| Función | Web actual | Nuevo | Automática | Real | Estado |
|---|:---:|:---:|:---:|:---:|---|
| Estado broker | Sí | No | — | — | Pendiente |
| MeshCore contactos | Sí | No | — | — | Pendiente |
| MeshCore canales | Sí | No | — | — | Pendiente |
| Traceroute | Sí | No | — | — | Pendiente |
| Descubrir + trazar | Sí | No | — | — | Pendiente |
| Meshtastic | Sí | No | — | — | Pendiente |
| BBS | Sí | No | — | — | Pendiente |
| Emergencias | Sí | No | — | — | Pendiente |
| Farmacias | Sí | No | — | — | Pendiente |
| Logs | Sí | No | — | — | Pendiente |
| Exportaciones | Por auditar | No | — | — | Pendiente |
| MQTT MeshCore | No / por desarrollar | No | — | — | Pendiente |
| Topología MeshCore | Parcial | No | — | — | Pendiente |
| Packet Analyzer | No | No | — | — | Pendiente |
| Administración remota | No | No | — | — | Pendiente |

---

## 9. Batería de pruebas reales prevista

Identificadores iniciales:

- `TEST-WEB-MC-001`: consultar contacto real;
- `TEST-WEB-MC-002`: trazar contacto con ruta existente;
- `TEST-WEB-MC-003`: descubrir ruta sin `out_path`;
- `TEST-WEB-MC-004`: descubrir + trazar;
- `TEST-WEB-MC-005`: correlacionar `TRACE_DATA` por tag;
- `TEST-WEB-MC-006`: timeout sin `PATH_RESPONSE`;
- `TEST-WEB-MC-007`: pérdida y recuperación del Companion;
- `TEST-WEB-MC-008`: MQTT offline/online;
- `TEST-WEB-MC-009`: reinicio del broker;
- `TEST-WEB-MC-010`: WebPanel productivo y nuevo WebPanel funcionando simultáneamente.

La lista crecerá con cada fase.

---

## 10. Criterios para sustituir el WebPanel productivo

No se realizará la sustitución hasta cumplir todos los puntos siguientes:

- paridad funcional completa de las funciones que deban conservarse;
- pruebas unitarias aplicables correctas;
- pruebas de integración correctas;
- pruebas frontend correctas;
- pruebas reales sobre radio correctas;
- ejecución paralela sin interferencias;
- autenticación/autorización de operaciones sensibles;
- procedimiento de backup/rollback documentado y probado;
- revisión final del informe técnico;
- validación explícita antes del cambio de puerto/servicio productivo.

Secuencia prevista:

```text
Fase 1
8081 antiguo / 8082 nuevo

Fase 2
8081 nuevo / 8082 antiguo como fallback

Fase 3
nuevo estable

Fase 4
retirada controlada del legado
```

---

## 11. Registro de commits

### 2026-10-21 — DOC/WEB-INIT — Inicialización del desarrollo paralelo

**Propósito**

Crear la rama independiente de renovación y establecer el informe técnico vivo que acompañará todos los commits de MeshNet Control Center.

**Base revisada**

- repositorio: `jmmpcc/MeshNet-Bot`;
- rama base: `main`;
- commit base: `1c0d7186ac4c1f139cf38a93b0dd5cfb36c1b388`;
- índice documental revisado: `docs/README.md`;
- no existía un informe técnico específico equivalente para WebPanel Next.

**Cambios**

- creada rama `feature/webpanel-next-control-center`;
- creado `docs/WEBPANEL_NEXT_TECHNICAL_REPORT.md`;
- añadido el documento al índice de documentación;
- documentadas arquitectura, reglas de desarrollo, fases, estrategia de ejecución paralela, matriz inicial de paridad y pruebas reales previstas.

**Código funcional afectado**

Ninguno.

**Comportamiento productivo**

Sin cambios.

**Pruebas**

- revisión de estructura documental del repositorio;
- comprobación de inexistencia de un informe técnico equivalente;
- comprobación de base Git de la rama.

**Riesgo**

Bajo. Commit documental y de preparación.

**Paridad**

No iniciada. La nueva WebUI todavía no existe.

**Siguiente punto**

WEB-0: inventario exhaustivo del WebPanel actual y construcción de la matriz funcional real antes de crear componentes duplicados.

---

## 12. Estado actual

| Área | Estado |
|---|---|
| Rama paralela | Creada |
| Informe técnico | Creado |
| WebPanel productivo | Sin cambios |
| WEB-0 | Pendiente |
| WEB-1 | Pendiente |
| WEB-2 | Pendiente |
| MC-1 | Pendiente |
| MC-2 | Pendiente |
| MC-3 | Pendiente |
| MC-4 | Pendiente |
| MC-5 | Pendiente |
| Sustitución productiva | Bloqueada hasta validación final |
