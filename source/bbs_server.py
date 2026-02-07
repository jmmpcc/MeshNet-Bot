# bbs_server.py v6.2.4
# Motor BBS para MeshNet v6.2.1 (broker-side), desacoplado del transporte.
# El broker llama a handle_text(from_id, ch, text) y envía los chunks devueltos.
#
# Consolidado:
# 1) MAS consistente + coste real de rate-limit
# 2) Rate-limit coherente para TODAS las respuestas (no solo LEER/MAS)
# 3) “DB cifrada” (boletines y privados) con migración transparente (prefijo ENC:)
# 4) TTL + limpieza de sesiones/caches (24/7)
# 5) Moderación: admins, cuotas diarias, tamaños máximos, control de publicación
# 6) send_func utilizable (modo standalone)
# 7) Paginación en listados: NEWS/LISTA/LEIDOS/BUSCAR/BANDEJA
# 8) VER/CUERPO para reducir tramas
#
# Ajustes finales solicitados:
# A) _apply_limits_small_reply respeta BBS_MAX_CHUNKS_PER_REPLY y usa cola + MAS si excede.
# B) Limpieza inmediata (pending_out / pending_subject / last_search) en login fallido.

from __future__ import annotations

import os
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Callable, Tuple

from cryptography.fernet import Fernet


# =========================
# Helpers / config
# =========================

def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

def _env_csv_ints(name: str, default_csv: str) -> set:
    """Lee una lista CSV de enteros desde una variable de entorno.
    - Ej.: "5,7,9" -> {5,7,9}
    - Ignora elementos vacíos o no numéricos.
    """
    raw = (os.getenv(name) or default_csv or "").strip()
    out = set()
    for part in raw.split(","):
        p = (part or "").strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            continue
    return out

def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return (v if v is not None else default).strip()


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _today_ymd() -> str:
    return datetime.utcnow().strftime("%Y%m%d")


def _norm_callsign(s: str) -> str:
    return (s or "").strip().upper()


def _norm_node_id(v) -> str:
    """Normaliza node_id para usarlo como clave estable de sesión (prefijo "!" y minúsculas)."""
    s = str(v).strip() if v is not None else ""
    if not s:
        return ""
    if not s.startswith("!"):
        s = "!" + s
    return s.lower()


def _ms() -> int:
    return int(time.time() * 1000)


def _split_chunks(text: str, max_len: int) -> List[str]:
    """
    Troceo por líneas manteniendo saltos, evitando fragmentar demasiado.
    """
    text = (text or "").strip()
    if not text:
        return []

    lines = text.splitlines()
    out: List[str] = []
    buf = ""

    def flush():
        nonlocal buf
        if buf.strip():
            out.append(buf.rstrip())
        buf = ""

    for ln in lines:
        ln = ln.rstrip()
        while len(ln) > max_len:
            part, ln = ln[:max_len], ln[max_len:]
            if buf:
                flush()
            out.append(part)

        if not buf:
            buf = ln
        elif len(buf) + 1 + len(ln) <= max_len:
            buf += "\n" + ln
        else:
            flush()
            buf = ln

    flush()
    return out




# =========================
# BBS Core
# =========================

@dataclass
class Session:
    state: str                # idle | wait_login | wait_pass | authed
    bbs_callsign: str
    pending_login: Optional[str] = None
    authed_user: Optional[str] = None
    last_activity_ts: float = 0.0


