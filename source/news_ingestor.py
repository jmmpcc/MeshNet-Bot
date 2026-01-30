#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#news_ingestor_v6.2.4.py

import os
import re
import json
import sqlite3
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests
import feedparser

HTTP_TIMEOUT = (5, 15)  # connect, read
MAX_ITEMS_PER_SOURCE = 40

DEFAULT_RSS_SOURCES = [

    # ─────────────────────────────────────────────
    # Radioafición / Radio / SDR
    {"source": "arrl_news", "url": "http://www.arrl.org/arrlletter?view=rss", "tags": "radio amateur"},
    {"source": "amsat_news", "url": "https://www.amsat.org/feed/", "tags": "radio amateur satellite"},
    {"source": "rs_sdr", "url": "https://www.rtl-sdr.com/feed/", "tags": "radio sdr"},
    {"source": "qrp_labs", "url": "https://qrp-labs.com/feed", "tags": "radio electronics"},
    {"source": "hamradio_dx", "url": "https://dxnews.com/feed/", "tags": "radio dx"},
    {"source": "hackaday_rf", "url": "https://hackaday.com/tag/rf/feed/", "tags": "radio electronics"},

    # ─────────────────────────────────────────────
    # Electrónica / Hardware
    {"source": "hackaday", "url": "https://hackaday.com/feed/", "tags": "electronics hacking"},
    {"source": "ee_times", "url": "https://www.eetimes.com/feed/", "tags": "electronics industry"},
    {"source": "electronicsweekly", "url": "https://www.electronicsweekly.com/feed/", "tags": "electronics"},
    {"source": "allaboutcircuits", "url": "https://www.allaboutcircuits.com/rss/", "tags": "electronics education"},

    # ─────────────────────────────────────────────
    # Ciencia (general)
    {"source": "science_mag", "url": "https://www.science.org/rss/news_current.xml", "tags": "science"},
    {"source": "nature_news", "url": "https://www.nature.com/nature.rss", "tags": "science"},
    {"source": "phys_org", "url": "https://phys.org/rss-feed/", "tags": "science"},
    {"source": "newscientist", "url": "https://www.newscientist.com/feed/home/", "tags": "science"},

    # ─────────────────────────────────────────────
    # Ciencia y divulgación en español
    {"source": "agencia_sinc", "url": "https://www.agenciasinc.es/rss", "tags": "science es"},
    {"source": "materia", "url": "https://elpais.com/rss/elpais/ciencia.xml", "tags": "science es"},
    {"source": "muyinteresante_ciencia", "url": "https://feeds.feedburner.com/muyinteresante/ciencia", "tags": "science es"},

    # ─────────────────────────────────────────────
    # Investigación española (oficial / institucional)
    {"source": "csic_noticias", "url": "https://www.csic.es/es/rss.xml", "tags": "research es"},
    {"source": "feci_noticias", "url": "https://www.fecyt.es/es/rss", "tags": "research es"},
    {"source": "universia_investigacion", "url": "https://www.universia.net/es/rss/investigacion.xml", "tags": "research es"},
    {"source": "uah_investigacion", "url": "https://www.uah.es/es/rss/investigacion.xml", "tags": "research es"},

    # ─────────────────────────────────────────────
    # Investigación / tecnología aplicada
    {"source": "ieee_spectrum", "url": "https://spectrum.ieee.org/rss/fulltext", "tags": "technology research"},
    {"source": "mit_tech_review", "url": "https://www.technologyreview.com/feed/", "tags": "technology research"},
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
    base_dir = os.getenv("BBS_DB_PATH", "bot_data/bbs").strip()
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "bbs.sqlite")


def db_connect(db_path: str) -> sqlite3.Connection:
    # timeout alto + busy_timeout para convivir con el servidor BBS (mismo SQLite) 24/7
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    return con


def db_init_news(con: sqlite3.Connection) -> None:
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
    headers = {
        "User-Agent": "MeshNet-BBS-NewsIngestor/1.0",
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return feedparser.parse(r.content)


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


def main() -> int:
    db_path = get_db_path()
    setup_logging(db_path)

    sources = load_sources()
    logging.info("news_ingestor start sources=%d db=%s", len(sources), db_path)

    inserted = 0
    dup = 0
    errors = 0

    con = db_connect(db_path)
    try:
        db_init_news(con)

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

                    link = canonicalize_url(getattr(e, "link", "") or "")
                    if not link:
                        continue

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

    finally:
        con.close()

    logging.info("news_ingestor done inserted=%d dup=%d errors=%d", inserted, dup, errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
