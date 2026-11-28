"""SQLite (WAL) thread/process-safe. Fila, estados e histórico vivem aqui (persistente)."""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import dbdriver
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
  updated REAL, error TEXT, face_matches TEXT NOT NULL DEFAULT '[]', UNIQUE(content_id, start, end));
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
CREATE TABLE IF NOT EXISTS face_profiles(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, consent_version TEXT NOT NULL,
  consent_scope TEXT NOT NULL, consent_evidence TEXT NOT NULL DEFAULT '',
  consent_recorded REAL NOT NULL, consent_by TEXT NOT NULL,
  consent_expires REAL, revoked_at REAL, threshold REAL NOT NULL DEFAULT 0.50,
  reference_count INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_face_profiles_active ON face_profiles(revoked_at, consent_expires);
CREATE TABLE IF NOT EXISTS face_profile_sources(
  profile_id INTEGER NOT NULL REFERENCES face_profiles(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  created REAL NOT NULL,
  PRIMARY KEY(profile_id, source_id));
CREATE INDEX IF NOT EXISTS ix_face_profile_sources_source
  ON face_profile_sources(source_id, profile_id);
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
    # controle de armazenamento no Drive (nuvem): pausa NOVAS ingestões, nunca publicações
    "ingest_paused": 0, "ingest_paused_reason": "", "drive_min_free_gb": 1.5, "drive_resume_free_gb": 2.5,
}


SCHEMA_VERSION = 6          # 6 = vínculo explícito entre perfis faciais e fontes.
DBError = (sqlite3.Error, dbdriver.DbUnavailable)


class Store:
    def __init__(self, settings: Settings):
        self.s = settings
        self.backend = "turso" if settings.use_turso else "sqlite"
        self._local = threading.local()
        settings.ensure_dirs()
        self._init()
        self._fernet = Fernet(base64.urlsafe_b64encode(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=b"xhakinha-secrets-v1",
                 info=b"fernet").derive(settings.secret_key.encode())))

    # conexão -----------------------------------------------------------
    @property
    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            if self.backend == "turso":
                c = dbdriver.LibsqlConn(self.s.turso_url, self.s.turso_token)
            else:
                c = dbdriver.open_sqlite(str(self.s.db_path))
            self._local.c = c
        return c

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"SELECT name FROM pragma_table_info('{table}')").fetchall()}

    def _migrate(self) -> None:
        for tbl, col, ddl in (
            ("clips", "drive_path", "TEXT"),
            ("contents", "drive_path", "TEXT"),
            ("clips", "face_matches", "TEXT NOT NULL DEFAULT '[]'"),
            ("face_profiles", "consent_evidence", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in self._columns(tbl):
                self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")

    def _init(self) -> None:
        if self.backend == "sqlite":
            self.conn.executescript(SCHEMA)
            self._migrate()                                   # bancos/snapshots antigos continuam válidos
            return
        # Turso: o esquema só é CRIADO de propósito (scripts/turso_migrate.py init ou XH_TURSO_INIT=1), nunca por acidente.
        has = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        if not has:
            if os.environ.get("XH_TURSO_INIT") != "1":
                from .config import ConfigError
                raise ConfigError("O banco Turso ainda não tem o esquema do Xhakinha. Rode UMA vez: "
                                  "python scripts/turso_migrate.py init   (ele cria as tabelas de forma controlada)")
            self.conn.executescript(SCHEMA)
        ver = self.conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone() if has else None
        cur = int(json.loads(ver["value"])) if ver else 0
        if not cur >= SCHEMA_VERSION:                              # só escreve quando o esquema realmente subiu
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.execute("INSERT INTO settings(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (json.dumps(SCHEMA_VERSION),))

    def backup_source(self):
        """Objeto com .backup(dst_sqlite_conn) para o gerador de snapshots (SQLite: a própria conexão; Turso: exportação lógica)."""
        if self.backend == "sqlite":
            return self.conn
        store = self

        class _Src:
            def backup(self, dst):
                import tempfile
                fd, tmp = tempfile.mkstemp(suffix=".sqlite")
                os.close(fd)
                try:
                    store.export_sqlite(tmp)
                    src = sqlite3.connect(tmp)
                    try:
                        src.backup(dst)
                    finally:
                        src.close()
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        return _Src()

    def export_sqlite(self, dest) -> None:
        """Gera um arquivo SQLite local com o conteúdo atual (backup/snapshot). No SQLite usa a API de backup (consistente);
        no Turso é uma cópia lógica tabela a tabela (NÃO transacional entre tabelas)."""
        dest = str(dest)
        if self.backend == "sqlite":
            dst = sqlite3.connect(dest)
            try:
                self.conn.backup(dst)
            finally:
                dst.close()
            return
        dst = sqlite3.connect(dest)
        try:
            dst.executescript(SCHEMA)
            names = [r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
            for t in names:
                have = {r[1] for r in dst.execute(f"PRAGMA table_info({t})").fetchall()}
                if not have:
                    continue
                cur = self.conn.execute(f"SELECT * FROM {t}")
                for r in cur.fetchall():
                    cols = [k for k in r.keys() if k in have]
                    dst.execute(f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [r[k] for k in cols])
            dst.commit()
        finally:
            dst.close()

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
        """Transação interativa: SOMENTE SQLite local. No Turso use um único comando atômico (UPDATE ... RETURNING / upsert)."""
        if self.backend != "sqlite":
            raise NotImplementedError("transação interativa não é suportada no Turso; use comandos atômicos")
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
        except Exception:          # log nunca derruba o fluxo (SQLite, Turso fora do ar, erro do cliente libsql)
            pass

    def attempt(self, ref_type: str, ref_id: int, op: str, ok: bool, message: str = "") -> None:
        self.x("INSERT INTO attempts_log(ts,ref_type,ref_id,op,ok,message) VALUES(?,?,?,?,?,?)",
               (time.time(), ref_type, ref_id, op, 1 if ok else 0, redact(message)[:1000]))