class BbsServer:
    """
    Motor BBS.
    - Broker: llama a handle_text(from_id, ch, text) y envía los chunks retornados.
    - Standalone: puede usar send_chunks(dest, chunks) (usa send_func inyectada).
    """

    ENC_PREFIX = "ENC:"  # marca de contenido cifrado

    def __init__(
        self,
        send_func: Callable[[str, int, str], None],  # send_func(dest_node_id, ch, text)
        *,
        enabled: bool,
        bbs_callsign: str,
        bbs_channel: int,
        db_path: str,
        key_path: str,
        max_tx: int = 234,

        # Límites históricos (techo por comando)
        list_limit: int = 6,         # NEWS
        all_list_limit: int = 10,    # LISTA
        read_list_limit: int = 10,   # LEIDOS
        search_limit: int = 10,      # BUSCAR (resultados devueltos)
        inbox_limit: int = 6,        # BANDEJA
        poll_list_limit: int = 3,    # ENCUESTAS (últimas N)
    ):
        self.enabled = bool(enabled)
        self.send_func = send_func
        self.bbs_callsign = _norm_callsign(bbs_callsign)
        self.bbs_channel = int(bbs_channel)

        self.db_path = db_path
        self.key_path = key_path

        # Control de carga LoRa
        self.max_tx = int(max_tx)
        self.page_chars = _env_int("BBS_READ_PAGE_CHARS", 160)
        self.max_chunks_per_reply = _env_int("BBS_MAX_CHUNKS_PER_REPLY", 3)
        self.rate_tokens_per_min = _env_int("BBS_RATE_TOKENS_PER_MIN", 12)
        self.rate_burst = _env_int("BBS_RATE_BURST", 6)
        self.min_cmd_interval_ms = _env_int("BBS_MIN_CMD_INTERVAL_MS", 700)

        # TTL / limpieza
        self.session_ttl_sec = _env_int("BBS_SESSION_TTL_SEC", 900)  # 15 min
        self.cache_ttl_sec = _env_int("BBS_CACHE_TTL_SEC", 1800)     # 30 min

        # Moderación / límites
        self.allow_public_posts = _env_bool("BBS_ALLOW_PUBLIC_POSTS", "1")
        self.admin_callsigns = set(
            _norm_callsign(x) for x in _env_str("BBS_ADMIN_CALLSIGNS", "").split(",") if x.strip()
        )
        self.max_subject_chars = _env_int("BBS_MAX_SUBJECT_CHARS", 80)
        self.max_post_chars = _env_int("BBS_MAX_POST_CHARS", 1200)
        self.max_mp_chars = _env_int("BBS_MAX_MP_CHARS", 240)
        self.max_posts_per_day = _env_int("BBS_MAX_POSTS_PER_DAY", 3)
        self.max_polls_per_day = _env_int("BBS_MAX_POLLS_PER_DAY", 2)

        # Paginación listados
        self.list_page_size = _env_int("BBS_LIST_PAGE_SIZE", 6)

        # Techos por comando
        self.list_limit = int(list_limit)
        self.all_list_limit = int(all_list_limit)
        self.read_list_limit = int(read_list_limit)
        self.search_limit = int(search_limit)
        self.inbox_limit = int(inbox_limit)
        self.poll_list_limit = int(poll_list_limit)

        # BUSCAR: escaneo sobre últimos N para evitar DoS CPU (descifrado)
        self.search_scan_limit = _env_int("BBS_SEARCH_SCAN_LIMIT", 200)
        self.search_cooldown_sec = float(_env_int("BBS_SEARCH_COOLDOWN_SEC", 8))

        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(self.key_path) or ".", exist_ok=True)

        self._fernet = self._load_fernet()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

        self._db_init()
        


        # Sesiones y caches
        self._sess: Dict[str, Session] = {}

        # Pendientes de publicación por sesión (from_id)
        self._pending_subject_by_from: Dict[str, str] = {}

        # Control de sobrecarga LoRa / caches
        self._pending_out: Dict[str, List[str]] = {}      # resto de chunks por from_id
        self._last_cmd_ms: Dict[str, int] = {}            # anti-spam
        self._rate: Dict[str, Dict[str, float]] = {}      # token bucket
        self._last_seen_ts: Dict[str, float] = {}         # from_id -> last activity
        self._last_search_ts: Dict[str, float] = {}       # from_id -> last search time

    # --------------------------
    # Crypto (contenido cifrado)
    # --------------------------

    def _load_fernet(self) -> Fernet:
        if not os.path.exists(self.key_path):
            key = Fernet.generate_key()
            with open(self.key_path, "wb") as f:
                f.write(key)
        else:
            with open(self.key_path, "rb") as f:
                key = f.read()
        return Fernet(key)

    def _enc(self, s: str) -> str:
        token = self._fernet.encrypt((s or "").encode("utf-8")).decode("utf-8")
        return self.ENC_PREFIX + token

    def _dec(self, s: str) -> str:
        if not s:
            return ""
        if not s.startswith(self.ENC_PREFIX):
            return s  # migración transparente
        token = s[len(self.ENC_PREFIX):]
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return "[CONTENIDO CIFRADO NO LEGIBLE]"

    # --------------------------
    # DB init + cuotas
    # --------------------------

    def _db_init(self) -> None:
        c = self._conn.cursor()

        c.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
          id TEXT PRIMARY KEY,
          alias TEXT,
          password_enc TEXT,
          ultima_conexion TEXT
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS boletines (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          autor TEXT,
          asunto TEXT,
          cuerpo TEXT,
          timestamp TEXT
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS leidos (
          user_id TEXT,
          boletin_id INTEGER,
          PRIMARY KEY(user_id, boletin_id)
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS privados (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          de TEXT,
          para TEXT,
          cuerpo TEXT,
          timestamp TEXT,
          leido INTEGER DEFAULT 0
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS encuestas (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          pregunta TEXT,
          opciones_json TEXT,
          autor TEXT,
          timestamp TEXT
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS votos (
          encuesta_id INTEGER,
          user_id TEXT,
          opcion INTEGER,
          PRIMARY KEY(encuesta_id, user_id)
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS acciones_diarias (
          ymd TEXT,
          user_id TEXT,
          posts INTEGER DEFAULT 0,
          polls INTEGER DEFAULT 0,
          PRIMARY KEY(ymd, user_id)
        )""")

        # --------------------------
        # NEWS (ingesta automática)
        # --------------------------
        c.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            url TEXT NOT NULL,
            published_at TEXT,
            tags TEXT,
            lang TEXT,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(url),
            UNIQUE(content_hash)
        );
        """)
        
        c.execute("CREATE INDEX IF NOT EXISTS idx_news_created_at ON news(created_at);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_news_published_at ON news(published_at);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_news_tags ON news(tags);")


        self._conn.commit()

    def _is_admin(self, callsign: str) -> bool:
        cs = _norm_callsign(callsign)
        return (cs in self.admin_callsigns) if self.admin_callsigns else False

    def _quota_get(self, user_id: str) -> Tuple[int, int]:
        cur = self._conn.cursor()
        cur.execute("SELECT posts, polls FROM acciones_diarias WHERE ymd=? AND user_id=?",
                    (_today_ymd(), user_id))
        row = cur.fetchone()
        if not row:
            return 0, 0
        return int(row[0] or 0), int(row[1] or 0)

    def _quota_inc_posts(self, user_id: str) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            INSERT INTO acciones_diarias (ymd, user_id, posts, polls)
            VALUES (?,?,1,0)
            ON CONFLICT(ymd, user_id) DO UPDATE SET posts=posts+1
        """, (_today_ymd(), user_id))
        self._conn.commit()

    def _quota_inc_polls(self, user_id: str) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            INSERT INTO acciones_diarias (ymd, user_id, posts, polls)
            VALUES (?,?,0,1)
            ON CONFLICT(ymd, user_id) DO UPDATE SET polls=polls+1
        """, (_today_ymd(), user_id))
        self._conn.commit()

    # --------------------------
    # Control LoRa: anti-spam + rate
    # --------------------------

    def _touch_from(self, from_id: str) -> None:
        now = time.time()
        self._last_seen_ts[from_id] = now
        if from_id in self._sess:
            self._sess[from_id].last_activity_ts = now

    def _allow_cmd(self, from_id: str) -> bool:
        now = _ms()
        last = self._last_cmd_ms.get(from_id, 0)
        if (now - last) < self.min_cmd_interval_ms:
            return False
        self._last_cmd_ms[from_id] = now
        return True

    def _rate_allow(self, from_id: str, cost: int) -> bool:
        """
        Token bucket por from_id.
        Cost = número estimado de tramas a enviar para esta respuesta.
        """
        cost = max(1, int(cost))
        now = time.time()
        st = self._rate.get(from_id)
        if not st:
            st = {"t": float(self.rate_burst), "ts": now}
            self._rate[from_id] = st

        elapsed = max(0.0, now - float(st["ts"]))
        refill = (self.rate_tokens_per_min / 60.0) * elapsed
        st["t"] = min(float(self.rate_burst), float(st["t"]) + refill)
        st["ts"] = now

        if float(st["t"]) >= float(cost):
            st["t"] -= float(cost)
            return True
        return False

    def _clear_pending_out(self, from_id: str) -> None:
        self._pending_out.pop(from_id, None)

    def _estimate_cost(self, chunks_total: int, max_chunks: int, add_more_hint: bool) -> int:
        send_now = min(max_chunks, chunks_total)
        has_rest = chunks_total > max_chunks
        return send_now + (1 if (has_rest and add_more_hint) else 0)

    def _enqueue_reply(self, from_id: str, chunks: List[str], max_now: int, add_more_hint: bool) -> List[str]:
        """
        Envía como máximo max_now chunks y guarda el resto para #BBS MAS.
        """
        if not chunks:
            self._pending_out.pop(from_id, None)
            return []

        now_send = chunks[:max_now]
        rest = chunks[max_now:]

        if rest:
            self._pending_out[from_id] = rest
            if add_more_hint:
                cs = self.bbs_callsign
                now_send.append(
                    "Más: #BBS MAS | Menú: #BBS MENU (DM) | "
                    f"Canal: #BBS {cs} MAS / #BBS {cs} MENU"
                )
        else:
            self._pending_out.pop(from_id, None)

        return now_send

    def _apply_limits_small_reply(self, from_id: str, text: str) -> List[str]:
        """
        Para respuestas cortas (menús, listados, confirmaciones):
        - split a max_tx
        - rate-limit por coste real (incluyendo línea "Más" si aplica)
        - respeta BBS_MAX_CHUNKS_PER_REPLY y usa cola + MAS si excede
        """
        chunks = _split_chunks(text, self.max_tx)
        if not chunks:
            self._pending_out.pop(from_id, None)
            return []

        add_more_hint = len(chunks) > self.max_chunks_per_reply
        cost = self._estimate_cost(len(chunks), self.max_chunks_per_reply, add_more_hint=add_more_hint)

        if not self._rate_allow(from_id, cost=cost):
            self._pending_out.pop(from_id, None)
            return _split_chunks("Límite de envío alcanzado. Reintenta en unos segundos.", self.max_tx)

        # Reemplaza cualquier pending anterior (si lo hubiera)
        return self._enqueue_reply(
            from_id,
            chunks,
            max_now=self.max_chunks_per_reply,
            add_more_hint=add_more_hint
        )

    # --------------------------
    # Limpieza (TTL) robusta
    # --------------------------

    def _gc(self) -> None:
        now = time.time()

        dead_sessions = [fid for fid, s in self._sess.items()
                         if (now - float(s.last_activity_ts or 0.0)) > float(self.session_ttl_sec)]
        for fid in dead_sessions:
            self._sess.pop(fid, None)
            self._pending_subject_by_from.pop(fid, None)
            self._pending_out.pop(fid, None)
            self._last_search_ts.pop(fid, None)

        def prune_map(m: Dict, ttl: int) -> None:
            dead = []
            for fid in list(m.keys()):
                last = float(self._last_seen_ts.get(fid, 0.0))
                if (now - last) > float(ttl):
                    dead.append(fid)
            for fid in dead:
                m.pop(fid, None)

        prune_map(self._last_cmd_ms, self.cache_ttl_sec)
        prune_map(self._rate, self.cache_ttl_sec)
        prune_map(self._pending_out, self.cache_ttl_sec)
        prune_map(self._pending_subject_by_from, self.cache_ttl_sec)
        prune_map(self._last_search_ts, self.cache_ttl_sec)

        dead_seen = [fid for fid, ts in self._last_seen_ts.items() if (now - float(ts)) > float(self.cache_ttl_sec)]
        for fid in dead_seen:
            self._last_seen_ts.pop(fid, None)

    # --------------------------
    # Menu / UX
    # --------------------------

    def _menu(self) -> str:
        """
        Menú principal de la BBS.

        Regla de diseño (canal público / multi-BBS):
            #BBS <BBS_CALLSIGN> <COMANDO> [parámetros]

        Regla de diseño (DM al nodo BBS):
            #BBS <COMANDO> [parámetros]
        (y para iniciar sesión basta con enviar: "#BBS")

        Ejemplos:
            Canal: #BBS EB2EAS-5 MENU
            DM:    #BBS MENU
        """
        cs = self.bbs_callsign

        return (
            f"BBS {cs}\n"
            "\n"
            "Sintaxis:\n"
            f"  Canal (multi-BBS): #BBS {cs} <COMANDO>\n"
            "  DM (al nodo BBS):   #BBS <COMANDO>\n"
            "  Inicio por DM:      #BBS\n"
            "\n"
            f"MENU: #BBS {cs} MENU  (canal) | #BBS MENU (DM)\n"
            "\n"
            "Boletines (usuarios):\n"
            f"  Canal: #BBS {cs} NEWS [p] | #BBS {cs} LISTA [p]\n"
            "  DM:    #BBS NEWS [p] | #BBS LISTA [p]\n"
            f"  Canal: #BBS {cs} LEER <id> | #BBS {cs} VER <id> | #BBS {cs} CUERPO <id>\n"
            "  DM:    #BBS LEER <id> | #BBS VER <id> | #BBS CUERPO <id>\n"
            f"  Canal: #BBS {cs} LEIDOS [p]\n"
            "  DM:    #BBS LEIDOS [p]\n"
            "\n"
            "Noticias automáticas:\n"
            f"  Canal: #BBS {cs} NOTICIAS [p]\n"
            "  DM:    #BBS NOTICIAS [p]\n"
            f"  Canal: #BBS {cs} NOTICIAS CAT\n"
            "  DM:    #BBS NOTICIAS CAT\n"
            f"  Canal: #BBS {cs} NOTICIAS CAT <categoria> [p]\n"
            "  DM:    #BBS NOTICIAS CAT <categoria> [p]\n"
            f"  Canal: #BBS {cs} NOTICIAS VER <id>\n"
            "  DM:    #BBS NOTICIAS VER <id>\n"
            "\n"
            "Publicar:\n"
            f"  Canal: #BBS {cs} NUEVA\n"
            "  DM:    #BBS NUEVA\n"
            f"  Canal: #BBS {cs} ASUNTO <texto>\n"
            "  DM:    #BBS ASUNTO <texto>\n"
            f"  Canal: #BBS {cs} TEXTO <cuerpo>\n"
            "  DM:    #BBS TEXTO <cuerpo>\n"
            "\n"
            "Buscar:\n"
            f"  Canal: #BBS {cs} BUSCAR <palabra/frase> [p]\n"
            "  DM:    #BBS BUSCAR <palabra/frase> [p]\n"
            "\n"
            "Privados:\n"
            f"  Canal: #BBS {cs} MP DEST:Mensaje | #BBS {cs} BANDEJA [p]\n"
            "  DM:    #BBS MP DEST:Mensaje | #BBS BANDEJA [p]\n"
            "\n"
            "Encuestas:\n"
            f"  Canal: #BBS {cs} ENCUESTA Pregunta?|Op1|Op2|...\n"
            "  DM:    #BBS ENCUESTA Pregunta?|Op1|Op2|...\n"
            f"  Canal: #BBS {cs} ENCUESTAS | #BBS {cs} VOTO <id> <op>\n"
            "  DM:    #BBS ENCUESTAS | #BBS VOTO <id> <op>\n"
            "\n"
            "Resultados:\n"
            f"  Canal: #BBS {cs} RESULT <id>\n"
            "  DM:    #BBS RESULT <id>\n"
            "\n"
            "Estadísticas:\n"
            f"  Canal: #BBS {cs} ESTADISTICAS\n"
            "  DM:    #BBS ESTADISTICAS\n"
            "\n"
            "Salir:\n"
            f"  Canal: #BBS {cs} SALIR\n"
            "  DM:    #BBS SALIR"
        )



    def send_chunks(self, dest_node_id: str, chunks: List[str], ch: Optional[int] = None) -> None:
        """
        Standalone: envía directamente usando send_func inyectado.
        El broker normalmente NO usa esto (usa SENDQ).
        """
        if not chunks:
            return
        channel = int(self.bbs_channel if ch is None else ch)
        for t in chunks:
            for c in _split_chunks(t, self.max_tx):
                self.send_func(dest_node_id, channel, c)

    # --------------------------
    # Users
    # --------------------------

    def _user_exists(self, callsign: str) -> bool:
        cs = _norm_callsign(callsign)
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM usuarios WHERE id=?", (cs,))
        return cur.fetchone() is not None

    def _user_check_password(self, callsign: str, password: str) -> bool:
        cs = _norm_callsign(callsign)
        cur = self._conn.cursor()
        cur.execute("SELECT password_enc FROM usuarios WHERE id=?", (cs,))
        row = cur.fetchone()
        if not row or not row[0]:
            return False
        try:
            return self._dec(row[0]) == password
        except Exception:
            return False

    def _user_create(self, callsign: str, password: str) -> None:
        cs = _norm_callsign(callsign)
        alias = cs
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO usuarios (id, alias, password_enc, ultima_conexion) VALUES (?,?,?,?)",
            (cs, alias, self._enc(password), _now_iso())
        )
        self._conn.commit()

    def _user_touch(self, callsign: str) -> None:
        cs = _norm_callsign(callsign)
        cur = self._conn.cursor()
        cur.execute("UPDATE usuarios SET ultima_conexion=? WHERE id=?", (_now_iso(), cs))
        self._conn.commit()

    # --------------------------
    # Boletines (cuerpo cifrado)
    # --------------------------

    def _page_size_for(self, limit_ceiling: int) -> int:
        return max(1, min(self.list_page_size, int(limit_ceiling)))

    def _bbs_news_unread(self, user_id: str, page: int) -> str:
        page = max(1, int(page))
        limit = self._page_size_for(self.list_limit)
        offset = (page - 1) * limit

        cur = self._conn.cursor()
        cur.execute("""
            SELECT id, asunto, autor, timestamp
            FROM boletines
            WHERE id NOT IN (SELECT boletin_id FROM leidos WHERE user_id=?)
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """, (user_id, limit, offset))
        rows = cur.fetchall()

        if not rows:
            return "No hay boletines nuevos."

        out = [f"Boletines no leídos (pág {page}):"]
        for bid, asunto, autor, ts in rows:
            d = (ts or "").split("T")[0]
            out.append(f"[{bid}] {asunto} ({autor}) {d}")
        out.append(f"Leer: #BBS LEER <id> | Vista: #BBS VER <id> | Sig: #BBS NEWS {page + 1}")
        return "\n".join(out)

    def _bbs_list_recent(self, page: int) -> str:
        page = max(1, int(page))
        limit = self._page_size_for(self.all_list_limit)
        offset = (page - 1) * limit

        cur = self._conn.cursor()
        cur.execute("""
            SELECT id, asunto, autor, timestamp
            FROM boletines
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        rows = cur.fetchall()

        if not rows:
            return "No hay boletines publicados."

        out = [f"Boletines recientes (pág {page}):"]
        for bid, asunto, autor, ts in rows:
            d = (ts or "").split("T")[0]
            out.append(f"[{bid}] {asunto} ({autor}) {d}")
        out.append(f"Leer: #BBS LEER <id> | Vista: #BBS VER <id> | Sig: #BBS LISTA {page + 1}")
        return "\n".join(out)

    def _bbs_list_read(self, user_id: str, page: int) -> str:
        page = max(1, int(page))
        limit = self._page_size_for(self.read_list_limit)
        offset = (page - 1) * limit

        cur = self._conn.cursor()
        cur.execute("""
            SELECT b.id, b.asunto, b.autor, b.timestamp
            FROM leidos l
            JOIN boletines b ON b.id = l.boletin_id
            WHERE l.user_id=?
            ORDER BY b.timestamp DESC
            LIMIT ? OFFSET ?
        """, (user_id, limit, offset))
        rows = cur.fetchall()

        if not rows:
            return "No tienes boletines leídos registrados."

        out = [f"Boletines leídos (pág {page}):"]
        for bid, asunto, autor, ts in rows:
            d = (ts or "").split("T")[0]
            out.append(f"[{bid}] {asunto} ({autor}) {d}")
        out.append(f"Leer: #BBS LEER <id> | Vista: #BBS VER <id> | Sig: #BBS LEIDOS {page + 1}")
        return "\n".join(out)

    def _bbs_read_full(self, user_id: str, bid: int) -> Tuple[str, str]:
        cur = self._conn.cursor()
        cur.execute("SELECT id, asunto, cuerpo, autor, timestamp FROM boletines WHERE id=?", (bid,))
        row = cur.fetchone()
        if not row:
            return "Boletín no encontrado.", ""

        cur.execute("INSERT OR IGNORE INTO leidos (user_id, boletin_id) VALUES (?,?)", (user_id, bid))
        self._conn.commit()

        _, asunto, cuerpo_raw, autor, ts = row
        d = (ts or "").split("T")[0]
        cuerpo = self._dec(cuerpo_raw or "")
        header = f"[{bid}] {asunto}\nPor: {autor} {d}"
        return header, cuerpo

    def _bbs_view(self, user_id: str, bid: int, preview_chars: int = 180) -> str:
        header, body = self._bbs_read_full(user_id, bid)
        if header.startswith("Boletín no encontrado"):
            return header
        body = (body or "").strip()
        prev = body[:preview_chars]
        if len(body) > preview_chars:
            prev += "..."
        return f"{header}\n\n{prev}"



    def _bbs_subject_set(self, from_id: str, subject: str) -> str:
        subject = (subject or "").strip()
        if not subject:
            return "Asunto vacío."
        if len(subject) > self.max_subject_chars:
            subject = subject[:self.max_subject_chars].rstrip()
        self._pending_subject_by_from[from_id] = subject
        return f"Asunto guardado. Envía el texto con: #BBS TEXTO <cuerpo>"

    def _bbs_post(self, from_id: str, user_id: str, body: str) -> str:
        body = (body or "").strip()
        if not body:
            return "Texto vacío."
        if len(body) > self.max_post_chars:
            return f"Texto demasiado largo ({len(body)}). Máximo: {self.max_post_chars}"

        subject = self._pending_subject_by_from.get(from_id)
        if not subject:
            return f"Primero define el asunto con: #BBS ASUNTO <texto>"

        if (not self.allow_public_posts) and (not self._is_admin(user_id)):
            return "Publicación deshabilitada para usuarios. Solo admins."

        posts, _ = self._quota_get(user_id)
        if (not self._is_admin(user_id)) and posts >= self.max_posts_per_day:
            return f"Límite diario de boletines alcanzado ({self.max_posts_per_day})."

        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO boletines (autor, asunto, cuerpo, timestamp) VALUES (?,?,?,?)",
            (user_id, subject, self._enc(body), _now_iso())
        )
        self._conn.commit()

        if not self._is_admin(user_id):
            self._quota_inc_posts(user_id)

        self._pending_subject_by_from.pop(from_id, None)
        return f"Boletín publicado: {subject}"

    # --------------------------
    # Privados (cuerpo cifrado)
    # --------------------------

    def _bbs_mp_send(self, sender: str, spec: str) -> str:
        if ":" not in spec:
            return f"Formato: #BBS MP DEST:Mensaje"
        dest, msg = spec.split(":", 1)
        dest = _norm_callsign(dest)
        msg = (msg or "").strip()
        if not dest or not msg:
            return f"Formato: #BBS MP DEST:Mensaje"
        if len(msg) > self.max_mp_chars:
            return f"Mensaje demasiado largo ({len(msg)}). Máximo: {self.max_mp_chars}"

        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO privados (de, para, cuerpo, timestamp, leido) VALUES (?,?,?,?,0)",
            (sender, dest, self._enc(msg), _now_iso())
        )
        self._conn.commit()
        return f"MP enviado a {dest}"

    def _bbs_inbox(self, user_id: str, page: int) -> str:
        page = max(1, int(page))
        limit = self._page_size_for(self.inbox_limit)
        offset = (page - 1) * limit

        cur = self._conn.cursor()
        cur.execute("""
            SELECT id, de, cuerpo, timestamp, leido
            FROM privados
            WHERE para=?
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """, (user_id, limit, offset))
        rows = cur.fetchall()

        if not rows:
            return "Bandeja vacía."

        out = [f"Bandeja (pág {page}):"]
        for mid, de, cuerpo_raw, ts, leido in rows:
            d = (ts or "").split("T")[0]
            cuerpo = self._dec(cuerpo_raw or "")
            flag = "NEW" if int(leido or 0) == 0 else "OK"
            prev = (cuerpo or "").strip().replace("\n", " ")
            if len(prev) > 60:
                prev = prev[:60] + "..."
            out.append(f"[{mid}] {flag} De:{de} {d} :: {prev}")

        for mid, _, _, _, leido in rows:
            if int(leido or 0) == 0:
                cur.execute("UPDATE privados SET leido=1 WHERE id=?", (mid,))
        self._conn.commit()

        out.append(f"Sig: #BBS BANDEJA {page + 1}")
        return "\n".join(out)

    # --------------------------
    # Encuestas + cuotas
    # --------------------------

    def _poll_create(self, author: str, spec: str) -> str:
        parts = [p.strip() for p in (spec or "").split("|") if p.strip()]
        if len(parts) < 3:
            return f"Formato: #BBS  ENCUESTA Pregunta?|Op1|Op2|..."
        pregunta = parts[0]
        opciones = parts[1:]

        _, polls = self._quota_get(author)
        if (not self._is_admin(author)) and polls >= self.max_polls_per_day:
            return f"Límite diario de encuestas alcanzado ({self.max_polls_per_day})."

        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO encuestas (pregunta, opciones_json, autor, timestamp) VALUES (?,?,?,?)",
            (pregunta, json.dumps(opciones, ensure_ascii=False), author, _now_iso())
        )
        self._conn.commit()

        if not self._is_admin(author):
            self._quota_inc_polls(author)

        return f"Encuesta creada. Ver con: #BBS  ENCUESTAS"

    def _poll_list(self) -> str:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, pregunta, opciones_json, autor, timestamp FROM encuestas ORDER BY timestamp DESC LIMIT ?",
            (self.poll_list_limit,)
        )
        rows = cur.fetchall()
        if not rows:
            return "No hay encuestas."
        out: List[str] = []
        for pid, pregunta, opciones_json, autor, ts in rows:
            opciones = json.loads(opciones_json) if opciones_json else []
            d = (ts or "").split("T")[0]
            block = [f"[{pid}] {pregunta} ({autor}) {d}"]
            for i, op in enumerate(opciones, start=1):
                block.append(f" {i}. {op}")
            block.append(f"Vota: #BBS VOTO {pid} <opción> | Resultados: #BBS RESULT {pid}")
            out.append("\n".join(block))
        return "\n\n".join(out)

    def _poll_vote(self, user_id: str, pid: int, option: int) -> str:
        cur = self._conn.cursor()
        cur.execute("SELECT opciones_json FROM encuestas WHERE id=?", (pid,))
        row = cur.fetchone()
        if not row:
            return "Encuesta no encontrada."
        opciones = json.loads(row[0]) if row[0] else []
        if option < 1 or option > len(opciones):
            return "Opción inválida."
        cur.execute("REPLACE INTO votos (encuesta_id, user_id, opcion) VALUES (?,?,?)", (pid, user_id, option))
        self._conn.commit()
        return "Voto registrado."

    def _poll_result(self, pid: int) -> str:
        cur = self._conn.cursor()
        cur.execute("SELECT pregunta, opciones_json FROM encuestas WHERE id=?", (pid,))
        row = cur.fetchone()
        if not row:
            return "Encuesta no encontrada."
        pregunta, opciones_json = row
        opciones = json.loads(opciones_json) if opciones_json else []

        cur.execute("SELECT opcion, COUNT(*) FROM votos WHERE encuesta_id=? GROUP BY opcion ORDER BY opcion", (pid,))
        counts = {op: cnt for op, cnt in cur.fetchall()}
        total = sum(counts.values())

        out = [f"Resultados [{pid}] {pregunta}", f"Votos: {total}"]
        for i, op in enumerate(opciones, start=1):
            out.append(f" {i}. {op} = {counts.get(i, 0)}")
        return "\n".join(out)

    # --------------------------
    # BUSCAR (asunto LIKE + scan cuerpo cifrado)
    # --------------------------

    def _bbs_search(self, query: str, page: int) -> str:
        q = (query or "").strip()
        if not q:
            return f"Formato: #BBS BUSCAR <palabra/frase> [p]"
        page = max(1, int(page))
        like = f"%{q}%"

        cur = self._conn.cursor()

        cur.execute("""
            SELECT id, asunto, autor, timestamp
            FROM boletines
            WHERE asunto LIKE ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (like, self.search_scan_limit))
        subj_rows = cur.fetchall()

        cur.execute("""
            SELECT id, asunto, autor, timestamp, cuerpo
            FROM boletines
            ORDER BY timestamp DESC
            LIMIT ?
        """, (self.search_scan_limit,))
        scan_rows = cur.fetchall()

        hits: Dict[int, Tuple[int, str, str, str]] = {}
        for bid, asunto, autor, ts in subj_rows:
            hits[int(bid)] = (int(bid), asunto, autor, ts)

        q_low = q.lower()
        for bid, asunto, autor, ts, cuerpo_raw in scan_rows:
            ibid = int(bid)
            if ibid in hits:
                continue
            cuerpo = self._dec(cuerpo_raw or "")
            if q_low in (cuerpo or "").lower():
                hits[ibid] = (ibid, asunto, autor, ts)

        if not hits:
            return f"Sin resultados para: {q}"

        ordered = sorted(hits.values(), key=lambda x: (x[3] or ""), reverse=True)

        per = max(1, int(self.search_limit))
        start = (page - 1) * per
        end = start + per
        page_items = ordered[start:end]

        if not page_items:
            return f"Sin resultados para: {q} (pág {page})"

        out = [f"Resultados para: {q} (pág {page})"]
        for bid, asunto, autor, ts in page_items:
            d = (ts or "").split("T")[0]
            out.append(f"[{bid}] {asunto} ({autor}) {d}")
        out.append(f"Leer: #BBS LEER <id> | Vista: #BBS VER <id> | Sig: #BBS BUSCAR {q} {page + 1}")
        return "\n".join(out)

    # --------------------------
    # Stats
    # --------------------------

    def _stats(self) -> str:
        cur = self._conn.cursor()
        users = cur.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        posts = cur.execute("SELECT COUNT(*) FROM boletines").fetchone()[0]
        mps = cur.execute("SELECT COUNT(*) FROM privados").fetchone()[0]
        polls = cur.execute("SELECT COUNT(*) FROM encuestas").fetchone()[0]
        return (
            f"ESTADÍSTICAS BBS {self.bbs_callsign}\n"
            f"Usuarios: {users}\n"
            f"Boletines: {posts}\n"
            f"Mensajes privados: {mps}\n"
            f"Encuestas: {polls}"
        )

    # --------------------------
    # Parsing helpers
    # --------------------------

    @staticmethod
    def _parse_page_arg(tokens: List[str], default_page: int = 1) -> int:
        if len(tokens) >= 2:
            try:
                return max(1, int(tokens[-1]))
            except Exception:
                return default_page
        return default_page

    
    @staticmethod
    def _parse_login_pass_chain(cmdline: str) -> Tuple[Optional[str], Optional[str], str]:
        """Parsea una cadena compacta del tipo:

            LOGIN <CALLSIGN> PASS <PASSWORD> [RESTO...]

        Devuelve:
        - callsign (str) o None
        - password (str) o None
        - resto (str) (puede ser "")

        Notas:
        - El password se interpreta como UN token (sin espacios).
        - "RESTO" conserva el resto de la línea tras el password.
        - Si la cadena no encaja, devuelve (None, None, "").
        """
        s = (cmdline or "").strip()
        if not s:
            return None, None, ""

        parts = s.split()
        if len(parts) < 4:
            return None, None, ""

        if parts[0].upper() != "LOGIN":
            return None, None, ""

        # Buscar el primer PASS (para permitir que el callsign tenga guiones, etc.)
        pass_idx = None
        for i in range(2, len(parts)):
            if parts[i].upper() == "PASS":
                pass_idx = i
                break
        if pass_idx is None:
            return None, None, ""

        login = " ".join(parts[1:pass_idx]).strip()
        if not login:
            return None, None, ""

        if pass_idx + 1 >= len(parts):
            return None, None, ""
        pwd = parts[pass_idx + 1].strip()
        if not pwd:
            return None, None, ""

        rest = " ".join(parts[pass_idx + 2:]).strip() if (pass_idx + 2) < len(parts) else ""
        return _norm_callsign(login), pwd, rest


    @staticmethod
    def _parse_pass_with_rest(cmdline: str) -> Tuple[Optional[str], str]:
        """Parsea:

            PASS <PASSWORD> [RESTO...]

        Devuelve (password o None, resto). El password es un token.
        """
        s = (cmdline or "").strip()
        if not s:
            return None, ""

        parts = s.split(maxsplit=2)
        if len(parts) < 2:
            return None, ""
        if parts[0].upper() != "PASS":
            return None, ""

        pwd = (parts[1] or "").strip()
        rest = (parts[2] if len(parts) >= 3 else "").strip()
        if not pwd:
            return None, ""
        return pwd, rest

    # NEWS: helpers
    # --------------------------

    @staticmethod
    def _news_split_tags(tags_raw: str) -> List[str]:
        """
        Acepta tags tipo: "tech security" o "tech,security" o mixto.
        Normaliza a lista única en minúsculas.
        """
        if not tags_raw:
            return []
        raw = tags_raw.replace(",", " ").strip().lower()
        parts = [p.strip() for p in raw.split() if p.strip()]
        # únicos preservando orden
        seen = set()
        out = []
        for p in parts:
            if p not in seen:
                out.append(p)
                seen.add(p)
        return out

    def _news_categories(self, page: int = 1) -> str:
        """
        Devuelve categorías (tags) disponibles en NOTICIAS con conteo y paginación.

        - Normaliza tags separadas por espacios o comas (usa _news_split_tags()).
        - Paginación para no saturar el canal.

        Uso:
          #BBS <BBS_ID> NOTICIAS CAT            -> página 1
          #BBS <BBS_ID> NOTICIAS CAT 2          -> página 2
        """
        page = max(1, int(page))
        per = 20  # tamaño página categorías (ajustable si se quiere exponer por env)

        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT tags FROM news WHERE tags IS NOT NULL AND TRIM(tags) != ''"
        ).fetchall()

        counts: Dict[str, int] = {}
        for (tags_raw,) in rows:
            for t in self._news_split_tags(tags_raw or ""):
                counts[t] = counts.get(t, 0) + 1

        if not counts:
            return "NOTICIAS: sin categorías (aún no hay noticias)."

        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

        total = len(ordered)
        max_page = max(1, (total + per - 1) // per)
        page = min(page, max_page)

        start = (page - 1) * per
        chunk = ordered[start:start + per]

        cs = self.bbs_callsign

        out = [f"Categorías NOTICIAS ({page}/{max_page}) — total: {total}"]
        for tag, n in chunk:
            out.append(f"• {tag} ({n})")

        if page < max_page:
            out.append(f"Más categorías: #BBS {cs} NOTICIAS CAT {page + 1}")
        else:
            out.append("Fin de categorías.")

        out.append(f"Filtrar: #BBS {cs} NOTICIAS CAT <categoria> [p]")
        return "\n".join(out)

    def _news_list(self, *, tag: Optional[str] = None, page: int = 1) -> str:
        """
        Lista noticias recientes, opcionalmente filtradas por tag.
        """
        per = max(1, int(self.list_page_size))
        offset = (max(1, page) - 1) * per

        cur = self._conn.cursor()
        if tag:
            tag_n = (tag or "").strip().lower()
            # match flexible en string de tags
            rows = cur.execute(
                """
                SELECT id, title, source, published_at, url, tags
                FROM news
                WHERE LOWER(tags) LIKE ?
                ORDER BY COALESCE(published_at, created_at) DESC
                LIMIT ? OFFSET ?
                """,
                (f"%{tag_n}%", per, offset),
            ).fetchall()
        else:
            rows = cur.execute(
                """
                SELECT id, title, source, published_at, url, tags
                FROM news
                ORDER BY COALESCE(published_at, created_at) DESC
                LIMIT ? OFFSET ?
                """,
                (per, offset),
            ).fetchall()

        if not rows:
            if tag:
                return f"NOTICIAS: sin resultados para categoría: {tag} (pág {page})"
            return f"NOTICIAS: sin noticias (pág {page})"

        hdr = f"NOTICIAS (pág {page})" + (f" — categoría: {tag}" if tag else "")
        out = [hdr]
        for nid, title, source, published_at, url, tags in rows:
            d = (published_at or "").split("T")[0] if published_at else ""
            t0 = (title or "").strip()
            out.append(f"[{nid}] {t0} ({source}) {d}".rstrip())
        out.append(f"Más: #BBS NOTICIAS" + (f" CAT {tag} {page+1}" if tag else f" {page+1}"))
        return "\n".join(out)

    def _news_read(self, nid: int) -> str:
        """
        Lee una noticia por id.
        """
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT title, summary, source, published_at, url, tags FROM news WHERE id=?",
            (int(nid),),
        ).fetchone()

        if not row:
            return f"NEWS: id no encontrado: {nid}"

        title, summary, source, published_at, url, tags = row
        d = (published_at or "").split("T")[0] if published_at else ""
        out = [
            f"NEWS [{nid}] {title}",
            f"Fuente: {source}",
            (f"Fecha: {d}" if d else "").strip(),
            (f"Tags: {tags}" if tags else "").strip(),
            "",
            (summary or "").strip(),
            "",
            f"URL: {url}",
        ]
        return "\n".join([x for x in out if x != ""])


    # =========================
    # Public entry
    # =========================

    def handle_text(self, from_id: str, ch: int, text: str) -> Optional[List[str]]:
        """
        Devuelve lista de chunks o None si no aplica.
        """
        if not self.enabled:
            return None
        # Normalizar el node_id del remitente para que la sesión (LOGIN/PASS/MAS) sea consistente.
        from_id = _norm_node_id(from_id)
        if not from_id:
            return None
        # Permitir entrada por varios canales públicos y por DM sin romper compatibilidad:
        # - BBS_CHANNEL: canal principal (histórico)
        # - BBS_CHANNELS: lista CSV opcional (p.ej. "5,7,9")
        # - BBS_DM_CHANNEL: canal DM (por defecto 0)
        dm_ch = _env_int("BBS_DM_CHANNEL", 0)
        allowed = _env_csv_ints("BBS_CHANNELS", str(self.bbs_channel))
        allowed.add(int(dm_ch))
        if int(ch) not in allowed:
            return None
       

        if not text or not text.strip().upper().startswith("#BBS"):
            return None

        self._gc()
        self._touch_from(from_id)

        after = text.strip()[4:].strip()

        # --- NUEVO: soportar "#BBS <BBS_CALLSIGN> <COMANDO> ..." ---
        # Si el primer token tras #BBS es el callsign de esta BBS, lo retiramos para interpretar el comando.
        # Mantiene compatibilidad con el formato corto "#BBS LOGIN ..." durante la sesión.
        tokens = after.split()
        addressed = False
        after_cmd = after

        if tokens and _norm_callsign(tokens[0]) == self.bbs_callsign:
            addressed = True
            after_cmd = " ".join(tokens[1:]).strip()

        # --- NUEVO: bootstrap por DM ---
        # - DM (BBS_DM_CHANNEL): permitir "#BBS" sin callsign para iniciar sesión.
        # - Canal público: "#BBS" sin callsign solo muestra guía (multi-BBS).
        is_dm = (int(ch) == int(dm_ch))
        wants_dm_bootstrap = bool(is_dm and (not after))

        if not after and not is_dm:
            return self._apply_limits_small_reply(
                from_id,
                (
                    "Para conectarte por canal (multi-BBS), todos los comandos deben ir precedidos por el indicativo de la BBS.\n"
                    f"Formato: #BBS {self.bbs_callsign} <COMANDO>\n"
                    f"Ejemplo: #BBS {self.bbs_callsign} LOGIN TU_INDICATIVO\n"
                    "\n"
                    "Si puedes abrir DM al nodo BBS, inicia con: #BBS"
                )
            )

        # A partir de aquí, el "comando real" a interpretar es after_cmd (si venía direccionado),
        # o after (si venía en formato corto)
        after_to_parse = after_cmd if addressed else after

        # Si es DM y solo llega "#BBS", lo tratamos como inicio de sesión
        # equivalente a "conectar" a esta BBS sin exigir callsign.
        if wants_dm_bootstrap:
            # Forzar estado idle->wait_login si aún no lo está
            s = self._sess.get(from_id)
            if not s:
                s = Session(state="idle", bbs_callsign=self.bbs_callsign, last_activity_ts=time.time())
                self._sess[from_id] = s
            s.last_activity_ts = time.time()

            if s.state == "idle":
                self._clear_pending_out(from_id)
                self._pending_subject_by_from.pop(from_id, None)
                self._last_search_ts.pop(from_id, None)
                s.state = "wait_login"

            return self._apply_limits_small_reply(
                from_id,
                (
                    "Conexión iniciada.\n"
                    "Envía tu indicativo:\n"
                    "#BBS LOGIN TU_INDICATIVO"
                )
            )


        if not self._allow_cmd(from_id):
            return self._apply_limits_small_reply(from_id, "Demasiado rápido. Reintenta.")

        s = self._sess.get(from_id)
        if not s:
            s = Session(state="idle", bbs_callsign=self.bbs_callsign, last_activity_ts=time.time())
            self._sess[from_id] = s
        s.last_activity_ts = time.time()

        # Conectar a esta BBS (multi-BBS)
        if s.state == "idle":
            # En idle, aceptamos conexión si:
            # - formato corto: "#BBS <CALLSIGN>"  → conecta y pide LOGIN
            # - formato direccionado: "#BBS <CALLSIGN> <COMANDO...>" → conecta y ejecuta el comando en el mismo mensaje
            #
            # Compatibilidad:
            # En DM permitimos "#BBS <COMANDO>" sin necesidad de "#BBS <CALLSIGN>" ni "#BBS" previo.
            # En canal público, si no viene direccionado, seguimos exigiendo el callsign de la BBS.
            if (not is_dm) and (not addressed) and (not wants_dm_bootstrap) and (_norm_callsign(after) != self.bbs_callsign):
                return None


            self._clear_pending_out(from_id)
            self._pending_subject_by_from.pop(from_id, None)
            self._last_search_ts.pop(from_id, None)
            s.state = "wait_login"

            # Si el mensaje ya trae comando (p.ej. "#BBS EB2EAS-5 LOGIN EB2EAS"),
            # continuamos el parseo sin responder todavía, para que LOGIN/PASS funcionen en un solo paso.
            if addressed and after_cmd:
                pass
            else:
                return self._apply_limits_small_reply(
                    from_id,
                    (
                        "Conexión iniciada.\n"
                        + (
                            "Envía tu indicativo:\n#BBS LOGIN TU_INDICATIVO"
                            if is_dm else
                            f"Envía tu indicativo:\n#BBS {self.bbs_callsign} LOGIN TU_INDICATIVO"
                        )
                    )
                )


        up = after_to_parse.strip()
        up_u = up.upper()

        # --- Login ---
        # ============================================================
        # Login / PASS compactos (una sola trama)
        # ============================================================
        # Soporta, sin romper el modo clásico paso-a-paso:
        #   #BBS <BBS> LOGIN <CALLSIGN> PASS <PWD> [COMANDO...]
        # Ejemplo:
        #   #BBS EB2EAS-5 LOGIN EB2-XXXX PASS 1234 NEWS
        #
        # Reglas:
        # - El password es un solo token (sin espacios).
        # - Si hay comando tras PASS, se ejecuta ya autenticado.
        # - Límite de pasos por mensaje: 3 (evita bucles).
        up = after_to_parse.strip()
        for _step in range(3):
            up = (up or "").strip()
            up_u = up.upper()

            # --- Login ---
            if s.state == "wait_login":
                login_hint = "#BBS LOGIN TU_INDICATIVO" if is_dm else f"#BBS {self.bbs_callsign} LOGIN TU_INDICATIVO"


                # Modo compacto: LOGIN ... PASS ... [RESTO]
                login_cs, login_pwd, rest = self._parse_login_pass_chain(up)
                if login_cs and login_pwd:
                    # Limpieza coherente (igual que en el flujo clásico)
                    self._clear_pending_out(from_id)
                    self._pending_subject_by_from.pop(from_id, None)
                    self._last_search_ts.pop(from_id, None)

                    # Autenticar/crear directamente
                    cs = _norm_callsign(login_cs)
                    pwd = (login_pwd or "").strip()
                    if not cs or not pwd:
                        return self._apply_limits_small_reply(
                            from_id,
                            (
                                "Envía tu indicativo usando el formato obligatorio:\n"  + login_hint
                                
                            )
                        )

                    if self._user_exists(cs):
                        just_created = False
                        if not self._user_check_password(cs, pwd):
                            # limpieza inmediata en fallo
                            self._sess.pop(from_id, None)
                            self._clear_pending_out(from_id)
                            self._pending_subject_by_from.pop(from_id, None)
                            self._last_search_ts.pop(from_id, None)
                            return self._apply_limits_small_reply(from_id, f"Contraseña incorrecta. Conecta de nuevo: #BBS {self.bbs_callsign}")
                        self._user_touch(cs)
                    else:
                        self._user_create(cs, pwd)
                        just_created = True

                    s.pending_login = None
                    s.authed_user = cs
                    s.state = "authed"

                    # Si hay comando restante, ejecutarlo ya autenticado
                    if rest:
                        up = rest
                        continue

                    # Si no hay comando restante, mantener el comportamiento original (menú)
                    if just_created:
                        return self._apply_limits_small_reply(from_id, f"Usuario registrado: {cs}\n\n{self._menu()}")
                    return self._apply_limits_small_reply(from_id, f"Conectado como {cs}\n\n{self._menu()}")

                # Modo clásico: LOGIN <CALLSIGN>
                if up_u.startswith("PASS"):
                    return self._apply_limits_small_reply(
                        from_id,
                        "Primero envía tu indicativo:\n" + (
                            "#BBS LOGIN TU_INDICATIVO" if is_dm else f"#BBS {self.bbs_callsign} LOGIN TU_INDICATIVO"
                        )
                    )


                if not up_u.startswith("LOGIN"):
                    return self._apply_limits_small_reply(
                        from_id,
                        (
                            "Envía tu indicativo usando el formato obligatorio:\n" + login_hint

                           
                        )
                    )

                parts = up.split(maxsplit=1)
                if len(parts) != 2:
                    return self._apply_limits_small_reply(
                        from_id,
                        (
                            "Envía tu indicativo usando el formato obligatorio:\n" + login_hint

                        )
                    )

                cs = _norm_callsign(parts[1])
                if not cs:
                    return self._apply_limits_small_reply(
                        from_id,
                        (
                            "Envía tu indicativo usando el formato obligatorio:\n" + login_hint

                        )
                    )

                self._clear_pending_out(from_id)
                self._pending_subject_by_from.pop(from_id, None)
                self._last_search_ts.pop(from_id, None)

                s.pending_login = cs
                s.state = "wait_pass"
                return self._apply_limits_small_reply(from_id, ("Ahora la contraseña: #BBS PASS <TU_CONTRASEÑA>" if is_dm else f"Ahora la contraseña: #BBS {self.bbs_callsign} PASS <TU_CONTRASEÑA>"))

            # --- PASS ---
            if s.state == "wait_pass":
                # Permitir reiniciar LOGIN aunque estemos esperando PASS.
                # Evita quedarse "atrapado" si el usuario reenvía LOGIN por error.
                if up_u.startswith("LOGIN"):
                    parts = up.split(maxsplit=1)
                    if len(parts) == 2:
                        cs2 = _norm_callsign(parts[1])
                        if cs2:
                            s.pending_login = cs2
                            s.state = "wait_pass"
                            return self._apply_limits_small_reply(from_id, ("Ahora la contraseña: #BBS PASS <TU_CONTRASEÑA>" if is_dm else f"Ahora la contraseña: #BBS {self.bbs_callsign} PASS <TU_CONTRASEÑA>"))
                    return self._apply_limits_small_reply(
                        from_id,
                        "Envía tu indicativo usando el formato obligatorio:\n" + (
                            "#BBS LOGIN TU_INDICATIVO" if is_dm else f"#BBS {self.bbs_callsign} LOGIN TU_INDICATIVO"
                        )
                    )


                pwd, rest = self._parse_pass_with_rest(up)
                if not pwd:
                    return self._apply_limits_small_reply(from_id, ("Envía la contraseña: #BBS PASS <TU_CONTRASEÑA>" if is_dm else f"Envía la contraseña: #BBS {self.bbs_callsign} PASS <TU_CONTRASEÑA>"))

                cs = _norm_callsign(s.pending_login or "")
                if not cs:
                    self._sess.pop(from_id, None)
                    self._clear_pending_out(from_id)
                    self._pending_subject_by_from.pop(from_id, None)
                    self._last_search_ts.pop(from_id, None)

                    return self._apply_limits_small_reply(from_id, f"Sesión reiniciada. Conecta: #BBS LOGIN TU_INDICATIVO")

                self._clear_pending_out(from_id)
                self._pending_subject_by_from.pop(from_id, None)
                self._last_search_ts.pop(from_id, None)

                if self._user_exists(cs):
                    if not self._user_check_password(cs, pwd):
                        # limpieza inmediata en fallo
                        self._sess.pop(from_id, None)
                        self._clear_pending_out(from_id)
                        self._pending_subject_by_from.pop(from_id, None)
                        self._last_search_ts.pop(from_id, None)
                        return self._apply_limits_small_reply(from_id, f"Contraseña incorrecta. Conecta de nuevo: #BBS {self.bbs_callsign}")
                    self._user_touch(cs)
                    just_created = False
                else:
                    self._user_create(cs, pwd)
                    just_created = True

                s.pending_login = None
                s.authed_user = cs
                s.state = "authed"

                if rest:
                    up = rest
                    continue

                if just_created:
                    return self._apply_limits_small_reply(from_id, f"Usuario registrado: {cs}\n\n{self._menu()}")
                return self._apply_limits_small_reply(from_id, f"Conectado como {cs}\n\n{self._menu()}")

            # --- Sesión ---
            if s.state != "authed" or not s.authed_user:
                self._sess.pop(from_id, None)
                self._clear_pending_out(from_id)
                self._pending_subject_by_from.pop(from_id, None)
                self._last_search_ts.pop(from_id, None)
                return self._apply_limits_small_reply(from_id, f"Sesión inválida. Conecta: #BBS LOGIN TU_INDICATIVO")

            # Si llegamos aquí, estamos autenticados: salimos del loop y pasamos al dispatcher normal.
            break


        user = s.authed_user

        # ---------- MAS ----------
        if up_u == "MAS":
            pend = self._pending_out.get(from_id, [])
            if not pend:
                return self._apply_limits_small_reply(from_id, "No hay más contenido pendiente.")

            cost = self._estimate_cost(len(pend), self.max_chunks_per_reply, add_more_hint=True)
            if not self._rate_allow(from_id, cost=cost):
                return self._apply_limits_small_reply(from_id, "Límite de envío alcanzado. Reintenta en unos segundos.")

            return self._enqueue_reply(
                from_id,
                pend,
                max_now=self.max_chunks_per_reply,
                add_more_hint=True
            )

        # En cualquier comando distinto de MAS, limpiar restos pendientes
        self._clear_pending_out(from_id)

        # ---------- Comandos ----------
        if up_u == "MENU":
            return self._apply_limits_small_reply(from_id, self._menu())

        if up_u == "SALIR":
            self._sess.pop(from_id, None)
            self._clear_pending_out(from_id)
            self._pending_subject_by_from.pop(from_id, None)
            self._last_search_ts.pop(from_id, None)
            return self._apply_limits_small_reply(from_id, "Sesión cerrada.")

        if up_u == "NUEVA":
            if (not self.allow_public_posts) and (not self._is_admin(user)):
                return self._apply_limits_small_reply(from_id, "Publicación deshabilitada para usuarios. Solo admins.")
            posts, _ = self._quota_get(user)
            if (not self._is_admin(user)) and posts >= self.max_posts_per_day:
                return self._apply_limits_small_reply(from_id, f"Límite diario de boletines alcanzado ({self.max_posts_per_day}).")
            return self._apply_limits_small_reply(from_id, f"Publicación: #BBS ASUNTO <texto>  y luego  #BBS TEXTO <cuerpo>")

        if up_u.startswith("ASUNTO"):
            parts = up.split(maxsplit=1)
            if len(parts) != 2:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS ASUNTO <texto>")
            return self._apply_limits_small_reply(from_id, self._bbs_subject_set(from_id, parts[1]))

        if up_u.startswith("TEXTO"):
            parts = up.split(maxsplit=1)
            if len(parts) != 2:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS TEXTO <cuerpo>")
            return self._apply_limits_small_reply(from_id, self._bbs_post(from_id, user, parts[1]))

        # ---------- NOTICIAS (RSS / automáticas) ----------
        if up_u.startswith("NOTICIAS"):
            tokens = up.split()

            if len(tokens) >= 2 and tokens[1].upper() == "CAT":
                # Formatos:
                # 1) NOTICIAS CAT                 -> lista categorías (p1)
                # 2) NOTICIAS CAT <p>             -> lista categorías (p)
                # 3) NOTICIAS CAT <tag> [p]       -> lista noticias filtradas por tag

                if len(tokens) == 2:
                    return self._apply_limits_small_reply(from_id, self._news_categories(page=1))

                # Si el tercer token es numérico -> es página de categorías
                try:
                    cat_page = int(tokens[2])
                    return self._apply_limits_small_reply(from_id, self._news_categories(page=max(1, cat_page)))
                except Exception:
                    pass

                # Si no es número -> es tag
                tag = tokens[2]
                page = 1
                if len(tokens) >= 4:
                    try:
                        page = max(1, int(tokens[3]))
                    except Exception:
                        pass
                return self._apply_limits_small_reply(from_id, self._news_list(tag=tag, page=page))


            if len(tokens) >= 2 and tokens[1].upper() == "VER":
                if len(tokens) < 3:
                    return self._apply_limits_small_reply(from_id, f"Uso: #BBS NOTICIAS VER <id>")
                try:
                    nid = int(tokens[2])
                except Exception:
                    return self._apply_limits_small_reply(from_id, "ID inválido.")
                return self._apply_limits_small_reply(from_id, self._news_read(nid))

            # NOTICIAS [pág]
            page = 1
            if len(tokens) >= 2:
                try:
                    page = int(tokens[1])
                except Exception:
                    pass

            return self._apply_limits_small_reply(from_id, self._news_list(page=page))


        # Listados con página opcional
        if up_u.startswith("NEWS"):
            parts = up.split()
            page = self._parse_page_arg(parts, 1) if len(parts) >= 2 else 1
            return self._apply_limits_small_reply(from_id, self._bbs_news_unread(user, page))

        if up_u.startswith("LISTA"):
            parts = up.split()
            page = self._parse_page_arg(parts, 1) if len(parts) >= 2 else 1
            return self._apply_limits_small_reply(from_id, self._bbs_list_recent(page))

        if up_u.startswith("LEIDOS"):
            parts = up.split()
            page = self._parse_page_arg(parts, 1) if len(parts) >= 2 else 1
            return self._apply_limits_small_reply(from_id, self._bbs_list_read(user, page))

        if up_u.startswith("BUSCAR"):
            tokens = up.split()
            if len(tokens) < 2:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS BUSCAR <palabra/frase> [p]")

            now = time.time()
            last_s = float(self._last_search_ts.get(from_id, 0.0))
            if (now - last_s) < self.search_cooldown_sec:
                return self._apply_limits_small_reply(from_id, "Espera unos segundos antes de buscar de nuevo.")
            self._last_search_ts[from_id] = now

            page = 1
            try:
                page = int(tokens[-1])
                q = " ".join(tokens[1:-1]).strip()
                if not q:
                    return self._apply_limits_small_reply(from_id, f"Formato: #BBS BUSCAR <palabra/frase> [p]")
            except Exception:
                q = " ".join(tokens[1:]).strip()
                page = 1

            return self._apply_limits_small_reply(from_id, self._bbs_search(q, page))

        # VER / CUERPO / LEER
        if up_u.startswith("VER"):
            parts = up.split()
            if len(parts) != 2:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS VER <id>")
            try:
                bid = int(parts[1])
            except Exception:
                return self._apply_limits_small_reply(from_id, "ID inválido.")
            return self._apply_limits_small_reply(from_id, self._bbs_view(user, bid))

        if up_u.startswith("CUERPO"):
            parts = up.split()
            if len(parts) != 2:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS CUERPO <id>")
            try:
                bid = int(parts[1])
            except Exception:
                return self._apply_limits_small_reply(from_id, "ID inválido.")

            header, body = self._bbs_read_full(user, bid)
            if header.startswith("Boletín no encontrado"):
                return self._apply_limits_small_reply(from_id, header)

            chunks = _split_chunks(body, min(self.max_tx, self.page_chars))
            cost = self._estimate_cost(len(chunks), self.max_chunks_per_reply, add_more_hint=True)
            if not self._rate_allow(from_id, cost=cost):
                return self._apply_limits_small_reply(from_id, f"Límite de envío alcanzado. Usa #BBS MAS en unos segundos.")

            return self._enqueue_reply(from_id, chunks, max_now=self.max_chunks_per_reply, add_more_hint=True)

        if up_u.startswith("LEER"):
            parts = up.split()
            if len(parts) != 2:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS LEER <id>")
            try:
                bid = int(parts[1])
            except Exception:
                return self._apply_limits_small_reply(from_id, "ID inválido.")

            header, body = self._bbs_read_full(user, bid)
            if header.startswith("Boletín no encontrado"):
                return self._apply_limits_small_reply(from_id, header)

            full = header + "\n\n" + (body or "")
            chunks = _split_chunks(full, min(self.max_tx, self.page_chars))
            cost = self._estimate_cost(len(chunks), self.max_chunks_per_reply, add_more_hint=True)
            if not self._rate_allow(from_id, cost=cost):
                return self._apply_limits_small_reply(from_id, f"Límite de envío alcanzado. Usa #BBS MAS en unos segundos.")

            return self._enqueue_reply(from_id, chunks, max_now=self.max_chunks_per_reply, add_more_hint=True)

        # Privados
        if up_u.startswith("MP"):
            spec = up[2:].strip()
            return self._apply_limits_small_reply(from_id, self._bbs_mp_send(user, spec))

        if up_u.startswith("BANDEJA"):
            parts = up.split()
            page = self._parse_page_arg(parts, 1) if len(parts) >= 2 else 1
            return self._apply_limits_small_reply(from_id, self._bbs_inbox(user, page))

        # Encuestas
        if up_u.startswith("ENCUESTA"):
            spec = up[len("ENCUESTA"):].strip()
            return self._apply_limits_small_reply(from_id, self._poll_create(user, spec))

        if up_u == "ENCUESTAS":
            return self._apply_limits_small_reply(from_id, self._poll_list())

        if up_u.startswith("VOTO"):
            parts = up.split()
            if len(parts) != 3:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS VOTO <id> <op>")
            try:
                pid = int(parts[1])
                op = int(parts[2])
            except Exception:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS VOTO <id> <op>")
            return self._apply_limits_small_reply(from_id, self._poll_vote(user, pid, op))

        if up_u.startswith("RESULT"):
            parts = up.split()
            if len(parts) != 2:
                return self._apply_limits_small_reply(from_id, f"Formato: #BBS RESULT <id>")
            try:
                pid = int(parts[1])
            except Exception:
                return self._apply_limits_small_reply(from_id, "ID inválido.")
            return self._apply_limits_small_reply(from_id, self._poll_result(pid))

        if up_u == "ESTADISTICAS":
            return self._apply_limits_small_reply(from_id, self._stats())

        return self._apply_limits_small_reply(from_id, f"Comando no reconocido. Usa: #BBS MENU")
