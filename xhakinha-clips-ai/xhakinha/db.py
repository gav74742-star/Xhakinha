"""SQLite (WAL) thread/process-safe. Fila, estados e histórico vivem aqui (persistente)."""
from __future__ import annotations

import base64
import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import Settings, redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS secrets(name TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, name TEXT NOT NULL, ref TEXT NOT NULL,
  enabled INTEGER DEFAULT 1, authorized INTEGER DEFAULT 0, interval_s INTEGER DEFAULT 300,
  config TEXT DEFAULT '{}', last_check REAL, last_ok REAL, last_error TEXT, created REAL,
  archive_scanned REAL, UNIQUE(platform, ref));
CREATE TABLE IF NOT EXISTS contents(
  id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id), ext_id TEXT NOT NULL,
  kind TEXT NOT NULL, title TEXT, url TEXT, duration REAL, status TEXT NOT NULL,
  local_path TEXT, fingerprint TEXT, live_state TEXT, ended_at REAL, published_at REAL,
  discovered REAL, processed_at REAL, error TEXT, meta TEXT DEFAULT '{}', protected INTEGER DEFAULT 0,
  clips_made INTEGER DEFAULT 0, UNIQUE(source_id, ext_id));
CREATE INDEX IF NOT EXISTS ix_contents_fp ON contents(fingerprint);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, payload TEXT DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  priority INTEGER DEFAULT 5, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 6,
  run_at REAL NOT NULL, last_error TEXT, error_kind TEXT, locked_by TEXT, locked_at REAL,
  dedupe_key TEXT, content_id INTEGER, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS ix_jobs_pick ON jobs(status, run_at, priority);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_dedupe ON jobs(dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending','running');
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY, content_id INTEGER REFERENCES contents(id), start REAL, end REAL, score REAL,
  scores TEXT DEFAULT '{}', category TEXT, status TEXT NOT NULL, path TEXT, sha256 TEXT, size INTEGER,
  duration REAL, transcript TEXT, origin TEXT, description TEXT, hashtags TEXT, created REAL,
  updated REAL, error TEXT, UNIQUE(content_id, start, end));
CREATE TABLE IF NOT EXISTS publications(
  id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL REFERENCES clips(id), platform TEXT NOT NULL,
  status TEXT NOT NULL, scheduled_at REAL, attempts INTEGER DEFAULT 0, next_try REAL,
  state TEXT DEFAULT '{}', remote_id TEXT, public_id TEXT, url TEXT, visibility TEXT,
  file_path TEXT, error TEXT, created REAL, updated REAL, published_at REAL, needs_admin INTEGER DEFAULT 0,
  UNIQUE(clip_id, platform));
CREATE TABLE IF NOT EXISTS attempts_log(
  id INTEGER PRIMARY KEY, ts REAL, ref_type TEXT, ref_id INTEGER, op TEXT, ok INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, level TEXT, component TEXT, message TEXT, ctx TEXT);
CREATE TABLE IF NOT EXISTS quota(day TEXT, api TEXT, used INTEGER DEFAULT 0, exhausted_until REAL, PRIMARY KEY(day, api));
CREATE TABLE IF NOT EXISTS metrics(
  id INTEGER PRIMARY KEY, publication_id INTEGER, ts REAL, views INTEGER, likes INTEGER, comments INTEGER,
  shares INTEGER, retention REAL, raw TEXT);
