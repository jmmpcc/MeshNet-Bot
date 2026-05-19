#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# news_ingestor_v7.0.12.py  (host-side)

import os
import re
import json
import sqlite3
import hashlib
import logging
import argparse
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit

import requests
import feedparser

HTTP_TIMEOUT = (5, 30)  # connect, read
MAX_ITEMS_PER_SOURCE = 10

# ── Retención automática de noticias (días)
# 0 = sin purga
NEWS_RETENTION_DAYS = 5

DEFAULT_RSS_SOURCES = [

    # 📡 Radioafición / HAM
    {"source": "ure", "url": "https://www.ure.es/feed/", "tags": "ham"},
    #{"source": "ure_noticias", "url": "https://www.ure.es/noticias/feed/", "tags": "ham"},
    #{"source": "radioaficion_blog", "url": "https://radioaficion.blogspot.com/feeds/posts/default?alt=rss", "tags": "ham"},
    #{"source": "radioaficion_digital", "url": "https://radioaficiondigital.com/feed/", "tags": "ham"},

    # 🔬 Ciencia (España)
    {"source": "agencia_sinc", "url": "http://www.agenciasinc.es/feed/noticias", "tags": "ciencia"},
    {"source": "csic", "url": "https://www.csic.es/es/rss.xml", "tags": "ciencia"},
    {"source": "elpais_ciencia", "url": "https://elpais.com/rss/elpais/ciencia.xml", "tags": "ciencia"},
    {"source": "abc_ciencia", "url": "https://www.abc.es/rss/feeds/abc_Ciencia.xml", "tags": "ciencia"},

    # ⚡ Electrónica
    {"source": "redeweb", "url": "https://www.redeweb.com/feed/", "tags": "electronica"},
    #{"source": "electronicapratica", "url": "https://www.electronicapratica.com/feed/", "tags": "electronica"},

    # 🖥️ Informática / Tecnología (Castellano)

    {"source": "xataka", "url": "https://www.xataka.com/index.xml", "tags": "informatica"},
    {"source": "genbeta", "url": "https://www.genbeta.com/index.xml", "tags": "informatica"},
    {"source": "muycomputer", "url": "https://www.muycomputer.com/feed/", "tags": "informatica"},
    #{"source": "computerhoy", "url": "https://www.computerhoy.com/rss.xml", "tags": "informatica"},

    # 🔐 Seguridad / IT profesional
    {"source": "incibe", "url": "https://www.incibe.es/feed/vulnerabilities", "tags": "ciberseguridad"},
    #{"source": "eshacker", "url": "https://www.es-hacker.com/feeds/posts/default?alt=rss", "tags": "ciberseguridad"},

    # ⚙️ Linux / Open Source
    {"source": "muylinux", "url": "https://www.muylinux.com/feed/", "tags": "linux"},
    {"source": "linuxadictos", "url": "https://www.linuxadictos.com/feed", "tags": "linux"},

    # ⚙️ Astronomia
    #{"source": "oan", "url": "https://www.oan.es/rss", "tags": "astronomia"},
    {"source": "iac", "url": "https://www.iac.es/es/rss.xml", "tags": "astronomia"},
    {"source": "eureka", "url": "https://danielmarin.naukas.com/feed/", "tags": "astronomia"},
    
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        fragmentless = parts._replace(fragment="")

        q = fragmentless.query
        if q:
            pairs = []
            for kv in q.split("&"):
                k = kv.split("=", 1)[0].lower().strip()
                if k.startswith("utm_") or k in ("fbclid", "gclid", "mc_cid", "mc_eid"):
                    continue
                pairs.append(kv)
            fragmentless = fragmentless._replace(query="&".join(pairs))

        return urlunsplit(fragmentless)
    except Exception:
        return url.strip()

def shorten_url(url: str) -> str:
    """
    Acorta URLs largas para uso en BBS (LoRa-friendly).
    Usa is.gd sin API key.
    Si falla, devuelve la URL original.
    """
    if not url or len(url) <= 40:
        return url

    try:
        r = requests.get(
            "https://is.gd/create.php",
            params={"format": "simple", "url": url},
            timeout=(4, 10),
        )
        if r.status_code == 200:
            short = r.text.strip()
            if short.startswith("http"):
                return short
    except Exception:
        pass

    return url


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_hash(title: str, summary: str) -> str:
    base = (title or "").strip().lower() + "\n" + (summary or "").strip().lower()
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def parse_published(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc)
                return dt.replace(microsecond=0).isoformat()
            except Exception:
                pass
    return ""


def get_db_path() -> str:
    """
    Devuelve la ruta real de la DB de la BBS (host-side).
    IMPORTANTE: en este proyecto la DB se llama 'bbs_data.db'
    y convive con '-wal' y '-shm' en el mismo directorio.
    """
    base_dir = (os.getenv("BBS_DB_PATH", "bot_data/bbs") or "").strip() or "bot_data/bbs"
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "bbs_data.db")


