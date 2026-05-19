#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# v7.0.12
"""
APRS <-> BBS Bridge (APRS-IS)
--------------------------------
Objetivo:
- Permitir acceso a la BBS desde APRS-IS (APRSdroid, etc.)
- El usuario envía un mensaje APRS a TU_GATE_CALL (addressee) cuyo texto empieza por "#BBS ..."
- El bridge:
  1) (Opcional) envía ACK si el mensaje trae {msgid}
  2) pasa el texto a la BBS (bbs_server.BBSServer.handle_text)
  3) envía la respuesta en trozos <= 67 caracteres (límite de mensajes APRS)

Requisitos:
- pip install aprslib  (o apt install python3-aprslib en Debian si aplica)
- Tu bbs_server.py accesible (mismo directorio o PYTHONPATH)
- Variables de entorno (mínimo):
    APRS_IS_HOST, APRS_IS_PORT, APRS_IS_CALLSIGN, APRS_IS_PASSCODE
    APRS_BBS_GATE_CALLSIGN (normalmente igual a APRS_IS_CALLSIGN)
    BBS_ENABLED=1 y DB/KEYs de tu BBS como ya uses en producción

Notas:
- Este módulo NO modifica tu broker ni tu BBS: se acopla por fuera (cambio mínimo y reversible).
"""

import os
import re
import time
import threading
from typing import List, Optional, Tuple

try:
    import aprslib
except Exception as e:
    raise SystemExit("Falta aprslib. Instala con: pip install aprslib") from e

# Importa tu BBS actual
try:
    from bbs_server import BBSServer
except Exception as e:
    raise SystemExit(f"No se pudo importar bbs_server.BBSServer: {e}") from e


# -----------------------------
# Config / helpers
# -----------------------------

_CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(-[0-9]{1,2})?$")

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

def _now() -> float:
    return time.time()

def _clean_aprs_text(s: str) -> str:
    """
    APRS101: en mensajes, evitar caracteres '|' '~' '{' dentro del cuerpo.
    También es buena idea eliminar CR/LF.
    """
    s = (s or "").replace("\r", " ").replace("\n", " ").strip()
    # caracteres problemáticos en cuerpo de mensaje APRS
    s = s.replace("|", "/").replace("~", "-").replace("{", "(").replace("}", ")")
    return s

def _split_67(text: str, max_len: int = 67) -> List[str]:
    """
    Trocea preservando palabras cuando se pueda, sin superar max_len.
    """
    t = _clean_aprs_text(text)
    if not t:
        return []

    out: List[str] = []
    cur = t
    while len(cur) > max_len:
        cut = cur.rfind(" ", 0, max_len + 1)
        if cut < 20:  # si no hay espacio razonable, corta duro
            cut = max_len
        out.append(cur[:cut].strip())
        cur = cur[cut:].strip()
    if cur:
        out.append(cur)
    return out

def _parse_msgid(text: str) -> Tuple[str, Optional[str]]:
    """
    Si el mensaje termina con {ID (1..5 chars alfanum) devuelve (body, id).
    """
    if not text:
        return "", None
    m = re.search(r"\{([A-Za-z0-9]{1,5})\s*$", text.strip())
    if not m:
        return text.strip(), None
    msgid = m.group(1)
    body = text.strip()[: m.start()].rstrip()
    return body, msgid


# -----------------------------
# Bridge
# -----------------------------

