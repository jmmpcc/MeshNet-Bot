#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mesh_triple_bridge.py  — Pasarela externa A↔B y A↔C usando TCP Meshtastic.

Objetivo:
- Mantener A como nodo principal (el que usa el broker/bot).
- Conectar además dos nodos remotos: B y C.
- Reenviar mensajes entre:
    A → B   (según mapa A2B)
    B → A   (según mapa B2A)
    A → C   (según mapa A2C)
    C → A   (según mapa C2A)

Efecto práctico:
- Todo lo que entre por A se replica hacia B y hacia C (según los mapas).
- Todo lo que venga de B entra a A (y por tanto lo ve el broker/bot, consola incluida).
- Todo lo que venga de C entra a A (igual).
- No se crea un enlace directo B↔C; el camino es B→A→C y C→A→B, evitando bucles
  complicados a tres bandas.

Características:
- Usa el pool TCP persistente si existe (tcpinterface_persistent.TCPInterface).
- Si no, cae al TCPInterface del SDK oficial.
- Anti-bucle por:
  - local_id de cada nodo (no reenvía de vuelta al mismo que lo generó).
  - ventana de deduplicación por (dirección, fromId, canal, payload).
- Rate limit independiente por sentido (msgs/min).
- Mapeo de canales configurable por variables de entorno.

Variables de entorno principales:
- A_HOST, A_PORT   → nodo A (central, el del broker).
- B_HOST, B_PORT   → nodo B (remoto 1).
- C_HOST, C_PORT   → nodo C (remoto 2).

Mapeos de canales:
- A2B_CH_MAP   (ej: "0:0,1:1")
- B2A_CH_MAP
- A2C_CH_MAP
- C2A_CH_MAP

Filtros y comportamiento:
- FORWARD_TEXT        (por defecto "1")
- FORWARD_POSITION    (por defecto "0")
- REQUIRE_ACK         (por defecto "0")
- RATE_LIMIT_PER_SIDE (por defecto "8" msgs/min por sentido)
- DEDUP_TTL           (por defecto "45" segundos)
- TAG_BRIDGE          (por defecto "[BRIDGE]")
- TAG_BRIDGE_A2B, TAG_BRIDGE_B2A, TAG_BRIDGE_A2C, TAG_BRIDGE_C2A (opcionales)

Uso básico:
    export A_HOST=192.168.1.100
    export B_HOST=192.168.1.101
    export C_HOST=192.168.1.102
    export A2B_CH_MAP="0:0"
    export B2A_CH_MAP="0:0"
    export A2C_CH_MAP="0:0"
    export C2A_CH_MAP="0:0"

    python mesh_triple_bridge.py
