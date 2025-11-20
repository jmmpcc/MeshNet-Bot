#!/usr/bin/env python3 	   	 	 	    	   		 
# -*- coding: utf-8 -*- 	  	   	 	 	     	 	
""" 	  		 	 					 	  		 
meshtastic_to_aprs.py (v6.1.3)	 		  	 	 			  		 		 
Puente Meshtastic ⇄ APRS vía Soundmodem (KISS TCP 8100) + Control UDP local.	    	   			 		    	 

- /aprs (bot) -> UDP local -> TX APRS (troceo automático).			 	   		  	 	 			 	
- APRS -> Mesh: si el comentario/status contiene [CHx], se reenvía al canal x del broker.		 		    	 				  	 	 
Con verificacion en APRS-IS:					 			 		   		 		 
    python meshtastic_to_aprs_v5.4.py --aprsis-user EB2XXX-10 --aprsis-passcode 12345
Sin verificacion APRS-IS
    python meshtastic_to_aprs_v5.4.py

"""

from __future__ import annotations
import asyncio, json, os, re, socket
from typing import Optional, List, Tuple
import aprslib

# === [NUEVO] Canal KISS (0=A, 1=B, etc.) y saneo ASCII ===
import unicodedata

KISS_CHANNEL = int(os.getenv("KISS_CHANNEL", "0"))
if not (0 <= KISS_CHANNEL <= 15):
    KISS_CHANNEL = 0

_APRS_ALLOWED = set(chr(c) for c in range(32, 127))  # 0x20..0x7E

# --- Lista blanca de indicativos APRS autorizados (RF -> Mesh/Control)
# Formato: variable de entorno APRS_ALLOWED_SOURCES="EA2XXX-7,EA2YYY-9"
APRS_ALLOWED_SOURCES = {
    s.strip().upper()
    for s in os.getenv("APRS_ALLOWED_SOURCES", "").split(",")
    if s.strip()
}

def _aprs_source_allowed(src: str) -> bool:
    """
    Devuelve True si el indicativo de origen está autorizado.
    Si APRS_ALLOWED_SOURCES está vacío, no se filtra nada (todo permitido).
    """
    if not APRS_ALLOWED_SOURCES:
        return True
    return (src or "").strip().upper() in APRS_ALLOWED_SOURCES



