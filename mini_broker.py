#!/usr/bin/env python3 	   	 	 	    	   		 
# -*- coding: utf-8 -*- 	  	   	 	 	     	 	
""" 	  		 	 					 	  		 
mini_broker.py V6.1.1	 		  	 	 			  		 		 
--------------	    	   			 		    	 
Listener JSONL ultraligero para Raspberry Pi 2B.			 	   		  	 	 			 	

- Se conecta al nodo Meshtastic por TCP (típicamente 127.0.0.1:4403 cuando hay USB + socat).		 		    	 				  	 	 
- Recibe eventos y los expone en un servidor TCP JSONL en :8765 para el bot y el APRS.					 			 		   		 		 
- No implementa backlog, control ni tareas; sólo "chorrea" eventos en tiempo real.

Variables de entorno (con valores por defecto):
  MESHTASTIC_HOST=127.0.0.1
  MESHTASTIC_PORT=4403
  JSONL_HOST=0.0.0.0
  JSONL_PORT=8765
  MESHTASTIC_SERIAL=/dev/ttyUSB0  (opcional; si existe y no hay servidor TCP, puedes lanzar socat aparte)
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
import time
import queue
import signal
from typing import Any, Dict, List, Optional

# Meshtastic (usa la interfaz TCP standard)
try:
    from meshtastic import tcp_interface
except Exception as e:
    print(f"[mini] ERROR: falta el paquete 'meshtastic' ({e}). Instálalo con: pip install meshtastic", file=sys.stderr)
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# Configuración por entorno
MESH_HOST   = os.getenv("MESHTASTIC_HOST", "127.0.0.1")
MESH_PORT   = int(os.getenv("MESHTASTIC_PORT", "4403"))
JSONL_HOST  = os.getenv("JSONL_HOST", "0.0.0.0")
JSONL_PORT  = int(os.getenv("JSONL_PORT", "8765"))

# Opcional: si quieres que este script lance socat tú mismo, podrías añadirlo aquí.
# Recomendado: lanzar socat fuera (systemd o tu run_mesh_usb.sh) para mantenerlo aún más simple.
SERIAL_DEV  = os.getenv("MESHTASTIC_SERIAL", "/dev/ttyUSB0")

# ──────────────────────────────────────────────────────────────────────────────
# Estado global
_evt_q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=2000)
_clients_lock = threading.Lock()
_clients: List[socket.socket] = []
_stop_event = threading.Event()

# ──────────────────────────────────────────────────────────────────────────────
def _flat(pkt: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aplana el paquete Meshtastic a un dict simple para el bot/APRS.
    Sólo los campos esenciales que tu bot ya maneja.
    """
    # pkt suele traer 'decoded' con info de alto nivel
    decoded = pkt.get("decoded") or {}
    d: Dict[str, Any] = {
        "portnum": decoded.get("portnum") or pkt.get("portnum"),
        "portnum_name": decoded.get("portnum_name") or decoded.get("app") or pkt.get("app"),
        "text": decoded.get("text") or pkt.get("text") or "",
        "channel": decoded.get("channel") or decoded.get("channelIndex") or pkt.get("channel") or pkt.get("channelIndex"),
        "from": pkt.get("fromId") or pkt.get("fromIdShort") or pkt.get("from"),
        "to": pkt.get("toId") or pkt.get("toIdShort") or pkt.get("to"),
    }
    # Métricas si existen
    for k_src, k_dst in (("rxSnr", "rx_snr"), ("snr", "rx_snr"), ("rssi", "rx_rssi")):
        v = pkt.get(k_src, decoded.get(k_src))
        if v is not None:
            d[k_dst] = v
    return d

def _serialize(pkt: Dict[str, Any]) -> bytes:
    try:
        return (json.dumps(_flat(pkt), ensure_ascii=False) + "\n").encode("utf-8", "ignore")
    except Exception:
        # En última instancia, emitimos crudo si algo no se pudo aplanar
        try:
            return (json.dumps(pkt, ensure_ascii=False) + "\n").encode("utf-8", "ignore")
        except Exception:
            return b"{}\n"

# ──────────────────────────────────────────────────────────────────────────────
class _ClientHandler(socketserver.BaseRequestHandler):
    def handle(self):
        with _clients_lock:
            _clients.append(self.request)
        try:
            # Mantiene la conexión viva; lectura mínima (no esperamos nada del cliente)
            while not _stop_event.is_set():
                data = self.request.recv(1)
                if not data:
                    break
        except Exception:
            pass
        finally:
            with _clients_lock:
                try:
                    _clients.remove(self.request)
                except ValueError:
                    pass
            try:
                self.request.close()
            except Exception:
                pass

class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def broadcaster() -> None:
    """Envía cada evento a todos los clientes conectados (JSONL)."""
    while not _stop_event.is_set():
        try:
            pkt = _evt_q.get(timeout=0.5)
        except queue.Empty:
            continue
        line = _serialize(pkt)
        dead: List[socket.socket] = []
        with _clients_lock:
            for c in _clients:
                try:
                    c.sendall(line)
                except Exception:
                    dead.append(c)
            for c in dead:
                try:
                    _clients.remove(c)
                except ValueError:
                    pass
                try:
                    c.close()
                except Exception:
                    pass

def iface_reader() -> None:
    """
    Lee del nodo mediante TCPInterface (que en USB estará expuesto por socat a 127.0.0.1:4403).
    Reintenta automáticamente si se cae la conexión.
    """
    backoff = 1.5
    while not _stop_event.is_set():
        try:
            print(f"[mini] Conectando a nodo Meshtastic {MESH_HOST}:{MESH_PORT} …")
            iface = tcp_interface.TCPInterface(hostname=MESH_HOST, portnum=MESH_PORT)

            # Registrar callback de recepción (compat según versión del lib)
            def on_receive(pkt: Dict[str, Any], *_):
                try:
                    _evt_q.put_nowait(pkt)
                except queue.Full:
                    # Si se llena, descartamos silenciosamente (Pi 2B preferimos soltar a bloquear)
                    pass

            try:
                # algunas versiones exponen atributo
                iface.onReceive = on_receive  # type: ignore[attr-defined]
            except Exception:
                try:
                    # otras exponen setter
                    iface.setOnReceive(on_receive)  # type: ignore[attr-defined]
                except Exception as e:
                    print(f"[mini] No se pudo registrar callback de recepción: {e}", file=sys.stderr)

            # Mantener viva la conexión
            backoff = 1.5
            while not _stop_event.is_set():
                time.sleep(0.5)

        except Exception as e:
            print(f"[mini] Conexión al nodo caída: {e}. Reintentando…", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 1.8, 30.0)

# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"[mini] Nodo: {MESH_HOST}:{MESH_PORT}  →  JSONL: {JSONL_HOST}:{JSONL_PORT}")
    # Servidor JSONL
    srv = _ThreadedTCPServer((JSONL_HOST, JSONL_PORT), _ClientHandler)

    # Hilos
    t_b = threading.Thread(target=broadcaster, name="broadcaster", daemon=True)
    t_r = threading.Thread(target=iface_reader, name="iface_reader", daemon=True)
    t_b.start(); t_r.start()

    # Señales de salida limpia
    def _stop(*_):
        _stop_event.set()
        try:
            srv.shutdown()
        except Exception:
            pass
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        srv.serve_forever()
    finally:
        _stop_event.set()
        try:
            srv.server_close()
        except Exception:
            pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