def db_connect(db_path: str) -> sqlite3.Connection:
    # timeout alto + busy_timeout para convivir con el servidor BBS (mismo SQLite) 24/7
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    return con


def _table_columns(con: sqlite3.Connection, table: str) -> set:
    cols = set()
    try:
        cur = con.execute(f"PRAGMA table_info({table});")
        for row in cur.fetchall():
            # row: cid, name, type, notnull, dflt_value, pk
            cols.add(str(row[1]))
    except Exception:
        pass
    return cols


def db_init_news(con: sqlite3.Connection) -> None:
    """
    Inicializa tabla news.
    Nota: si la tabla ya existía con otro esquema, CREATE TABLE IF NOT EXISTS no la cambia.
    """
    con.execute(
        """
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
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_news_created_at ON news(created_at);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_news_published_at ON news(published_at);")
    con.commit()


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    headers = {"User-Agent": "MeshNet-BBS-NewsIngestor/1.0"}
    last_exc = None
    for _ in range(2):  # 1 reintento
        try:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return feedparser.parse(r.content)
        except Exception as e:
            last_exc = e
    raise last_exc


def insert_news(con: sqlite3.Connection, item: dict) -> bool:
    try:
        con.execute(
            """
            INSERT INTO news (source, title, summary, url, published_at, tags, lang, content_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["source"],
                item["title"],
                item.get("summary", ""),
                item["url"],
                item.get("published_at", ""),
                item.get("tags", ""),
                item.get("lang", ""),
                item["content_hash"],
                item["created_at"],
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def load_sources() -> list[dict]:
    raw = os.getenv("BBS_NEWS_SOURCES_JSON", "").strip()
    if not raw:
        return DEFAULT_RSS_SOURCES
    try:
        data = json.loads(raw)
        if isinstance(data, list) and all(isinstance(x, dict) for x in data):
            return data
    except Exception:
        pass
    return DEFAULT_RSS_SOURCES


def setup_logging(db_path: str) -> None:
    base_dir = os.path.dirname(db_path)
    log_path = os.path.join(base_dir, "news_ingestor.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )


def _get_retention_days() -> int:
    """
    Retención fija definida en código.
    """
    try:
        return int(NEWS_RETENTION_DAYS) if int(NEWS_RETENTION_DAYS) > 0 else 0
    except Exception:
        return 0


def purge_old_news(con: sqlite3.Connection, retention_days: int) -> int:
    """
    Borra registros antiguos para mantener solo noticias recientes.
    Criterio:
      - Si published_at existe: borra donde published_at < cutoff (y no vacío)
      - Además, si published_at es vacío: borra por created_at < cutoff (si existe)
    Devuelve número de filas borradas (aprox, suma de ambos deletes).
    """
    if retention_days <= 0:
        return 0

    cols = _table_columns(con, "news")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).replace(microsecond=0).isoformat()

    deleted = 0
    cur = con.cursor()

    # 1) Por published_at (si existe)
    if "published_at" in cols:
        cur.execute(
            "DELETE FROM news WHERE published_at <> '' AND published_at < ?;",
            (cutoff,),
        )
        deleted += cur.rowcount

    # 2) Fallback por created_at para filas sin published_at o esquema sin published_at
    if "created_at" in cols:
        if "published_at" in cols:
            cur.execute(
                "DELETE FROM news WHERE (published_at = '' OR published_at IS NULL) AND created_at <> '' AND created_at < ?;",
                (cutoff,),
            )
        else:
            cur.execute(
                "DELETE FROM news WHERE created_at <> '' AND created_at < ?;",
                (cutoff,),
            )
        deleted += cur.rowcount

    con.commit()
    return deleted


def main(argv=None) -> int:

    # ── CLI (modo compatible). Por defecto se ejecuta la ingesta (comportamiento histórico).
    # Esto evita que un simple '--help' dispare una ingesta real (útil en despliegues 24/7).
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="news_ingestor.py",
        description="Ingesta RSS/Atom hacia la DB de BBS (tabla news).",
    )
    parser.add_argument("--list-sources", action="store_true", help="Muestra las fuentes configuradas y sale.")
    parser.add_argument("--check-only", action="store_true", help="Valida accesibilidad de las fuentes (HTTP) y sale.")
    args = parser.parse_args(argv)

    db_path = get_db_path()
    setup_logging(db_path)

    sources = load_sources()

    if args.list_sources:
        for src in sources:
            print(f'{src.get("source","")}\t{src.get("tags","")}\t{src.get("url","")}', flush=True)
        return 0

    if args.check_only:
        ok = 0
        bad = 0
        for src in sources:
            name = src.get("source","")
            url = src.get("url","")
            try:
                r = requests.get(url, headers={"User-Agent": "MeshNet-NewsIngestor/1.0"}, timeout=HTTP_TIMEOUT, allow_redirects=True)
                if 200 <= r.status_code < 400:
                    ok += 1
                    print(f"OK\t{name}\t{r.status_code}\t{url}", flush=True)
                else:
                    bad += 1
                    print(f"BAD\t{name}\t{r.status_code}\t{url}", flush=True)
            except Exception as e:
                bad += 1
                print(f"BAD\t{name}\t{type(e).__name__}: {e}\t{url}", flush=True)
        print(f"check-only: ok={ok} bad={bad}", flush=True)
        return 0

    logging.info("news_ingestor start sources=%d db=%s", len(sources), db_path)

    inserted = 0
    dup = 0
    errors = 0

    con = db_connect(db_path)
    try:
        db_init_news(con)

        # ── Purga automática por retención (antes de ingerir)
        retention_days = _get_retention_days()
        if retention_days > 0:
            purged = purge_old_news(con, retention_days)
            logging.info("news_ingestor retention purge days=%d deleted=%d", retention_days, purged)

        for src in sources:
            name = src["source"]
            url = src["url"]
            tags = src.get("tags", "")

            try:
                feed = fetch_feed(url)
                entries = feed.entries[:MAX_ITEMS_PER_SOURCE]

                for e in entries:
                    title = strip_html(getattr(e, "title", "")).strip()
                    if not title:
                        continue

                    link_raw = canonicalize_url(getattr(e, "link", "") or "")
                    if not link_raw:
                        continue

                    link = shorten_url(link_raw)


                    summary = strip_html(getattr(e, "summary", "") or getattr(e, "description", "") or "")
                    published = parse_published(e)

                    item = {
                        "source": name,
                        "title": title,
                        "summary": summary[:2000],
                        "url": link,
                        "published_at": published,
                        "tags": tags,
                        "lang": (feed.feed.get("language") or "").strip(),
                        "content_hash": make_hash(title, summary),
                        "created_at": utc_now_iso(),
                    }

                    if insert_news(con, item):
                        inserted += 1
                    else:
                        dup += 1

                con.commit()

            except Exception:
                errors += 1
                logging.exception("Error ingesting source=%s url=%s", name, url)

        # ── Purga automática por retención (después de ingerir)
        if retention_days > 0:
            purged2 = purge_old_news(con, retention_days)
            if purged2:
                logging.info("news_ingestor retention post-purge days=%d deleted=%d", retention_days, purged2)

    finally:
        con.close()

    logging.info("news_ingestor done inserted=%d dup=%d errors=%d", inserted, dup, errors)

    # IMPORTANTE 24/7:
    # Un feed roto (SSL/404/timeout) no debe marcar el servicio como "failed".
    # Se registra en log/journal y el timer sigue funcionando.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