"""

from __future__ import annotations

import os
import time
import json
import threading
import re
import hashlib
import socket
from collections import deque
from typing import Optional, Dict

from pubsub import pub

# ============================================================
#  Compat TCPInterface: pool persistente si existe, si no SDK
# ============================================================

_TCPI = None
try:
    # Preferencia: tu pool persistente
    from tcpinterface_persistent import TCPInterface as _PoolTCPIF  # type: ignore
    _TCPI = _PoolTCPIF
except Exception:
    try:
        from meshtastic.tcp_interface import TCPInterface as _SDKTCPIF  # type: ignore
        _TCPI = _SDKTCPIF
    except Exception as e:
        raise SystemExit(f"[FATAL] No se pudo importar TCPInterface: {e}")


# ============================================================
#  Helpers básicos (mapeos, texto, hash)
# ============================================================

def _truthy(s: str | None, default: bool = False) -> bool:
    """
    Interpreta strings tipo "1,true,yes,on,si,sí" como True.
    Si s es None, devuelve 'default'.
    """
    if s is None:
        return default
    return s.strip().lower() in {"1", "true", "t", "yes", "y", "on", "si", "sí"}


def _parse_ch_map(s: str | None) -> dict[int, int]:
    """
    Convierte una cadena tipo "0:0,1:1,2:0" en un diccionario {0:0,1:1,2:0}.
    Cualquier elemento mal formateado se ignora de forma silenciosa.
    """
    out: dict[int, int] = {}
    if not s:
        return out
    for part in s.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        a, b = part.split(":", 1)
        try:
            out[int(a.strip())] = int(b.strip())
        except Exception:
            continue
    return out


def _norm_text(s: str) -> str:
    """
    Normaliza un texto:
    - Sustituye comillas tipográficas y guiones raros.
    - Colapsa espacios múltiples.
    - Elimina espacios al principio y final.
    """
    if not s:
        return ""
    rep = {
        "“": '"',
        "”": '"',
        "’": "'",
        "‘": "'",
        "—": "-",
        "–": "-",
        "…": ".",
        "\u00A0": " ",
    }
    s = s.translate(str.maketrans(rep))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _hash_key(direction: str, from_id: str, ch: int, payload: str) -> str:
    """
    Crea un hash estable (SHA-256) a partir de:
    - direction (ej: "A2B")
    - from_id (ej: "!9eeb1328")
    - canal lógico (int)
    - payload normalizado (texto o JSON ordenado)
    """
    h = hashlib.sha256()
    h.update(direction.encode("utf-8"))
    h.update(str(from_id or "?").encode("utf-8"))
    h.update(str(int(ch)).encode("utf-8"))
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


# ============================================================
#  Estado auxiliar: rate-limit y deduplicación
# ============================================================

class RateLimiter:
    """
    Limitador sencillo: máximo N mensajes por minuto.
    Guarda timestamps y purga los de más de 60 s.
    """

    def __init__(self, max_per_min: int = 8):
        self.max = max(1, int(max_per_min))
        self.ts = deque()  # epoch seconds

    def allow(self) -> bool:
        now = time.time()
        while self.ts and (now - self.ts[0]) > 60.0:
            self.ts.popleft()
        if len(self.ts) < self.max:
            self.ts.append(now)
            return True
        return False


class DedupWindow:
    """
    Ventana de deduplicación basada en hash:
    - TTL en segundos (por defecto 45).
    - Si una clave se ha visto dentro de la ventana, se considera duplicado.
    """

    def __init__(self, ttl_sec: int = 45):
        self.ttl = max(5, int(ttl_sec))
        self.store: Dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        # Purga entradas caducadas
        dead = [k for k, ts in self.store.items() if (now - ts) > self.ttl]
        for k in dead:
            self.store.pop(k, None)
        if key in self.store:
            return True
        self.store[key] = now
        return False


# ============================================================
#  Broker Hub (BacklogServer): recibir desde A sin abrir TCP a A
# ============================================================

class BrokerBacklogClient:
    """
    Cliente mínimo para el BacklogServer del broker (TCP, JSON por línea).

    Soporta:
      - FETCH_BACKLOG  (para leer eventos persistidos del nodo A)
      - SEND_TEXT      (para inyectar textos hacia A usando la cola TX del broker)

    Importante:
      - Este cliente evita abrir una segunda conexión TCP directa contra el nodo A,
        lo cual en muchos firmwares provoca 'connection reset by peer' cuando ya
        existe un cliente (p.ej. el broker).
    """

    def __init__(self, host: str, port: int, timeout_s: float = 8.0):
        self.host = str(host or "127.0.0.1").strip()
        self.port = int(port or 8765)
        self.timeout_s = float(timeout_s or 8.0)

    def _request(self, payload: dict) -> dict:
        """
        Envía un JSON (una línea) y lee una respuesta (una línea).
        """
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as s:
            s.settimeout(self.timeout_s)
            s.sendall(line)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        try:
            return json.loads(buf.decode("utf-8", "ignore").strip() or "{}")
        except Exception:
            return {"ok": False, "error": "invalid_response"}

    def fetch_backlog(self, since_ts: int | None, portnums: list[str] | None, limit: int = 2000) -> list[dict]:
        """
        Lee eventos del JSONL persistido por el broker.
        - since_ts: epoch seconds (int). Si None, el broker devuelve desde el principio (no recomendado).
        - portnums: lista de tipos, p.ej. ["TEXT_MESSAGE_APP"].
        """
        req = {
            "cmd": "FETCH_BACKLOG",
            "params": {
                "since_ts": int(since_ts) if since_ts is not None else None,
                "until_ts": None,
                "channel": None,
                "portnums": portnums or ["TEXT_MESSAGE_APP"],
                "limit": int(limit or 2000),
            },
        }
        resp = self._request(req)
        if not resp.get("ok"):
            return []
        data = resp.get("data") or []
        return data if isinstance(data, list) else []

    def send_text(self, text: str, ch: int = 0, dest: str | None = None, ack: bool = False) -> bool:
        """
        Inyecta un texto hacia la radio A usando el broker (SEND_TEXT).
        - dest: None o "broadcast" para ^all (broadcast). Si se pasa un id, será unicast.
        """
        params = {"text": str(text or ""), "ch": int(ch or 0)}
        if dest and str(dest).strip() and str(dest).strip().lower() != "broadcast":
            params["dest"] = str(dest).strip()
            params["ack"] = bool(ack)
        req = {"cmd": "SEND_TEXT", "params": params}
        resp = self._request(req)
        return bool(resp.get("ok"))

# ============================================================
#  TripleBridge: A↔B y A↔C
# ============================================================

class TripleBridge:
    """
    Pasarela 3-way:
      - Nodo A: central (el del broker/bot).
      - Nodo B: remoto 1.
      - Nodo C: remoto 2.

    Lógica de reenvío:

      Desde A:
        - A → B (si canal en A2B_CH_MAP)
        - A → C (si canal en A2C_CH_MAP)

      Desde B:
        - B → A (si canal en B2A_CH_MAP)

      Desde C:
        - C → A (si canal en C2A_CH_MAP)

    Con eso:
      - Mensajes originados en A llegan a B y C.
      - Mensajes originados en B llegan a A, y desde A pueden llegar a C
        (si el texto vuelve a salir por A con el bridge embebido o con
         otro flujo que tengas configurado).
      - Mensajes originados en C llegan a A, y desde A pueden llegar a B.

    Se evita crear un enlace directo B↔C en este proceso para minimizar
    bucles. El camino B→A→C y C→A→B ya reparte la información entre todos.
    """

    def __init__(
        self,
        a_host: str,
        a_port: int,
        hub_mode: str = \"tcp\",
        broker_ctrl_host: str | None = None,
        broker_ctrl_port: int | None = None,
        broker_poll_sec: float = 1.5,
        broker_fetch_limit: int = 2000,
        broker_timeout_s: float = 8.0,
        b_host: str,
        b_port: int,
        c_host: str,
        c_port: int,
        a2b_map: dict[int, int],
        b2a_map: dict[int, int],
        a2c_map: dict[int, int],
        c2a_map: dict[int, int],
        forward_text: bool = True,
        forward_position: bool = False,
        require_ack: bool = False,
        rate_limit_per_side: int = 8,
        dedup_ttl: int = 45,
        tag_bridge: str = "[BRIDGE]",
        tag_bridge_a2b: str | None = None,
        tag_bridge_b2a: str | None = None,
        tag_bridge_a2c: str | None = None,
        tag_bridge_c2a: str | None = None,
    ):
        # Parámetros de red
        self.a_host, self.a_port = a_host, int(a_port or 4403)

        # Modo HUB:
        # - "tcp"    -> la pasarela abre TCP directo a A (modo original)
        # - "broker" -> NO se abre TCP a A. Se usa BacklogServer del broker para:
        #              * leer backlog (lo recibido por A)
        #              * enviar textos hacia A (SEND_TEXT)
        self.hub_mode = (hub_mode or "tcp").strip().lower()
        self.broker_ctrl_host = (broker_ctrl_host or os.getenv("BROKER_CTRL_HOST") or "").strip() or None
        try:
            self.broker_ctrl_port = int(broker_ctrl_port or os.getenv("BROKER_CTRL_PORT") or 8765)
        except Exception:
            self.broker_ctrl_port = 8765
        try:
            self.broker_poll_sec = float(broker_poll_sec or os.getenv("BROKER_POLL_SEC") or 1.5)
        except Exception:
            self.broker_poll_sec = 1.5
        try:
            self.broker_fetch_limit = int(broker_fetch_limit or os.getenv("BROKER_FETCH_LIMIT") or 2000)
        except Exception:
            self.broker_fetch_limit = 2000
        try:
            self.broker_timeout_s = float(broker_timeout_s or os.getenv("BROKER_TIMEOUT_S") or 8.0)
        except Exception:
            self.broker_timeout_s = 8.0

        # Cliente del broker (solo en hub_mode="broker")
        self._broker: BrokerBacklogClient | None = None
        self._poll_thread: threading.Thread | None = None
        self._hub_last_ts: int | None = None

        self.b_host, self.b_port = b_host, int(b_port or 4403)
        self.c_host, self.c_port = c_host, int(c_port or 4403)

        # Mapas de canal
        self.a2b_map = dict(a2b_map or {})
        self.b2a_map = dict(b2a_map or {})
        self.a2c_map = dict(a2c_map or {})
        self.c2a_map = dict(c2a_map or {})

        # Qué reenviar
        self.forward_text = bool(forward_text)
        self.forward_position = bool(forward_position)
        self.require_ack = bool(require_ack)

        # Etiquetas
        base = str(tag_bridge or "").strip()
        self.tag_a2b = (tag_bridge_a2b.strip() if tag_bridge_a2b else base)
        self.tag_b2a = (tag_bridge_b2a.strip() if tag_bridge_b2a else base)
        self.tag_a2c = (tag_bridge_a2c.strip() if tag_bridge_a2c else base)
        self.tag_c2a = (tag_bridge_c2a.strip() if tag_bridge_c2a else base)

        # Interfaces TCP
        self.iface_a = None
        self.iface_b = None
        self.iface_c = None

        # Local IDs (para evitar rebotes)
        self.local_id_a: Optional[str] = None
        self.local_id_b: Optional[str] = None
        self.local_id_c: Optional[str] = None

        # Sincronización
        self._lock = threading.RLock()
        self._stop = threading.Event()

        # Dedup y rate limit
        self.dedup = DedupWindow(dedup_ttl)
        self.rl_a2b = RateLimiter(rate_limit_per_side)
        self.rl_b2a = RateLimiter(rate_limit_per_side)
        self.rl_a2c = RateLimiter(rate_limit_per_side)
        self.rl_c2a = RateLimiter(rate_limit_per_side)

    # --------------------------------------------------------
    #  Conexión y ciclo de vida
    # --------------------------------------------------------

    def connect(self):
        """
        Abre las 3 conexiones TCP y se suscribe al bus de eventos Meshtastic.
        """
        print(f"[triple-bridge] Conectando A: {self.a_host}:{self.a_port}")
        self.iface_a = _TCPI(hostname=self.a_host, portNumber=self.a_port)

        print(f"[triple-bridge] Conectando B: {self.b_host}:{self.b_port}")
        self.iface_b = _TCPI(hostname=self.b_host, portNumber=self.b_port)

        print(f"[triple-bridge] Conectando C: {self.c_host}:{self.c_port}")
        self.iface_c = _TCPI(hostname=self.c_host, portNumber=self.c_port)

        # Intentar descubrir los local_id de cada nodo (si el SDK los expone)
        self.local_id_a = self._discover_local_id(self.iface_a, "A")
        self.local_id_b = self._discover_local_id(self.iface_b, "B")
        self.local_id_c = self._discover_local_id(self.iface_c, "C")

        # Suscripción global de recepción
        pub.subscribe(self._on_rx, "meshtastic.receive")

        print(
            f"[triple-bridge] Conectado. "
            f"local_id_a={self.local_id_a}  local_id_b={self.local_id_b}  local_id_c={self.local_id_c}"
        )

    def _discover_local_id(self, iface, label: str) -> Optional[str]:
        """
        Extrae el identificador local del nodo (si lo hay) y lo normaliza a string tipo "!XXXXXXXX".
        """
        try:
            info = getattr(iface, "myInfo", None) or {}
            local_id = (
                info.get("my_node_num")
                or info.get("num")
                or info.get("id")
                or info.get("nodeNum")
            )
            if isinstance(local_id, int):
                local_id = f"!{local_id:08x}"
            if local_id:
                s = str(local_id)
                print(f"[triple-bridge] local_id_{label}={s}")
                return s
        except Exception:
            pass
        return None

    def close(self):
        """
        Cierra las interfaces TCP y se desuscribe del bus Meshtastic.
        """
        try:
            pub.unsubscribe(self._on_rx, "meshtastic.receive")
        except Exception:
            pass

        for name, iface in (("A", self.iface_a), ("B", self.iface_b), ("C", self.iface_c)):
            try:
                if iface and hasattr(iface, "close"):
                    iface.close()
                    print(f"[triple-bridge] Cerrada interfaz {name}")
            except Exception as e:
                print(f"[triple-bridge] Error cerrando interfaz {name}: {type(e).__name__}: {e}")

    def run_forever(self):
        """
        Bucle principal: mantiene vivo el proceso hasta Ctrl+C.
        """
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    # --------------------------------------------------------
    #  Handler RX global
    # --------------------------------------------------------

# --------------------------------------------------------
#  HUB via broker: polling de backlog y envío hacia A
# --------------------------------------------------------

def _hub_poll_loop(self) -> None:
    """
    Bucle de polling del backlog del broker para simular "RX desde A" sin abrir TCP a A.

    Estrategia:
      - Lee cada BROKER_POLL_SEC el backlog desde _hub_last_ts.
      - Por cada evento recibido, llama a _bridge_from_a(...) con el canal y texto.
      - Solo reenvía los tipos activados (FORWARD_TEXT / FORWARD_POSITION).
    """
    # Primer arranque: arrancar desde "ahora" para no reprocesar histórico
    if self._hub_last_ts is None:
        self._hub_last_ts = int(time.time())

    portnums: list[str] = []
    if self.forward_text:
        portnums.append("TEXT_MESSAGE_APP")
    if self.forward_position:
        # Si quieres posiciones/telemetría como resumen, añade estos
        portnums.extend(["POSITION_APP", "TELEMETRY_APP"])

    while not self._stop.is_set():
        try:
            assert self._broker is not None
            items = self._broker.fetch_backlog(
                since_ts=int(self._hub_last_ts or 0),
                portnums=portnums or ["TEXT_MESSAGE_APP"],
                limit=int(self.broker_fetch_limit or 2000),
            )

            # Orden por tiempo por seguridad
            items.sort(key=lambda x: int(x.get("rx_time") or 0))

            for obj in items:
                self._process_hub_event(obj)

        except Exception as e:
            print(f"[triple-bridge] HUB poll error: {type(e).__name__}: {e}")

        time.sleep(float(self.broker_poll_sec or 1.5))

def _process_hub_event(self, obj: dict) -> None:
    """
    Procesa un evento del backlog del broker y lo trata como si fuera RX desde A.
    Formato esperado (del broker):
      - rx_time (int epoch)
      - channel (int)
      - portnum (str, p.ej. TEXT_MESSAGE_APP)
      - fromId (str, p.ej. !1234abcd)
      - text (solo en TEXT_MESSAGE_APP)
      - position/telemetry (si aplica)
    """
    try:
        rx_time = int(obj.get("rx_time") or 0)
        if rx_time <= 0:
            return

        # Avanzar watermark (sumamos 1s para evitar repetir el mismo timestamp)
        if self._hub_last_ts is None or rx_time >= self._hub_last_ts:
            self._hub_last_ts = rx_time + 1

        port = str(obj.get("portnum") or "").upper()
        ch = int(obj.get("channel") or 0)
        frm = str(obj.get("fromId") or "")

        want_text = self.forward_text and ("TEXT_MESSAGE_APP" in port or port == "TEXT")
        want_pos = self.forward_position and (
            ("POSITION_APP" in port) or ("TELEMETRY_APP" in port) or (port == "POSITION") or (port == "TELEMETRY")
        )
        if not (want_text or want_pos):
            return

        # Construir 'decoded' compatible con el resto del código
        decoded = {"portnum": port, "channel": ch}
        text = ""

        if want_text:
            text = str(obj.get("text") or "")
            decoded["text"] = text
        else:
            # Para pos/telemetría, pasa el dict crudo
            if "position" in obj:
                decoded["position"] = obj.get("position") or {}
            if "telemetry" in obj:
                decoded["telemetry"] = obj.get("telemetry") or {}

        self._bridge_from_a(ch, frm, text, decoded, want_text, want_pos)

    except Exception as e:
        print(f"[triple-bridge] HUB event error: {type(e).__name__}: {e}")

def _send_text_to_hub(self, text: str, ch: int) -> bool:
    """
    Enviar texto hacia el HUB (A).
    - Modo tcp: sendText directo a iface_a
    - Modo broker: SEND_TEXT al BacklogServer
    """
    msg = _norm_text(text or "")
    if not msg:
        return False

    if self.hub_mode == "broker":
        if not self._broker:
            return False
        return self._broker.send_text(msg, ch=int(ch or 0), dest=None, ack=False)

    # tcp
    if not self.iface_a:
        return False
    try:
        self.iface_a.sendText(
            msg,
            destinationId="^all",
            wantAck=bool(self.require_ack),
            wantResponse=False,
            channelIndex=int(ch or 0),
        )
        return True
    except Exception:
        return False

    def _on_rx(self, interface=None, packet=None, **kwargs):
        """
        Callback de recepción Meshtastic (pubsub).
        'interface' indica cuál de las 3 TCPInterface ha recibido el paquete.
        """
        try:
            pkt = packet or {}
            decoded = pkt.get("decoded") or {}

            # puerto / tipo de app
            port = (
                decoded.get("portnum")
                or decoded.get("portnum_name")
                or decoded.get("portnum_str")
                or decoded.get("portnumText")
            )
            if isinstance(port, int):
                port_str = str(port)
            else:
                port_str = str(port or "").upper()

            # qué tipos de mensaje queremos reenviar
            if self.forward_text:
                want_text = ("TEXT_MESSAGE_APP" in port_str) or (port_str == "TEXT")
            else:
                want_text = False

            if self.forward_position:
                want_pos = (
                    ("POSITION_APP" in port_str)
                    or (port_str == "POSITION")
                    or ("TELEMETRY_APP" in port_str)
                    or (port_str == "TELEMETRY")
                )
            else:
                want_pos = False

            if not (want_text or want_pos):
                return

            # canal lógico
            ch = (
                decoded.get("channel")
                if decoded.get("channel") is not None
                else pkt.get("channel")
            )
            try:
                ch = int(ch) if ch is not None else 0
            except Exception:
                ch = 0

            # emisor y texto
            frm = pkt.get("fromId") or decoded.get("fromId") or pkt.get("from")
            frm = str(frm or "")

            text = decoded.get("text") or decoded.get("payload") or ""
            text = str(text or "")

            # de qué interfaz proviene
            came_from_a = interface is self.iface_a
            came_from_b = interface is self.iface_b
            came_from_c = interface is self.iface_c

            if not (came_from_a or came_from_b or came_from_c):
                # Paquete de otra interface (si existiera): ignorar
                return

            # Según origen, aplicamos las direcciones que tocan
            if came_from_a:
                self._bridge_from_a(ch, frm, text, decoded, want_text, want_pos)
            elif came_from_b:
                self._bridge_from_b(ch, frm, text, decoded, want_text, want_pos)
            elif came_from_c:
                self._bridge_from_c(ch, frm, text, decoded, want_text, want_pos)

        except Exception as e:
            print(f"[triple-bridge] on_rx error: {type(e).__name__}: {e}")

    # --------------------------------------------------------
    #  Operaciones por origen
    # --------------------------------------------------------

    def _bridge_from_a(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
        """
        Reenvío desde A hacia B y hacia C.
        """
        # A → B
        if ch in self.a2b_map and self.iface_b is not None:
            self._bridge_one(
                direction="A2B",
                src_label="A",
                dst_label="B",
                src_iface=self.iface_a,
                dst_iface=self.iface_b,
                ch=ch,
                out_ch=self.a2b_map[ch],
                frm=frm,
                text=text,
                decoded=decoded,
                want_text=want_text,
                want_pos=want_pos,
                rl=self.rl_a2b,
                tag=self.tag_a2b,
                dst_local_id=self.local_id_b,
            )

        # A → C
        if ch in self.a2c_map and self.iface_c is not None:
            self._bridge_one(
                direction="A2C",
                src_label="A",
                dst_label="C",
                src_iface=self.iface_a,
                dst_iface=self.iface_c,
                ch=ch,
                out_ch=self.a2c_map[ch],
                frm=frm,
                text=text,
                decoded=decoded,
                want_text=want_text,
                want_pos=want_pos,
                rl=self.rl_a2c,
                tag=self.tag_a2c,
                dst_local_id=self.local_id_c,
            )

    def _bridge_from_b(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
    """
    Reenvío desde B hacia A.
    En modo HUB=broker, el destino A se envía vía BacklogServer (SEND_TEXT).
    """
    if ch not in self.b2a_map:
        return

    out_ch = self.b2a_map[ch]

    # Anti-bucle si conocemos local_id_a (solo en modo tcp)
    if self.local_id_a and frm == self.local_id_a:
        return

    # Rate limit / dedup
    if not self.rl_b2a.allow():
        return

    payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True, ensure_ascii=False)
    key = _hash_key("B2A", frm, ch, payload_for_hash)
    if self.dedup.seen(key):
        return

    if want_text:
        msg = _norm_text(text)
        tag = self.tag_b2a
        if tag and tag not in msg:
            msg = f"{tag} {msg}"

        ok = False
        if self.hub_mode == "broker":
            ok = self._send_text_to_hub(msg, out_ch)
            if ok:
                print(f"[triple-bridge] B2A B->A ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK via BROKER")
            else:
                print(f"[triple-bridge] B2A B->A sendText ERROR via BROKER")
        else:
            # modo tcp original
            if self.iface_a is None:
                return
            try:
                self.iface_a.sendText(
                    msg,
                    destinationId="^all",
                    wantAck=bool(self.require_ack),
                    wantResponse=False,
                    channelIndex=int(out_ch),
                )
                ok = True
                print(f"[triple-bridge] B2A B->A ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK")
            except Exception as e:
                print(f"[triple-bridge] B2A B->A sendText ERROR: {type(e).__name__}: {e}")

    elif want_pos:
        # (opcional) Mantener el comportamiento anterior (resumen como texto) hacia A.
        try:
            summary = {
                "via": "triple-bridge",
                "from": frm[-8:] if frm else "?",
                "lat": decoded.get("position", {}).get("latitude"),
                "lon": decoded.get("position", {}).get("longitude"),
                "alt": decoded.get("position", {}).get("altitude"),
                "bat": decoded.get("deviceMetrics", {}).get("batteryLevel"),
            }
            msg = f"POS {summary}"
            tag = self.tag_b2a
            if tag:
                msg = f"{tag} {msg}"

            ok = self._send_text_to_hub(msg, out_ch) if self.hub_mode == "broker" else False
            if self.hub_mode == "broker":
                print(f"[triple-bridge] B2A B->A ch {ch}->{out_ch} POS OK via BROKER" if ok else f"[triple-bridge] B2A B->A POS ERROR via BROKER")
        except Exception as e:
            print(f"[triple-bridge] B2A POS build/send ERROR: {type(e).__name__}: {e}")

    def _bridge_from_c(self, ch: int, frm: str, text: str, decoded: dict, want_text: bool, want_pos: bool):
    """
    Reenvío desde C hacia A.
    En modo HUB=broker, el destino A se envía vía BacklogServer (SEND_TEXT).
    """
    if ch not in self.c2a_map:
        return

    out_ch = self.c2a_map[ch]

    # Anti-bucle si conocemos local_id_a (solo en modo tcp)
    if self.local_id_a and frm == self.local_id_a:
        return

    # Rate limit / dedup
    if not self.rl_c2a.allow():
        return

    payload_for_hash = _norm_text(text) if want_text else json.dumps(decoded, sort_keys=True, ensure_ascii=False)
    key = _hash_key("C2A", frm, ch, payload_for_hash)
    if self.dedup.seen(key):
        return

    if want_text:
        msg = _norm_text(text)
        tag = self.tag_c2a
        if tag and tag not in msg:
            msg = f"{tag} {msg}"

        ok = False
        if self.hub_mode == "broker":
            ok = self._send_text_to_hub(msg, out_ch)
            if ok:
                print(f"[triple-bridge] C2A C->A ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK via BROKER")
            else:
                print(f"[triple-bridge] C2A C->A sendText ERROR via BROKER")
        else:
            # modo tcp original
            if self.iface_a is None:
                return
            try:
                self.iface_a.sendText(
                    msg,
                    destinationId="^all",
                    wantAck=bool(self.require_ack),
                    wantResponse=False,
                    channelIndex=int(out_ch),
                )
                ok = True
                print(f"[triple-bridge] C2A C->A ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK")
            except Exception as e:
                print(f"[triple-bridge] C2A C->A sendText ERROR: {type(e).__name__}: {e}")

    elif want_pos:
        # (opcional) Mantener el comportamiento anterior (resumen como texto) hacia A.
        try:
            summary = {
                "via": "triple-bridge",
                "from": frm[-8:] if frm else "?",
                "lat": decoded.get("position", {}).get("latitude"),
                "lon": decoded.get("position", {}).get("longitude"),
                "alt": decoded.get("position", {}).get("altitude"),
                "bat": decoded.get("deviceMetrics", {}).get("batteryLevel"),
            }
            msg = f"POS {summary}"
            tag = self.tag_c2a
            if tag:
                msg = f"{tag} {msg}"

            ok = self._send_text_to_hub(msg, out_ch) if self.hub_mode == "broker" else False
            if self.hub_mode == "broker":
                print(f"[triple-bridge] C2A C->A ch {ch}->{out_ch} POS OK via BROKER" if ok else f"[triple-bridge] C2A C->A POS ERROR via BROKER")
        except Exception as e:
            print(f"[triple-bridge] C2A POS build/send ERROR: {type(e).__name__}: {e}")

    # --------------------------------------------------------
    #  Reenvío individual
    # --------------------------------------------------------

    def _bridge_one(
        self,
        direction: str,
        src_label: str,
        dst_label: str,
        src_iface,
        dst_iface,
        ch: int,
        out_ch: int,
        frm: str,
        text: str,
        decoded: dict,
        want_text: bool,
        want_pos: bool,
        rl: RateLimiter,
        tag: Optional[str],
        dst_local_id: Optional[str],
    ):
        """
        Reenvía un mensaje de un nodo origen a un nodo destino con:
        - Anti-bucle por local_id del destino.
        - Rate limit por sentido.
        - Deduplicación por hash (direction,from,canal,payload).
        """
        try:
            # Anti-bucle: no reenvíes si el emisor coincide con el local del destino
            if dst_local_id and frm == dst_local_id:
                return

            # Rate limit
            if not rl.allow():
                return

            # Payload para dedup
            if want_text:
                payload_for_hash = _norm_text(text)
            else:
                payload_for_hash = json.dumps(decoded, sort_keys=True, ensure_ascii=False)

            key = _hash_key(direction, frm, ch, payload_for_hash)
            if self.dedup.seen(key):
                return

            # Reenvío
            if want_text:
                msg = _norm_text(text)
                # Etiqueta de bridge si procede (A2B, B2A, etc.)
                if tag:
                    if tag not in msg:
                        msg = f"{tag} {msg}"

                try:
                    # broadcast en el destino (canal lógico out_ch)
                    dst_iface.sendText(
                        msg,
                        destinationId="^all",
                        wantAck=bool(self.require_ack),
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )
                    print(
                        f"[triple-bridge] {direction} {src_label}->{dst_label} "
                        f"ch {ch}->{out_ch} txt({len(msg.encode('utf-8'))}B) OK"
                    )
                except Exception as e:
                    print(
                        f"[triple-bridge] {direction} {src_label}->{dst_label} "
                        f"sendText ERROR: {type(e).__name__}: {e}"
                    )

            elif want_pos:
                # Resumen ligero como texto
                try:
                    summary = {
                        "via": "triple-bridge",
                        "from": frm[-8:] if frm else "?",
                        "lat": decoded.get("position", {}).get("latitude"),
                        "lon": decoded.get("position", {}).get("longitude"),
                        "alt": decoded.get("position", {}).get("altitude"),
                        "bat": decoded.get("deviceMetrics", {}).get("batteryLevel"),
                    }
                    msg = f"POS {summary}"
                    if tag:
                        msg = f"{tag} {msg}"

                    dst_iface.sendText(
                        msg,
                        destinationId="^all",
                        wantAck=False,
                        wantResponse=False,
                        channelIndex=int(out_ch),
                    )
                    print(
                        f"[triple-bridge] {direction} {src_label}->{dst_label} "
                        f"ch {ch}->{out_ch} POS OK"
                    )
                except Exception as e:
                    print(
                        f"[triple-bridge] {direction} {src_label}->{dst_label} "
                        f"sendPOS ERROR: {type(e).__name__}: {e}"
                    )

        except Exception as e:
            print(f"[triple-bridge] _bridge_one error: {type(e).__name__}: {e}")


# ============================================================
#  CLI simple: lee entorno, arranca bridge y bucle
# ============================================================

def main():
    """
    Punto de entrada CLI.
    - Lee A_HOST/B_HOST/C_HOST (y puertos).
    - Lee mapas de canales y flags desde el entorno.
    - Arranca la TripleBridge y se queda en bucle.
    """
    a_host = (os.getenv("A_HOST", "") or "").strip()
    b_host = (os.getenv("B_HOST", "") or "").strip()
    c_host = (os.getenv("C_HOST", "") or "").strip()

    hub_mode = (os.getenv("HUB_MODE", "tcp") or "tcp").strip().lower()

if hub_mode == "broker":
    # En modo broker, A_HOST no es necesario para abrir TCP (A lo gestiona el broker),
    # pero lo dejamos opcional para logs/consistencia.
    if not b_host or not c_host:
        raise SystemExit("HUB_MODE=broker requiere B_HOST y C_HOST (A_HOST es opcional).")
else:
    if not a_host or not b_host or not c_host:
        raise SystemExit("Debe indicar A_HOST, B_HOST y C_HOST en el entorno.")

    try:
        a_port = int(os.getenv("A_PORT", "4403"))
    except Exception:
        a_port = 4403
    try:
        b_port = int(os.getenv("B_PORT", "4403"))
    except Exception:
        b_port = 4403
    try:
        c_port = int(os.getenv("C_PORT", "4403"))
    except Exception:
        c_port = 4403

    a2b = _parse_ch_map(os.getenv("A2B_CH_MAP", "0:0"))
    b2a = _parse_ch_map(os.getenv("B2A_CH_MAP", "0:0"))
    a2c = _parse_ch_map(os.getenv("A2C_CH_MAP", "0:0"))
    c2a = _parse_ch_map(os.getenv("C2A_CH_MAP", "0:0"))

    forward_text = _truthy(os.getenv("FORWARD_TEXT", "1"), True)
    forward_position = _truthy(os.getenv("FORWARD_POSITION", "0"), False)
    require_ack = _truthy(os.getenv("REQUIRE_ACK", "0"), False)
    rate = int(os.getenv("RATE_LIMIT_PER_SIDE", "8") or "8")
    dedup = int(os.getenv("DEDUP_TTL", "45") or "45")

    tag_base = os.getenv("TAG_BRIDGE", "[BRIDGE]")

    tag_a2b = os.getenv("TAG_BRIDGE_A2B", "").strip() or None
    tag_b2a = os.getenv("TAG_BRIDGE_B2A", "").strip() or None
    tag_a2c = os.getenv("TAG_BRIDGE_A2C", "").strip() or None
    tag_c2a = os.getenv("TAG_BRIDGE_C2A", "").strip() or None

    bridge = TripleBridge(
        a_host=a_host,
        a_port=a_port,
        hub_mode=hub_mode,
        broker_ctrl_host=(os.getenv('BROKER_CTRL_HOST') or '').strip() or None,
        broker_ctrl_port=int(os.getenv('BROKER_CTRL_PORT') or 8765),
        broker_poll_sec=float(os.getenv('BROKER_POLL_SEC') or 1.5),
        broker_fetch_limit=int(os.getenv('BROKER_FETCH_LIMIT') or 2000),
        broker_timeout_s=float(os.getenv('BROKER_TIMEOUT_S') or 8.0),
        b_host=b_host,
        b_port=b_port,
        c_host=c_host,
        c_port=c_port,
        a2b_map=a2b,
        b2a_map=b2a,
        a2c_map=a2c,
        c2a_map=c2a,
        forward_text=forward_text,
        forward_position=forward_position,
        require_ack=require_ack,
        rate_limit_per_side=rate,
        dedup_ttl=dedup,
        tag_bridge=tag_base,
        tag_bridge_a2b=tag_a2b,
        tag_bridge_b2a=tag_b2a,
        tag_bridge_a2c=tag_a2c,
        tag_bridge_c2a=tag_c2a,
    )

    bridge.connect()
    bridge.run_forever()


if __name__ == "__main__":
    main()