CREATE TABLE IF NOT EXISTS segments_used(id INTEGER PRIMARY KEY, content_id INTEGER, fingerprint TEXT, start REAL, end REAL);
CREATE TABLE IF NOT EXISTS realtests(id INTEGER PRIMARY KEY, started REAL, finished REAL, status TEXT, platform TEXT, steps TEXT DEFAULT '[]', result TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS logins(ip TEXT, ts REAL);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "engine_state": "running",           # running | paused | stopped
    "daily_min": 8, "daily_normal": 10, "daily_max": 12,   # por plataforma
    "min_publish_score": 60, "min_clip_score": 55,
    "min_interval_min": 45, "window_start": "08:00", "window_end": "23:30",
    "clip_min_s": 15, "clip_pref_max_s": 60, "clip_hard_max_s": 90,
    "clips_per_hour_of_video": 6,         # alvo de busca (não é limite de produção)
    "chunk_min": 45, "chunk_overlap_s": 120,
    "vod_delay_min": 5, "vod_delay_max": 15,
    "archive_clips_per_week": 5, "archive_enabled": 1,
    "watermark_opacity": 0.65, "watermark_position": "top_left", "watermark_scale": 0.035,
    "handle_instagram": "", "handle_tiktok": "",
    "platforms_enabled": ["instagram", "tiktok"],
    "tiktok_audited": 0,                  # 0 => força SELF_ONLY (apps não auditados)
    "tiktok_privacy": "SELF_ONLY",
    "credit_line": 1,
    "original_grace_h": 24,               # carência antes de apagar original já processado
    "published_clip_retention_h": 0,      # 0 = apaga assim que confirmado em todas as plataformas
    "work_retention_h": 6, "unpublished_retention_days": 0,   # 0 = nunca expira
    "backup_every_h": 12, "backup_keep": 14,
    "youtube_monitor_interval_s": 300,
    "monitor_enabled": 1, "bypass_none": 1,
}


class Store:
    def __init__(self, settings: Settings):
        self.s = settings
        self._local = threading.local()
        settings.ensure_dirs()
        self._init()
        self._fernet = Fernet(base64.urlsafe_b64encode(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=b"xhakinha-secrets-v1",
                 info=b"fernet").derive(settings.secret_key.encode())))

    # conexão -----------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(str(self.s.db_path), timeout=30, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.c = c
        return c

    def _init(self) -> None:
        self.conn.executescript(SCHEMA)

    def q(self, sql: str, args: Iterable = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Iterable = ()) -> dict | None:
        r = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, args: Iterable = ()) -> int:
        cur = self.conn.execute(sql, tuple(args))
        return cur.lastrowid or 0

    @contextlib.contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # settings ----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if r is not None:
            return json.loads(r["value"])
        return DEFAULT_SETTINGS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    def all_settings(self) -> dict:
        d = dict(DEFAULT_SETTINGS)
        for r in self.q("SELECT key,value FROM settings"):
            d[r["key"]] = json.loads(r["value"])
        return d

    # segredos criptografados (tokens de plataformas) ---------------------
    def put_secret(self, name: str, value: dict | str) -> None:
        raw = json.dumps(value).encode()
        self.x("INSERT INTO secrets(name,value,updated) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
               "value=excluded.value, updated=excluded.updated",
               (name, self._fernet.encrypt(raw).decode(), time.time()))

    def get_secret(self, name: str):
        r = self.one("SELECT value FROM secrets WHERE name=?", (name,))
        if not r:
            return None
        try:
            return json.loads(self._fernet.decrypt(r["value"].encode()))
        except InvalidToken:
            return None

    def del_secret(self, name: str) -> None:
        self.x("DELETE FROM secrets WHERE name=?", (name,))

    # logs --------------------------------------------------------------
    def event(self, level: str, component: str, message: str, **ctx) -> None:
        try:
            self.x("INSERT INTO events(ts,level,component,message,ctx) VALUES(?,?,?,?,?)",
                   (time.time(), level, component, redact(message)[:2000], redact(json.dumps(ctx, default=str))[:2000]))
        except sqlite3.Error:
            pass

    def attempt(self, ref_type: str, ref_id: int, op: str, ok: bool, message: str = "") -> None:
        self.x("INSERT INTO attempts_log(ts,ref_type,ref_id,op,ok,message) VALUES(?,?,?,?,?,?)",
               (time.time(), ref_type, ref_id, op, 1 if ok else 0, redact(message)[:1000]))