class AprsBbsBridge:
    """
    Bridge APRS-IS que transforma mensajes APRS en comandos BBS y devuelve respuestas.
    """

    def __init__(self) -> None:
        # APRS-IS
        self.aprs_host = os.getenv("APRS_IS_HOST", "rotate.aprs2.net").strip()
        self.aprs_port = _env_int("APRS_IS_PORT", 14580)
        self.aprs_call = os.getenv("APRS_IS_CALLSIGN", "").strip().upper()
        self.aprs_pass = os.getenv("APRS_IS_PASSCODE", "").strip()

        # A qué addressee llegan los comandos BBS por APRS
        self.gate_call = os.getenv("APRS_BBS_GATE_CALLSIGN", self.aprs_call).strip().upper()

        # Límite de envío por respuesta (para no inundar APRS)
        self.max_chunks = _env_int("APRS_BBS_MAX_CHUNKS", 6)

        # Canal “virtual” para pasar el filtro de la BBS
        # (tu BBS filtra por ch == bbs_channel)
        self.bbs_channel = _env_int("BBS_CHANNEL", 0)

        # Dedup simple por (from, body) con TTL
        self.dedup_ttl = _env_int("APRS_BBS_DEDUP_TTL", 60)
        self._dedup = {}  # (from, body) -> ts

        if not self.aprs_call or not self.aprs_pass:
            raise SystemExit("Faltan APRS_IS_CALLSIGN / APRS_IS_PASSCODE")

        # Instancia BBS (usa tu config actual por env / defaults internos)
        self.bbs = BBSServer()

        # APRS-IS client
        self.is_conn = aprslib.IS(self.aprs_call, self.aprs_pass, host=self.aprs_host, port=self.aprs_port)

        # Lock para envíos simultáneos
        self._tx_lock = threading.Lock()

    def _dedup_ok(self, from_call: str, body: str) -> bool:
        now = _now()
        # purge
        for k, ts in list(self._dedup.items()):
            if (now - float(ts)) > self.dedup_ttl:
                self._dedup.pop(k, None)
        key = (from_call, body)
        if key in self._dedup:
            return False
        self._dedup[key] = now
        return True

    def _send_aprs_message(self, to_call: str, text: str) -> None:
        """
        Envía un mensaje APRS dirigido.
        aprslib: formato de mensaje dirigido se maneja con 'sendall' usando raw frame.
        """
        to_call = (to_call or "").strip().upper()
        if not to_call:
            return

        # El addressee en APRS ocupa 9 chars (relleno con espacios si se construye raw).
        # aprslib tiene helper: aprslib.util.message()
        msg = aprslib.util.message(to_call, _clean_aprs_text(text))

        with self._tx_lock:
            self.is_conn.sendall(msg)

    def _send_ack(self, to_call: str, msgid: str) -> None:
        if not msgid:
            return
        # ACK estándar: "ackNNN" como cuerpo del mensaje dirigido
        self._send_aprs_message(to_call, f"ack{msgid}")

    def _handle_packet(self, packet: dict) -> None:
        """
        Callback de aprslib: procesa paquetes parseados.
        Nos interesan los de tipo 'message'.
        """
        try:
            if not packet:
                return
            if packet.get("format") != "message":
                return

            from_call = (packet.get("from") or "").strip().upper()
            to_call = (packet.get("addressee") or "").strip().upper()
            text = (packet.get("message_text") or "").strip()

            if not from_call or not _CALL_RE.match(from_call):
                return

            # Solo comandos BBS dirigidos a nuestra pasarela
            if to_call != self.gate_call:
                return

            body, msgid = _parse_msgid(text)

            # ACK temprano para cortar retries del cliente
            if msgid:
                self._send_ack(from_call, msgid)

            # Solo tratamos mensajes que empiecen por #BBS
            if not body or not body.upper().startswith("#BBS"):
                return

            if not self._dedup_ok(from_call, body):
                return

            # Llamada a tu BBS (pasa el filtro por canal)
            chunks = self.bbs.handle_text(from_id=from_call, ch=self.bbs_channel, text=body)
            if not chunks:
                return

            # La BBS devuelve chunks para Meshtastic; aquí re-troceamos a 67
            reply_text = "\n".join(chunks)
            out = _split_67(reply_text, 67)

            # Cap de seguridad anti-inundación
            out = out[: max(1, self.max_chunks)]

            for part in out:
                self._send_aprs_message(from_call, part)
                time.sleep(1.0)  # pacing básico para APRS-IS

        except Exception:
            # No propagamos errores: operación 24/7
            return

    def run_forever(self) -> None:
        """
        Conecta a APRS-IS y queda escuchando.
        """
        # Filtro APRS-IS: queremos solo mensajes dirigidos a nuestra pasarela (m/TOCALL)
        # Nota: el filtro exacto puede variar por servidor; aprslib lo envía en login.
        # Aun así, validamos en _handle_packet.
        aprs_filter = f"t/m"

        self.is_conn.connect(blocking=True, port=self.aprs_port, host=self.aprs_host, filter=aprs_filter)
        self.is_conn.consumer(self._handle_packet, raw=False)


def main() -> None:
    bridge = AprsBbsBridge()
    bridge.run_forever()


if __name__ == "__main__":
    main()