def _aprs_ascii(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = s.encode("ascii", "ignore").decode("ascii", "ignore")
    s = "".join(ch if ch in _APRS_ALLOWED else "?" for ch in s)
    return " ".join(s.split())

def _to_ascii7(text: str) -> str:
    """
    Convierte a ASCII 7-bit seguro.
    Sustituye caracteres no válidos por '?' para no romper la trama.
    """
    if not text:
        return ""
    return "".join(ch if 32 <= ord(ch) <= 126 else "?" for ch in text)


# =========================
# CONFIG
# =========================
BROKER_HOST = os.getenv("BROKER_HOST", "127.0.0.1").strip()
BROKER_PORT = int(os.getenv("BROKER_PORT", "8765"))

KISS_HOST = os.getenv("KISS_HOST", "127.0.0.1").strip()
KISS_PORT = int(os.getenv("KISS_PORT", "8100"))

MY_CALL = os.getenv("APRS_CALL", "EB2EAS-11").strip()
GATEWAY_DEST_PREFIX = os.getenv("APRS_GATEWAY_PREFIX", "EB2EAS").strip().upper()
APRS_PATH = [p for p in (os.getenv("APRS_PATH", "WIDE1-1,WIDE2-1").strip() or "").split(",") if p]

MAX_MSG_LEN = int(os.getenv("APRS_MSG_MAX", "67"))
MAX_STATUS_LEN = int(os.getenv("APRS_STATUS_MAX", "67"))

MESHTASTIC_CHANNEL = int(os.getenv("MESHTASTIC_CH", "0"))  # canal por defecto Mesh

# Control UDP local (bot -> APRS)
CONTROL_UDP_HOST = os.getenv("APRS_CTRL_HOST", "127.0.0.1").strip()
CONTROL_UDP_PORT = int(os.getenv("APRS_CTRL_PORT", "9464"))

# BacklogServer del broker (para APRS -> Mesh)
BROKER_CTRL_HOST = os.getenv("BROKER_CTRL_HOST", BROKER_HOST)
try:
    BROKER_CTRL_PORT = int(os.getenv("BROKER_CTRL_PORT", str(int(BROKER_PORT) + 1)))
except Exception:
    BROKER_CTRL_PORT = 8766  # fallback

# --- Uplink APRS-IS (aprs.fi) con aprslib ---
# Si APRSIS_USER y APRSIS_PASSCODE están definidos y no vacíos → subimos a APRS-IS.
APRSIS_USER     = os.getenv("APRSIS_USER", "").strip()     # p.ej. "EB2XXX-10"
APRSIS_PASSCODE = os.getenv("APRSIS_PASSCODE", "").strip() # passcode APRS-IS para ese indicativo
APRSIS_HOST     = os.getenv("APRSIS_HOST", "rotate.aprs2.net").strip()
APRSIS_PORT     = int(os.getenv("APRSIS_PORT", "14580"))
APRSIS_FILTER   = os.getenv("APRSIS_FILTER", "").strip()   # opcional, p.ej. "m/50"

def _aprsis_ready() -> bool:
    return bool(APRSIS_USER and APRSIS_PASSCODE)

# --- De-dup sencillo para evitar doble TX (bot UDP + eco broker) ---
import time

_DEDUP_TTL_S = int(os.getenv("APRS_DEDUP_TTL", "20"))  # segundos
_recent_aprs_keys: dict[str, float] = {}

# --- Debug opcional para APRS ---
import os as _os
APRS_DEBUG = int(_os.getenv("APRS_DEBUG", "1"))  # 1=log activo (por defecto), 0=callado

def _aprs_dbg(msg: str) -> None:
    if APRS_DEBUG:
        print(msg)

# --- Gate APRS→Mesh: 1=ON (por defecto), 0=OFF ---
APRS_GATE_ENABLED = int(os.getenv("APRS_GATE_ENABLED", "1"))

def _aprs_gate_is_enabled() -> bool:
    return bool(APRS_GATE_ENABLED)



def _dedup_key(dest: str, text: str) -> str:
    d = (dest or "broadcast").strip().lower()
    t = re.sub(r"\s+", " ", (text or "").strip())
    return f"{d}|{t}"[:512]

def _dedup_mark(dest: str, text: str) -> None:
    now = time.time()
    _recent_aprs_keys[_dedup_key(dest, text)] = now + _DEDUP_TTL_S
    # Prune
    stale = [k for k, exp in _recent_aprs_keys.items() if exp < now]
    for k in stale:
        _recent_aprs_keys.pop(k, None)

def _dedup_seen(dest: str, text: str) -> bool:
    now = time.time()
    k = _dedup_key(dest, text)
    exp = _recent_aprs_keys.get(k)
    if exp is None:
        return False
    if exp < now:
        _recent_aprs_keys.pop(k, None)
        return False
    return True

# --- Cliente APRS-IS (persistente) usando aprslib (bloqueante => executor) ---
class _AprsISClient:
    def __init__(self, user: str, passcode: str, host: str, port: int, filt: str = ""):
        self.user = user
        self.passcode = passcode
        self.host = host
        self.port = port
        self.filt = filt
        self._is = None
        self._lock = asyncio.Lock()
        self._announced = False  # <<< NUEVO

    def _ensure_sync(self):
        # (Re)conectar si hace falta
        if self._is is None:
            try:
                self._is = aprslib.IS(self.user, passwd=self.passcode, host=self.host, port=self.port)
                self._is.connect()
                if self.filt:
                    try:
                        self._is.sendall(f"filter {self.filt}")
                    except Exception:
                        pass
                if not self._announced:
                    print(f"[aprs→IS] Conectado OK como {self.user} a {self.host}:{self.port} (filtro='{self.filt or '-'}').")
                    print("           Subiré SOLO POSICIONES con [CHx] en formato third-party (respetando NOGATE/RFONLY).")
                    self._announced = True
            except Exception as e:
                if not self._announced:
                    print(f"[aprs→IS] ❌ No se pudo conectar a {self.host}:{self.port} como {self.user}: {e}")
                    self._announced = True
                raise

    def _send_line_sync(self, line: str):
        self._ensure_sync()
        self._is.sendall(line)

    async def connect(self):
        """Conecta ahora (login + filter si procede) y deja la sesión preparada."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._ensure_sync)


    async def send_line(self, line: str):
        async with self._lock:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._send_line_sync, line)
            except Exception:
                # fuerza reconexión en el siguiente intento
                self._is = None
                raise

_aprsis_client: _AprsISClient | None = None

def _should_not_gate(info_text: str) -> bool:
    """
    Devuelve True si el comentario contiene NOGATE o RFONLY (no subir a APRS-IS).
    """
    t = (info_text or "").upper()
    return ("NOGATE" in t) or ("RFONLY" in t)

def _make_thirdparty_line(pkt: dict, igate_call: str) -> str | None:
    """
    Construye una línea APRS-IS en formato 'third-party', preservando el paquete original:
      IGATE>APRS,TCPIP*,qAR,IGATE:}SRC>DEST,PATH:payload

    Requisitos:
      - pkt debe tener: src, dest, path (lista o vacía) e info (str/bytes)
      - igate_call: tu indicativo de iGate (p.ej. 'EB2XXX-10')

    Devuelve:
      - str con la línea TNC2 lista para enviar a APRS-IS, o
      - None si faltan campos o si el payload incluye NOGATE/RFONLY.
    """
    if not pkt:
        return None

    src = (pkt.get("src") or "").upper().strip()
    dest = (pkt.get("dest") or "").upper().strip()
    path_list = pkt.get("path") or []
    info = pkt.get("info")

    if isinstance(info, bytes):
        try:
            info = info.decode("utf-8", "ignore")
        except Exception:
            info = info.decode("latin-1", "ignore")
    info = (info or "").replace("\r", " ").replace("\n", " ").strip()

    # Campos mínimos
    if not src or not dest or not info:
        return None

    # Respetar NOGATE / RFONLY
    if _should_not_gate(info):
        return None

    # Sanitiza a ASCII visible + espacio (IS no admite binarios)
    info_ascii = "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in info)

    # PATH opcional
    path = ",".join(path_list) if path_list else ""

    # Paquete original (dentro de '}')
    orig = f"{src}>{dest}{(','+path) if path else ''}:{info_ascii}"

    ig = (igate_call or "NOCALL-10").upper().strip()
    # Formato third-party recomendado para iGates RF→IS
    line = f"{ig}>APRS,TCPIP*,qAR,{ig}:" + orig

    # Longitud segura (IS suele aceptar ~512B por línea)
    return line[:510]

# =========================
# === KISS helpers =========
# =========================
_FEND  = 0xC0
_FESC  = 0xDB
_TFEND = 0xDC
_TFESC = 0xDD

def _kiss_escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == _FEND:
            out.extend([_FESC, _TFEND])
        elif b == _FESC:
            out.extend([_FESC, _TFESC])
        else:
            out.append(b)
    return bytes(out)

def _kiss_unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == _FESC and i + 1 < n:
            nb = data[i + 1]
            if nb == _TFEND:
                out.append(_FEND); i += 2; continue
            if nb == _TFESC:
                out.append(_FESC); i += 2; continue
        out.append(b); i += 1
    return bytes(out)

def kiss_wrap(ax25_frame: bytes, port: int = 0, cmd: int = 0x00) -> bytes:
    typ = ((port & 0x0F) << 4) | (cmd & 0x0F)
    payload = bytes([typ]) + _kiss_escape(ax25_frame)
    return bytes([_FEND]) + payload + bytes([_FEND])

def kiss_iter_frames_from_buffer(buf: bytearray):
    """
    Consume buf (bytearray) y entrega tramas DATA des-escapadas, dejando resto en buf.
    """
    frames: List[bytes] = []
    while True:
        try:
            start = buf.index(_FEND)
        except ValueError:
            break
        try:
            end = buf.index(_FEND, start + 1)
        except ValueError:
            if start > 0:
                del buf[:start]
            break
        raw = bytes(buf[start + 1:end])
        del buf[:end + 1]
        if not raw:
            continue
        if (raw[0] & 0x0F) != 0x00:  # solo DATA
            continue
        frames.append(_kiss_unescape(raw[1:]))
    return frames

def _hexdump(b: bytes, width: int = 16) -> str:
    s = []
    for i in range(0, len(b), width):
        chunk = b[i:i+width]
        hexs = " ".join(f"{x:02X}" for x in chunk)
        ascii_ = "".join(chr(x) if 32 <= x <= 126 else "." for x in chunk)
        s.append(f"{i:04X}  {hexs:<{width*3}}  {ascii_}")
    return "\n".join(s)


# =========================
# === AX.25 helpers ========
# =========================
def _call_ssid_parts(call: str) -> Tuple[str, int]:
    call = (call or "").upper().strip()
    if "-" in call:
        c, s = call.split("-", 1)
        try:
            ssid = int(s)
        except Exception:
            ssid = 0
    else:
        c, ssid = call, 0
    return c[:6].ljust(6), max(0, min(15, ssid))

def _addr_field(call: str, last: bool = False) -> bytes:
    c6, ssid = _call_ssid_parts(call)
    b = bytearray(7)
    for i, ch in enumerate(c6.encode("ascii", "ignore")[:6]):
        b[i] = (ch << 1) & 0xFE
    b[6] = 0x60 | ((ssid & 0x0F) << 1) | (0x01 if last else 0x00)
    return bytes(b)

def _decode_ax25_addrs(axhdr: bytes) -> list[str]:
    # axhdr: múltiplo de 7 bytes (DEST,SRC,PATH..., cada 7B)
    out = []
    for i in range(0, len(axhdr), 7):
        c = axhdr[i:i+7]
        if len(c) < 7: break
        call = "".join(chr((c[j] >> 1) & 0x7F) for j in range(6)).strip()
        ssid = (c[6] >> 1) & 0x0F
        last = (c[6] & 0x01) == 0x01
        out.append(f"{call}-{ssid} {'(last)' if last else ''}")
    return out


def build_ax25_ui(dest: str, src: str, path: List[str] | Tuple[str, ...], payload: bytes) -> bytes:
    hops = [dest, src] + list(path or [])
    addrs = [_addr_field(c, last=(i == len(hops) - 1)) for i, c in enumerate(hops)]
    return b"".join(addrs) + b"\x03" + b"\xF0" + (payload or b"")

# =========================
# === Troceo mensajes =====
# =========================
def _split_by_words(text: str, max_len: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    parts, cur = [], ""
    for token in re.split(r"(\s+)", text):
        if not token:
            continue
        if len(cur) + len(token) <= max_len:
            cur += token
        else:
            if cur:
                parts.append(cur.rstrip())
            cur = token.lstrip()
            while len(cur) > max_len:  # corte duro si una “palabra” excede
                parts.append(cur[:max_len]); cur = cur[max_len:]
    if cur:
        parts.append(cur.rstrip())
    return parts

def build_aprs_status_chunks(text: str, max_len: int | None = None) -> List[bytes]:
    limit = int(max_len if max_len is not None else MAX_STATUS_LEN)
    text = _to_ascii7(text)  # <-- Sanitizar aquí
    base = _split_by_words(text, limit)
    if len(base) <= 1:
        return [b">" + base[0].encode("ascii", "ignore")] if base else []
    suf_worst = len(f" ({len(base)}/{len(base)})")
    parts = _split_by_words(text, limit - suf_worst)
    n = len(parts)
    return [b">" + f"{p} ({i}/{n})".encode("ascii", "ignore") for i, p in enumerate(parts, 1)]


def build_aprs_message_chunks(dest_call: str, text: str, max_len: int | None = None) -> List[bytes]:
    limit = int(max_len if max_len is not None else MAX_MSG_LEN)
    dest9 = ((dest_call or "").upper().strip() + " " * 9)[:9]
    text = _to_ascii7(text)  # <-- Sanitizar aquí
    parts = _split_by_words(text, limit - len(" (99/99)"))
    if len(parts) <= 1:
        return [f":{dest9}:{text}".encode("ascii", "ignore")]
    n = len(parts)
    return [f":{dest9}:{p} ({i}/{n})".encode("ascii", "ignore") for i, p in enumerate(parts, 1)]


# =========================
# === Parse APRS ===========
# =========================
def _decode_addr(addr7: bytes) -> Tuple[str, int, bool]:
    call = "".join(chr((addr7[i] >> 1) & 0x7F) for i in range(6)).strip()
    ssid = (addr7[6] >> 1) & 0x0F
    last = bool(addr7[6] & 0x01)
    return call, ssid, last

def parse_ax25_ui(frame: bytes) -> dict | None:
    try:
        p = memoryview(frame)
        addrs = []; off = 0
        while True:
            if off + 7 > len(p): return None
            a = bytes(p[off:off+7]); off += 7
            addrs.append(_decode_addr(a))
            if a[6] & 0x01: break
        if off + 2 > len(p): return None
        control = p[off]; pid = p[off + 1]; off += 2
        if control != 0x03 or pid != 0xF0: return None
        info_raw = bytes(p[off:])
        try:
            info = info_raw.decode("utf-8", "ignore")
        except Exception:
            info = info_raw.decode("latin-1", "ignore")

        dest_call = f"{addrs[0][0]}-{addrs[0][1]}" if addrs else ""
        src_call  = f"{addrs[1][0]}-{addrs[1][1]}" if len(addrs) > 1 else ""
        path = [f"{c}-{s}" for (c, s, _l) in addrs[2:]] if len(addrs) > 2 else []

        out = {"dest": dest_call, "src": src_call, "path": path,
               "info_raw": info_raw, "info": info}

        if info.startswith(">"):
            out["type"] = "status"
            out["text"] = info[1:].strip()
            return out

        if info.startswith(":") and len(info) >= 11 and ":" in info[10:]:
            # :DEST9:mensaje{nn}
            msg_dest = info[1:10].strip()
            rest = info[10:]
            if rest.startswith(":"): rest = rest[1:]
            text = re.sub(r"\{[ -~]{1,5}\}$", "", rest).strip()  # quita ACK {nn}
            out.update({"type": "message", "msg_dest": msg_dest, "text": text})
            return out

        return out
    except Exception:
        return None

# =========================
# === Canal en comentario ==
# =========================
# Soporta:
#   [CH 1] Texto
#   [CH1] Texto
#   [CANAL 3] Texto
#   [CH 1+10] Texto  (delay 10 min)
#   [CANAL3+5] Texto (delay 5 min)
_CH_REGEX = re.compile(
    r"\[(?:CH|CANAL)\s*([0-9]{1,2})(?:\s*([+])\s*([0-9]{1,4}))?\]",
    re.IGNORECASE,
)

def extract_channel_if_tagged(comment: str) -> tuple[Optional[int], str]:
    """
    Devuelve (canal, texto_sin_etiqueta) únicamente si hay [CHx] / [CANAL x] / [CHx+N].
    Ignora el sufijo +N. Si no hay etiqueta, devuelve (None, comment).
    No aplica canal por defecto (evita inyectar sin prefijo).
    """
    if not comment:
        return (None, "")
    m = _CH_REGEX.search(comment)
    if not m:
        return (None, comment.strip())
    try:
        ch = int(m.group(1))
    except Exception:
        return (None, comment.strip())
    ch = max(0, min(15, ch))
    text = (comment[:m.start()] + comment[m.end():]).strip()
    import re as _re
    text = _re.sub(r"\s{2,}", " ", text)
    return (ch, text)


def extract_channel_from_comment(comment: str, default_ch: int = MESHTASTIC_CHANNEL) -> Tuple[int, str]:
    """
    [CH2] Texto..., [CANAL 5] Aviso..., [CH 1+10] ...
    Devuelve (canal, texto_sin_etiqueta). Si no hay etiqueta, (default_ch, comment).
    Ignora el sufijo de programación (+N).
    """
    if not comment:
        return (int(default_ch), "")
    m = _CH_REGEX.search(comment)
    if not m:
        return (int(default_ch), comment.strip())
    try:
        ch = int(m.group(1))
    except Exception:
        ch = int(default_ch)

    ch = max(0, min(15, ch))
    text = (comment[:m.start()] + comment[m.end():]).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return (ch, text)

def extract_channel_and_delay(comment: str) -> tuple[Optional[int], Optional[int], str]:
    """
    Versión extendida para APRS→Mesh:
      - [CH 1] Hola      → (1, None, "Hola")
      - [CH1+10] Aviso   → (1, 10, "Aviso")   (10 minutos)
      - [CANAL 3+5] Test → (3, 5, "Test")

    Además, heurística para casos colapsados:
      - [CH42] → ch=4, delay=2   (cuando 42 > 15)

    delay_min está en minutos si se usa '+N' o se infiere por heurística; si no, None.
    """
    if not comment:
        return (None, None, "")

    m = _CH_REGEX.search(comment)
    if not m:
        return (None, None, comment.strip())

    raw = (m.group(1) or "").strip()
    sign = m.group(2)
    val  = m.group(3)

    ch: Optional[int] = None
    delay_min: Optional[int] = None

    # 1) Caso normal con '+N' explícito: [CH4+2], [CANAL 3+10], etc.
    if sign == "+" and val is not None:
        try:
            ch_val = int(raw)
        except Exception:
            return (None, None, comment.strip())
        ch = max(0, min(15, ch_val))
        try:
            delay_min = max(0, int(val))
        except Exception:
            delay_min = None

    # 2) Caso colapsado sin '+': [CH42] → si 42>15 y tiene 2 dígitos, interpretamos ch=4, delay=2
    else:
        try:
            ch_val = int(raw)
        except Exception:
            return (None, None, comment.strip())

        if ch_val > 15 and len(raw) == 2:
            # heurística específica APRS: primer dígito = canal, segundo = delay (minutos)
            try:
                ch = int(raw[0])
                delay_min = int(raw[1])
            except Exception:
                ch = ch_val
                delay_min = None
        else:
            ch = ch_val

        ch = max(0, min(15, ch))

    # Texto sin la etiqueta
    text = (comment[:m.start()] + comment[m.end():]).strip()
    import re as _re
    text = _re.sub(r"\s{2,}", " ", text)

    return (ch, delay_min, text)


def _is_position_pkt(pkt: dict) -> bool:
    """True si el payload APRS es de posición: empieza por ! / = @"""
    info = pkt.get("info") or ""
    return bool(info) and info[0] in "!/=@"


def _has_ch_tag_in_info(pkt: dict) -> bool:
    """True si en el campo info aparece [CHx] / [CANAL x] (con o sin +N)"""
    return bool(_CH_REGEX.search(pkt.get("info") or ""))


# =========================
# === TX APRS util ========
# =========================
def _tx_aprs_payload(payload: bytes, dest_hdr: str, path_override: Optional[List[str]] = None) -> bool:
    ax25 = build_ax25_ui(dest=dest_hdr, src=MY_CALL,
                         path=[p for p in (path_override or APRS_PATH) if p],
                         payload=payload)
    kiss = kiss_wrap(ax25, port=KISS_CHANNEL)  # [MOD] usa el canal elegido
    try:
        s = socket.create_connection((KISS_HOST, KISS_PORT), timeout=3.0)
        _kiss_init(s)                           # [NUEVO] fija TXDELAY/PERSIST/SLOTTIME
        s.sendall(kiss)
        s.close()
        
        print(f"[ctrl→aprs] TX {len(payload)}B → {dest_hdr}")
        return True
    except Exception as e:
        print(f"[ctrl→aprs] ❌ KISS send error: {e}")
        return False

# === [NUEVO] Parámetros KISS (unidades de 10 ms) + init ===
KISS_TXDELAY = int(os.getenv("KISS_TXDELAY", "30"))   # 300 ms (robusto para 1200 AFSK)
KISS_PERSIST = int(os.getenv("KISS_PERSIST", "200"))
KISS_SLOTTIME = int(os.getenv("KISS_SLOTTIME", "10")) # 100 ms
KISS_TXTAIL  = int(os.getenv("KISS_TXTAIL",  "3"))

def _kiss_param_frame(cmd_id: int, value: bytes, port: int = 0) -> bytes:
    typ = ((int(port) & 0x0F) << 4) | (cmd_id & 0x0F)
    payload = bytes([typ]) + _kiss_escape(value)
    return bytes([_FEND]) + payload + bytes([_FEND])

def _kiss_init(sock: socket.socket, port: int = KISS_CHANNEL) -> None:
    try:
        sock.sendall(_kiss_param_frame(0x01, bytes([max(0, min(255, KISS_TXDELAY))]), port=port))  # TXDELAY
        sock.sendall(_kiss_param_frame(0x02, bytes([max(1, min(255, KISS_PERSIST))]), port=port))  # PERSIST
        sock.sendall(_kiss_param_frame(0x03, bytes([max(1, min(255, KISS_SLOTTIME))]), port=port)) # SLOTTIME
        sock.sendall(_kiss_param_frame(0x04, bytes([max(0, min(255, KISS_TXTAIL))]),  port=port))  # TXTAIL
    except Exception:
        pass



# =========================
# === Broker control =======
# =========================
def _broker_send_text(ch: int, text: str, dest: str | None = None, ack: bool = False, timeout: float = 6.0) -> dict:
    """
    Cliente ligero del BacklogServer del broker: cmd SEND_TEXT.
    """
    req = {
        "cmd": "SEND_TEXT",
        "params": {
            "text": text,
            "dest": (None if (not dest or str(dest).strip().lower() == "broadcast") else str(dest).strip()),
            "ch": int(ch),
            "ack": 1 if ack else 0
        }
    }
    data = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.create_connection((BROKER_CTRL_HOST, BROKER_CTRL_PORT), timeout=timeout) as s:
        s.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk: break
            buf += chunk
    line = (buf.decode("utf-8", "ignore") or "").strip()
    try:
        return json.loads(line) if line else {"ok": False, "error": "empty broker reply"}
    except Exception as e:
        return {"ok": False, "error": f"bad json: {e}"}

# =========================
# === Helpers APRS→Mesh ===
# =========================

def _parse_ch_and_delay_from_pkt(pkt: dict, default_ch: int = MESHTASTIC_CHANNEL) -> tuple[Optional[int], Optional[int], str]:
    """
    Igual que _pick_ch_and_text, pero devolviendo también delay_min (minutos) si se usa [CH x+N].
    - 'status' y 'message': usan pkt['text']
    - resto: usan pkt['info']
    """
    if not pkt:
        return (None, None, "")

    if pkt.get("type") in {"status", "message"}:
        ch, delay_min, msg = extract_channel_and_delay(pkt.get("text", ""))
        return (ch, delay_min, msg) if (ch is not None and msg) else (None, None, "")

    info = (pkt.get("info") or "").strip()
    if not info:
        return (None, None, "")

    ch, delay_min, msg = extract_channel_and_delay(info)
    return (ch, delay_min, msg) if (ch is not None and msg) else (None, None, "")


def _schedule_aprs_to_mesh(ch: int, msg: str, delay_min: int, src: str) -> None:
    """
    Programación local en este proceso (no en el broker):
      [CH 1+10] Texto  → envía a CH1 dentro de 10 minutos vía _broker_send_text.
    Funciona sin bot y sin Internet.
    """
    delay_sec = max(0, int(delay_min) * 60)

    async def _job():
        try:
            await asyncio.sleep(delay_sec)
            if not _aprs_gate_is_enabled():
                print(f"[aprs→mesh sched] GATE OFF al ejecutar CH{ch} (+{delay_min}m) ← {src}: {msg[:120]}")
                return
            res = _broker_send_text(ch, msg, dest=None, ack=False)
            ok = bool(res.get("ok"))
            print(f"[aprs→mesh sched] CH{ch} (+{delay_min}m) ← {src}: {msg[:120]} -> {'OK' if ok else 'KO'}")
        except Exception as e:
            print(f"[aprs→mesh sched] ❌ {type(e).__name__}: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_job())
    except RuntimeError:
        # Si no hay loop (caso raro), ejecuta inmediato
        try:
            res = _broker_send_text(ch, msg, dest=None, ack=False)
            ok = bool(res.get("ok"))
            print(f"[aprs→mesh sched/now] CH{ch} (+{delay_min}m≡0) ← {src}: {msg[:120]} -> {'OK' if ok else 'KO'}")
        except Exception as e:
            print(f"[aprs→mesh sched/now] ❌ {type(e).__name__}: {e}")


def _handle_aprs_control_from_rf(src: str, msg: str) -> bool:
    """
    Comandos de control en CH0, por APRS, desde indicativo autorizado:

      [CH 0] APRS ON
      [CH 0] APRS OFF

    Actúan sobre APRS_GATE_ENABLED (gate APRS→Mesh).
    Devuelve True si ha gestionado el comando (para NO reenviar a la malla).
    """
    global APRS_GATE_ENABLED

    t = (msg or "").strip().upper()
    if not t:
        return False

    if t in ("APRS ON", "APRS GATE ON", "APRS-ON", "ON"):
        APRS_GATE_ENABLED = 1
        print(f"[aprs ctrl] {src}: APRS GATE → ON")
        return True

    if t in ("APRS OFF", "APRS GATE OFF", "APRS-OFF", "OFF"):
        APRS_GATE_ENABLED = 0
        print(f"[aprs ctrl] {src}: APRS GATE → OFF")
        return True

    return False


# =========================
# === APRS → Mesh ==========
# =========================
# === NUEVO: extractor unificado de canal+texto desde un paquete APRS ===

def _pick_ch_and_text(pkt: dict, default_ch: int = MESHTASTIC_CHANNEL) -> tuple[int, str] | None:
    """
    Devuelve (canal, texto) SOLO si existe una etiqueta [CHx] / [CANAL x].
    - 'status' y 'message': buscan etiqueta en pkt['text'].
    - Resto (posiciones/otros): buscan en pkt['info'] completa.
    Si no hay etiqueta → None (NO reinyectar).
    """
    if not pkt:
        return None

    if pkt.get("type") in {"status", "message"}:
        ch, msg = extract_channel_if_tagged(pkt.get("text", ""))
        return (ch, msg) if (ch is not None and msg) else None

    info = (pkt.get("info") or "").strip()
    if not info:
        return None

    ch, msg = extract_channel_if_tagged(info)
    return (ch, msg) if (ch is not None and msg) else None


async def task_aprs_to_meshtastic():
    """
    Escucha KISS TCP y reenvía a la malla:
      - '>' status con [CHx]
      - Mensajes dirigidos ':' con [CHx]
      - Posiciones/otros que lleven [CHx] en el comentario dentro de 'info'
    Respeta el flag APRS_GATE_ENABLED (ON/OFF).
    """
    backoff = 2.0
    while True:
        try:
            reader, writer = await asyncio.open_connection(KISS_HOST, KISS_PORT)
            buf = bytearray()
            backoff = 2.0
            while True:
                data = await reader.read(4096)
                if not data:
                    raise ConnectionError("KISS closed")
                
                buf.extend(data)

                for fr in kiss_iter_frames_from_buffer(buf):
                    pkt = parse_ax25_ui(fr)
                    if not pkt:
                        continue

                    src  = (pkt.get("src", "?") or "?").strip().upper()
                    dest = pkt.get("dest", "?")
                    path = ",".join(pkt.get("path", []))
                    typ  = pkt.get("type") or "ui"
                    preview = (pkt.get("text") or pkt.get("info") or "").replace("\n", " ")[:160]
                    print(f"[aprs] RX {typ} src={src} dest={dest} path={path} info='{preview}'")

                    # --- Filtro por indicativo de origen (si APRS_ALLOWED_SOURCES está definido) ---
                    if not _aprs_source_allowed(src):
                        _aprs_dbg(f"[aprs] drop(src not allowed) src={src}")
                        continue

                    # === Parseo con aprslib (opcional) para datos de posición (solo log) ===
                    try:
                        tnc2 = f"{src}>{dest}{(',' + path) if path else ''}:{pkt.get('info', '')}"
                        ap = aprslib.parse(tnc2)
                        if 'latitude' in ap and 'longitude' in ap:
                            lat = ap['latitude']; lon = ap['longitude']
                            course = ap.get('course'); speed = ap.get('speed')
                            alt = ap.get('altitude')
                            print(f"[aprs] POS aprslib lat={lat:.6f} lon={lon:.6f}"
                                  f"{'' if course is None else f' crs={int(course):03d}°'}"
                                  f"{'' if speed is None else f' spd={int(speed)}'}"
                                  f"{'' if alt is None else f' alt={int(alt)}'}")
                    except Exception:
                        ap = None

                    # --- Extraer canal + posible delay (+N minutos) desde [CH x] / [CANAL x+N] ---
                    ch, delay_min, msg = _parse_ch_and_delay_from_pkt(pkt, default_ch=MESHTASTIC_CHANNEL)
                    if ch is None or not msg:
                        _aprs_dbg(f"[aprs] drop(no CH) {pkt.get('type','ui')} src={pkt.get('src','?')}")
                        continue

                   # --- Si es posición APRS, convertir a enlace de mapa ---
                    try:
                        if ap and isinstance(ap, dict) and 'latitude' in ap and 'longitude' in ap and _is_position_pkt(pkt):
                            lat = ap['latitude']
                            lon = ap['longitude']
                            link = f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"

                            comment = (ap.get('comment') or '').strip()
                            if comment:
                                # Limpiamos cualquier [CH…] del comentario mostrado
                                _c_ch, _c_delay, clean_comment = extract_channel_and_delay(comment)
                                if clean_comment:
                                    comment = clean_comment

                                msg = f"{comment} {link}"
                            else:
                                msg = link
                    except Exception as _e:
                        _aprs_dbg(f"[aprs] maplink error: {type(_e).__name__}: {_e}")


                    # --- Comandos de control en CH0 (no reinyectar a Mesh) ---
                    if ch == 0:
                        if _handle_aprs_control_from_rf(src, msg):
                            # Comando gestionado (APRS ON/OFF); no se envía a la malla
                            continue
                        _aprs_dbg(f"[aprs ctrl] CH0 sin comando conocido desde {src}: {msg[:80]}")
                        continue

                    # --- Gate APRS→Mesh ON/OFF ---
                    if not _aprs_gate_is_enabled():
                        print(f"[aprs→mesh] GATE OFF: descartado CH{ch} ← {src}: {msg[:120]}")
                        continue

                    # --- Programación local con [CH x+N] ---
                    if delay_min is not None and delay_min > 0:
                        print(f"[aprs→mesh] PROGRAMADO CH{ch} (+{delay_min}m) ← {src}: {msg[:120]}")
                        _schedule_aprs_to_mesh(ch, msg, delay_min, src)
                    else:
                        # Envío inmediato al broker (como antes)
                        res = _broker_send_text(ch, msg, dest=None, ack=False)
                        ok = bool(res.get("ok"))
                        print(f"[aprs→mesh] CH{ch} ← {src}: {msg[:120]}  -> {'OK' if ok else 'KO'}")



                    # --- (OPCIONAL) Uplink APRS-IS: SOLO posiciones con [CHx], respetando NOGATE/RFONLY ---

                    if _aprsis_ready() and _is_position_pkt(pkt) and _has_ch_tag_in_info(pkt) and not _should_not_gate(pkt.get('info','')):
                        try:
                            global _aprsis_client
                            if _aprsis_client is None:
                                _aprsis_client = _AprsISClient(APRSIS_USER, APRSIS_PASSCODE, APRSIS_HOST, APRSIS_PORT, APRSIS_FILTER)
                            line = _make_thirdparty_line(pkt, APRSIS_USER)
                            if line:
                                await _aprsis_client.send_line(line)
                                print(f"[aprs→IS] UP {len(line)}B")
                        except Exception as e:
                            print(f"[aprs→IS] ❌ {e}")


        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.5)

async def task_aprsis_connect_on_startup():
    """
    Si APRS-IS está habilitado (user+passcode), intenta conectar al inicio
    y muestra el resultado por consola (OK o error). No reintenta en bucle:
    el resto del código reconectará si hace falta al primer envío real.
    """
    if not _aprsis_ready():
        return
    try:
        global _aprsis_client
        if _aprsis_client is None:
            _aprsis_client = _AprsISClient(APRSIS_USER, APRSIS_PASSCODE, APRSIS_HOST, APRSIS_PORT, APRSIS_FILTER)
        await _aprsis_client.connect()
        # Si llega aquí, ya se anunció "Conectado OK ..." desde _ensure_sync()
    except Exception as e:
        # Ya se anunció el error desde _ensure_sync(); dejamos constancia adicional si quieres:
        print(f"[aprs→IS] ❌ Conexión inicial fallida: {e}")


# =========================
# === Control UDP (bot→APRS)
# =========================

async def task_control_udp():
    """
    Escucha UDP local:
      {"mode":"aprs","dest":"EA2ABC|broadcast","text":"...","path":"WIDE1-1,WIDE2-1"}
      {"mode":"aprs_gate","enable":1|0}
      {"mode":"aprs_status"}
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
    sock.bind((CONTROL_UDP_HOST, CONTROL_UDP_PORT))
    sock.setblocking(False)
    print(f"[ctrl] UDP escuchando en {CONTROL_UDP_HOST}:{CONTROL_UDP_PORT}")

    loop = asyncio.get_running_loop()
    global APRS_GATE_ENABLED

    while True:
        try:
            data, addr = await loop.run_in_executor(None, sock.recvfrom, 65536)
        except Exception:
            await asyncio.sleep(0.05)
            continue

        try:
            obj = json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            print("[ctrl] ❌ JSON inválido")
            continue

        mode = (obj.get("mode") or obj.get("cmd") or "").strip().lower()
        if not mode:
            continue

        # --- Consulta de estado del gate ---
        if mode == "aprs_status":
            resp = {"ok": True, "aprs_gate_enabled": bool(APRS_GATE_ENABLED)}
            try: sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except Exception: pass
            print(f"[ctrl] status → gate={'ON' if APRS_GATE_ENABLED else 'OFF'}")
            continue

        # --- ON/OFF del gate ---
        if mode in ("aprs_gate", "aprs_on", "aprs_off"):
            if mode == "aprs_on":
                APRS_GATE_ENABLED = 1
            elif mode == "aprs_off":
                APRS_GATE_ENABLED = 0
            else:
                APRS_GATE_ENABLED = 1 if int(obj.get("enable", 1)) else 0
            resp = {"ok": True, "aprs_gate_enabled": bool(APRS_GATE_ENABLED)}
            try: sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except Exception: pass
            print(f"[ctrl] gate → {'ON' if APRS_GATE_ENABLED else 'OFF'} (petición UDP)")
            continue

        # --- TX APRS normal ---
        if mode != "aprs":
            continue

        dest = (obj.get("dest") or "").strip()
        text = (obj.get("text") or "").strip()
        # [NUEVO] Sanear a ASCII APRS
        text = _aprs_ascii(text)
        dest = _aprs_ascii(dest)

        path_str = (obj.get("path") or "").strip()
        path_override = [p for p in path_str.split(",") if p] if path_str else None
        if not dest or not text:
            print("[ctrl] ❌ falta dest o text")
            continue

        # DEDUP
        dest_norm = "broadcast" if dest.lower() in ("broadcast", "all") else dest.upper()
        if _dedup_seen(dest_norm, text):
            print(f"[ctrl] duplicado ignorado para dest={dest_norm}")
            continue

        if dest_norm == "broadcast":
            payloads = build_aprs_status_chunks(text)
            dest_hdr = "APRS"
        else:
            payloads = build_aprs_message_chunks(dest_norm, text)
            dest_hdr = dest_norm

        ok_all = True
        for pld in payloads:
            ok = _tx_aprs_payload(pld, dest_hdr, path_override=path_override)
            ok_all = ok_all and ok
            await asyncio.sleep(0.12)

        _dedup_mark(dest_norm, text)
        print(f"[ctrl] Resultado: {'OK' if ok_all else 'KO'} para dest={dest_norm}")


