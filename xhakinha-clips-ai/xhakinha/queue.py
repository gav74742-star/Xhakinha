"""Fila persistente (SQLite local ou Turso). Sobrevive a reinícios; vários workers/processos podem consumir."""
from __future__ import annotations

import json
import time
from typing import Any

from .config import redact
from .db import Store
from .retry import NotReadyYet, PermanentError, TransientError, backoff_delay

LEASE_S = 1800  # job "running" há mais que isso (processo morreu) volta para pending


class JobQueue:
    def __init__(self, db: Store, base: float = 20.0, cap: float = 3600.0, max_attempts: int = 6):
        self.db, self.base, self.cap, self.max_attempts = db, base, cap, max_attempts

    def enqueue(self, type_: str, payload: dict | None = None, *, run_at: float | None = None,
                priority: int = 5, dedupe_key: str | None = None, content_id: int | None = None,
                max_attempts: int | None = None) -> int | None:
        now = time.time()
        try:
            return self.db.x(
                "INSERT INTO jobs(type,payload,status,priority,max_attempts,run_at,dedupe_key,content_id,created,updated)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (type_, json.dumps(payload or {}), "pending", priority, max_attempts or self.max_attempts,
                 run_at or now, dedupe_key, content_id, now, now))
        except Exception as e:  # dedupe_key duplicado entre jobs ativos
            if "UNIQUE" in str(e).upper():
                return None
            raise

    def claim(self, worker: str, types: list[str] | None = None, now: float | None = None) -> dict | None:
        now = now or time.time()
        # UM único comando atômico (funciona igual em SQLite local e no Turso, sem transação interativa):
        # escolhe o job mais antigo e já o marca como running; dois consumidores nunca pegam o mesmo job.
        where, args = "status='pending' AND run_at<=?", [now]
        if types:
            where += " AND type IN (%s)" % ",".join("?" * len(types))
            args += types
        rows = self.db.q(
            "UPDATE jobs SET status='running', locked_by=?, locked_at=?, attempts=attempts+1, updated=? "
            f"WHERE status='pending' AND id=(SELECT id FROM jobs WHERE {where} ORDER BY priority ASC, run_at ASC LIMIT 1) "
            "RETURNING *", [worker, now, now] + args)
        if not rows:
            return None
        job = dict(rows[0])
        job["payload"] = json.loads(job["payload"] or "{}")
        return job

    def done(self, job_id: int) -> None:
        self.db.x("UPDATE jobs SET status='done', updated=?, locked_by=NULL WHERE id=?", (time.time(), job_id))

    def fail(self, job: dict, exc: Exception, now: float | None = None) -> str:
        """Retorna o novo status: pending (reagendado) | dead (fila de falhas)."""
        now = now or time.time()
        msg = redact(f"{type(exc).__name__}: {exc}")[:1500]
        jid = job["id"]
        if isinstance(exc, NotReadyYet):
            deadline = job["payload"].get("deadline")
            if deadline and now > deadline:
                return self._dead(jid, msg, "timeout")
            delay = exc.retry_after or 300
            self.db.x("UPDATE jobs SET status='pending', run_at=?, attempts=MAX(attempts-1,0), last_error=?, error_kind='not_ready',"
                      " locked_by=NULL, updated=? WHERE id=?", (now + delay, msg, now, jid))
            return "pending"
        if isinstance(exc, PermanentError):
            return self._dead(jid, msg, "permanent")
        if isinstance(exc, TransientError):
            if job["attempts"] >= job["max_attempts"]:
                return self._dead(jid, msg, "transient_exhausted")
            delay = exc.retry_after or backoff_delay(job["attempts"], self.base, self.cap)
            self.db.x("UPDATE jobs SET status='pending', run_at=?, last_error=?, error_kind='transient', locked_by=NULL, updated=? WHERE id=?",
                      (now + delay, msg, now, jid))
            return "pending"
        # erro inesperado (bug): trata como transitório limitado, não infinito
        if job["attempts"] >= min(3, job["max_attempts"]):
            return self._dead(jid, msg, "unexpected")
        self.db.x("UPDATE jobs SET status='pending', run_at=?, last_error=?, error_kind='unexpected', locked_by=NULL, updated=? WHERE id=?",
                  (now + backoff_delay(job["attempts"], self.base, self.cap), msg, now, jid))
        return "pending"

    def _dead(self, jid: int, msg: str, kind: str) -> str:
        self.db.x("UPDATE jobs SET status='dead', last_error=?, error_kind=?, locked_by=NULL, updated=? WHERE id=?",
                  (msg, kind, time.time(), jid))
        return "dead"

    def defer(self, job_id: int, seconds: float) -> None:
        """Reagenda sem consumir tentativa (ex.: cota de publicação do dia)."""
        self.db.x("UPDATE jobs SET status='pending', run_at=?, attempts=MAX(attempts-1,0), locked_by=NULL, updated=? WHERE id=?",
                  (time.time() + seconds, time.time(), job_id))

    def recover(self, now: float | None = None) -> int:
        """Retoma tarefas interrompidas (processo caiu / reinício)."""
        now = now or time.time()
        n = self.db.conn.execute(
            "UPDATE jobs SET status='pending', locked_by=NULL, error_kind='recovered', updated=? "
            "WHERE status='running' AND locked_at<?", (now, now - LEASE_S)).rowcount
        return n

    def recover_all_running(self) -> int:
        """Na partida: ninguém mais está rodando, então tudo que estava 'running' volta."""
        return self.db.conn.execute(
            "UPDATE jobs SET status='pending', locked_by=NULL, error_kind='recovered', updated=? WHERE status='running'",
            (time.time(),)).rowcount

    def retry_dead(self, job_id: int) -> bool:
        n = self.db.conn.execute(
            "UPDATE jobs SET status='pending', attempts=0, run_at=?, error_kind=NULL, updated=? WHERE id=? AND status='dead'",
            (time.time(), time.time(), job_id)).rowcount
        return bool(n)

    def counts(self) -> dict[str, int]:
        out = {"pending": 0, "running": 0, "done": 0, "dead": 0}
        for r in self.db.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status"):
            out[r["status"]] = r["n"]
        return out

    def open_for_content(self, content_id: int) -> int:
        return self.db.one("SELECT COUNT(*) n FROM jobs WHERE content_id=? AND status IN ('pending','running')",
                           (content_id,))["n"]