# =========================
# === Mesh → APRS (stub) ===
# =========================
# =========================
# === Mesh → APRS (stream broker)
# =========================
_APRS_CMD_RE = re.compile(r"^\s*/aprs\s+([A-Za-z0-9\-]+)\s*:\s*(.+)\s*$", re.IGNORECASE)

async def task_broker_to_aprs():
    """
    Conecta al servidor JSONL del broker (BROKER_HOST:BROKER_PORT),
    detecta /aprs broadcast: ... y /aprs EA2ABC: ... y los transmite por APRS.
    Evita duplicados con el dedup (si el bot ya envió por UDP).
    """
    backoff = 2.0
    while True:
        try:
            print(f"[broker→aprs] Conectando JSONL {BROKER_HOST}:{BROKER_PORT} …")
            reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
            print("[broker→aprs] Conectado. Esperando líneas…")
            backoff = 2.0

            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("broker closed")
                try:
                    obj = json.loads(line.decode("utf-8", "ignore"))
                except Exception:
                    continue

                # Sólo textos de usuario
                if (obj.get("portnum") or "").upper() != "TEXT_MESSAGE_APP":
                    continue

                text = obj.get("text")
                if not isinstance(text, str):
                    continue
                if not text.lstrip().lower().startswith("/aprs"):
                    continue

                m = _APRS_CMD_RE.match(text)
                if not m:
                    # Ignoramos formatos /aprs N <texto> o /aprs canal N <texto>
                    continue

                dest_token = _aprs_ascii((m.group(1) or "").strip())      # [NUEVO]
                payload_text = _aprs_ascii((m.group(2) or "").strip())    # [NUEVO]

                if not payload_text:
                    continue

                # Normaliza destino
                if dest_token.lower() in ("broadcast", "all"):
                    dest_norm = "broadcast"
                    dest_hdr = "APRS"
                    if _dedup_seen(dest_norm, payload_text):
                        # Ya lo manejó el bot por UDP
                        continue
                    payloads = build_aprs_status_chunks(payload_text, MAX_STATUS_LEN)
                else:
                    dest_norm = dest_token.upper()
                    dest_hdr = dest_norm
                    if _dedup_seen(dest_norm, payload_text):
                        continue
                    payloads = build_aprs_message_chunks(dest_norm, payload_text, MAX_MSG_LEN)

                ok_all = True
                for p in payloads:
                    ok = _tx_aprs_payload(pld := p, dest_hdr)
                    ok_all = ok_all and ok
                    # Pequeña pausa para no saturar el TNC
                    await asyncio.sleep(0.12)

                _dedup_mark(dest_norm, payload_text)
                print(f"[broker→aprs] {dest_norm} parts={len(payloads)} → {'OK' if ok_all else 'KO'}")

        except Exception as e:
            print(f"[broker→aprs] ❌ {type(e).__name__}: {e} — reintento en {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.5)


import argparse

def _apply_cli_overrides():
    global APRSIS_USER, APRSIS_PASSCODE, APRSIS_HOST, APRSIS_PORT, APRSIS_FILTER
    global APRS_GATE_ENABLED, KISS_HOST, KISS_PORT, BROKER_HOST, BROKER_PORT, MESHTASTIC_CHANNEL, KISS_CHANNEL

    p = argparse.ArgumentParser(prog="meshtastic_to_aprs.py", description="Pasarela Meshtastic ⇄ APRS")
    # --- APRS-IS ---
    p.add_argument("--aprsis-user", help="Indicativo para APRS-IS (ej. EB2XXX-10)")
    p.add_argument("--aprsis-passcode", help="Passcode APRS-IS asociado al indicativo")
    p.add_argument("--aprsis-host", help="Servidor APRS-IS (def. rotate.aprs2.net)")
    p.add_argument("--aprsis-port", type=int, help="Puerto APRS-IS (def. 14580)")
    p.add_argument("--aprsis-filter", help="Filtro opcional, p.ej. 'm/50'")

    # --- Otros útiles ---
    p.add_argument("--aprs-gate-enabled", type=int, choices=[0,1], help="1=ON 0=OFF para APRS→Mesh")
    p.add_argument("--kiss-host", help="Host KISS (def. 127.0.0.1)")
    p.add_argument("--kiss-port", type=int, help="Puerto KISS (def. 8100)")
    p.add_argument("--kiss-channel", type=int, help="Canal KISS 0..15 (def. 0)")
    p.add_argument("--broker-host", help="Host broker (def. 127.0.0.1)")
    p.add_argument("--broker-port", type=int, help="Puerto JSONL broker (def. 8765)")
    p.add_argument("--mesh-channel", type=int, help="Canal por defecto Mesh (def. 0)")

    args = p.parse_args()

    if args.aprsis_user is not None:     APRSIS_USER     = args.aprsis_user.strip()
    if args.aprsis_passcode is not None: APRSIS_PASSCODE = args.aprsis_passcode.strip()
    if args.aprsis_host is not None:     APRSIS_HOST     = args.aprsis_host.strip()
    if args.aprsis_port is not None:     APRSIS_PORT     = int(args.aprsis_port)
    if args.aprsis_filter is not None:   APRSIS_FILTER   = args.aprsis_filter.strip()

    if args.aprs_gate_enabled is not None: APRS_GATE_ENABLED = int(args.aprs_gate_enabled)
    if args.kiss_host is not None:       KISS_HOST = args.kiss_host.strip()
    if args.kiss_port is not None:       KISS_PORT = int(args.kiss_port)
    if args.kiss_channel is not None:    KISS_CHANNEL = max(0, min(15, int(args.kiss_channel)))
    if args.broker_host is not None:     BROKER_HOST = args.broker_host.strip()
    if args.broker_port is not None:     BROKER_PORT = int(args.broker_port)
    if args.mesh_channel is not None:    MESHTASTIC_CHANNEL = int(args.mesh_channel)

# =========================
# === main =================
# =========================
async def main():
    t1 = asyncio.create_task(task_broker_to_aprs())       # stub (operativo)
    t2 = asyncio.create_task(task_aprs_to_meshtastic())   # APRS -> Mesh
    t3 = asyncio.create_task(task_control_udp())          # Bot(/aprs) -> APRS
    t4 = asyncio.create_task(task_aprsis_connect_on_startup())  # <<< NUEVO
    await asyncio.gather(t1, t2, t3, t4)
   
if __name__ == "__main__":
    try:
        _apply_cli_overrides()  # <<< NUEVO

        if _aprsis_ready():
            print(f"[aprs→IS] HABILITADO: user={APRSIS_USER} host={APRSIS_HOST}:{APRSIS_PORT} filtro='{APRSIS_FILTER or '-'}'.")
            print("           Se subirán SOLO POSICIONES con etiqueta [CHx] / [CANAL x] (respeta NOGATE/RFONLY).")
        else:
            print("[aprs→IS] Deshabilitado (sin credenciales APRSIS_USER + APRSIS_PASSCODE). No se subirá nada a APRS-IS.")

        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bye")

