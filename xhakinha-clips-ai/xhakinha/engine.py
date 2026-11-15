"""Motor: workers com concorrência autoajustável + agendador. Roda dentro do painel ou em processo separado."""
from __future__ import annotations

import math
import os
import random
import threading
import time
import traceback

import psutil

from . import scheduler
from .config import Settings, redact
from .db import Store
from .pipeline import HEAVY, Pipeline
from .queue import JobQueue
from .retry import XhError
from .storage import Storage


class Engine:
    def __init__(self, s: Settings, db: Store | None = None, pipeline: Pipeline | None = None):
        self.s = s
        self.db = db or Store(s)
        self.q = JobQueue(self.db, s.retry_base_s, s.retry_cap_s, s.retry_max_attempts)
        self.st = Storage(s, self.db, self.q)
        self.pl = pipeline or Pipeline(s, self.db, self.q, self.st)
        self._stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.target_workers = 1
        cpus = os.cpu_count() or 2
        self.heavy = threading.Semaphore(max(1, min(cpus // 2 or 1, 4)))
        self.busy: dict[str, dict] = {}
        self._last: dict[str, float] = {}
        self.id = f"{os.getpid()}"

    # --------------------------------------------------------------- ciclo de vida
    def start(self, workers: bool = True, scheduler_on: bool = True) -> None:
        n = self.q.recover_all_running()
        if n:
            self.db.event("info", "engine", f"Recuperadas {n} tarefas interrompidas por reinício")
        if workers:
            for i in range(self.s.max_workers):
                t = threading.Thread(target=self._worker, args=(i,), daemon=True, name=f"worker-{i}")
                t.start()
                self.threads.append(t)
        if scheduler_on:
            t = threading.Thread(target=self._sched_loop, daemon=True, name="scheduler")
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self.threads:
            t.join(timeout=5)

    def state(self) -> str:
        return self.db.get("engine_state", "running")

    # --------------------------------------------------------------- workers
    def run_one(self, worker: str = "w") -> bool:
        """Executa 1 job (usado pelos workers e pelos testes). Retorna False se não havia trabalho."""
        job = self.q.claim(worker)
        if not job:
            return False
        heavy = job["type"] in HEAVY
        if heavy:
            self.heavy.acquire()
        self.busy[worker] = {"job": job["id"], "type": job["type"], "since": time.time()}
        try:
            self.pl.handle(job)
            self.q.done(job["id"])
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, XhError):
                self.db.event("error", "worker", f"Erro inesperado no job {job['id']} ({job['type']}): {e}", tb=traceback.format_exc()[-800:])
            status = self.q.fail(job, e)
            self.db.event("warn" if status == "pending" else "error", "worker",
                          f"job {job['id']} {job['type']} -> {status}: {redact(str(e))[:200]}")
            if status == "dead" and job["type"] == "publish":
                pid = job["payload"].get("pub_id")
                self.db.x("UPDATE publications SET status='failed', needs_admin=1, error=?, updated=? WHERE id=?", (redact(str(e))[:400], time.time(), pid))
                p = self.db.one("SELECT clip_id FROM publications WHERE id=?", (pid,))
                if p:
                    scheduler.refresh_clip_status(self.db, p["clip_id"])
        finally:
            self.busy.pop(worker, None)
            if heavy:
                self.heavy.release()
        return True

    def _worker(self, idx: int) -> None:
        name = f"{self.id}-w{idx}"
        while not self._stop.is_set():
            if idx >= self.target_workers or self.state() != "running":
                time.sleep(2)
                continue
            try:
                if not self.run_one(name):
                    time.sleep(2 + random.random())
            except Exception:  # noqa: BLE001
                time.sleep(5)

    def autoscale(self) -> int:
        """Dimensiona a concorrência por CPU/memória (sem cota fixa; só teto técnico configurável)."""
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().available / psutil.virtual_memory().total
        pending = self.q.counts()["pending"]
        t = self.target_workers
        if cpu < 65 and mem > 0.2 and pending > len(self.busy):
            t += 1
        elif cpu > 90 or mem < 0.1:
            t -= 1
        self.target_workers = max(1, min(self.s.max_workers, t))
        return self.target_workers

    # --------------------------------------------------------------- agendador
    def _every(self, key: str, secs: float) -> bool:
        now = time.time()
        if now - self._last.get(key, 0) >= secs:
            self._last[key] = now
            return True
        return False

    def _sched_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.db.event("error", "scheduler", f"tick falhou: {e}", tb=traceback.format_exc()[-600:])
            self._stop.wait(20)

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        if self.state() == "stopped":
            return
        self.autoscale()
        self.q.recover(now)
        if self.state() != "running":
            return
        # monitores (um por fonte, respeitando intervalo e backoff gravado em last_check)
        if self.db.get("monitor_enabled") and not self.db.get("ingest_paused"):
            for src in self.db.q("SELECT * FROM sources WHERE enabled=1 AND platform NOT IN ('url','upload')"):
                if now - (src["last_check"] or 0) >= src["interval_s"]:
                    self.q.enqueue("monitor", {"source_id": src["id"]}, dedupe_key=f"monitor:{src['id']}", priority=1, max_attempts=1)
                    self.db.x("UPDATE sources SET last_check=? WHERE id=?", (now, src["id"]))
                if src["authorized"] and not src["archive_scanned"] and src["platform"] in ("youtube", "twitch", "instagram"):
                    self.q.enqueue("archive_scan", {"source_id": src["id"]}, dedupe_key=f"archscan:{src['id']}", priority=9, max_attempts=4)
        # ciclo diário (00:00 no fuso configurado) + planejamento contínuo
        if self.db.get("last_cycle_day") != scheduler.day_key(self.s.tz, now):
            scheduler.daily_cycle(self.db, self.s.tz, now)
        elif self._every("plan", 300):
            scheduler.plan(self.db, self.s.tz, now)
        # publicações vencidas
        for p in scheduler.due(self.db, now):
            self.q.enqueue("publish", {"pub_id": p["id"]}, dedupe_key=f"pub:{p['id']}", priority=2, max_attempts=12)
        if not self.db.get("ingest_paused"):
            self.plan_archive(now)
        if self._every("cleanup", 900):
            self.q.enqueue("cleanup", dedupe_key="cleanup", max_attempts=2, priority=9)
        if self._every("backup", float(self.db.get("backup_every_h")) * 3600):
            self.q.enqueue("backup", dedupe_key="backup", max_attempts=2, priority=9)

    def plan_archive(self, now: float) -> None:
        """Reaproveita o acervo antigo de forma gradual (~N clipes/semana), sem competir com conteúdo novo."""
        if not self.db.get("archive_enabled"):
            return
        weekly = int(self.db.get("archive_clips_per_week"))
        if weekly <= 0:
            return
        busy = self.db.one("SELECT COUNT(*) n FROM contents WHERE kind='archive' AND status IN ('queued','downloading','downloaded','analyzing')")["n"]
        if busy:
            return
        recent = self.db.one("SELECT COUNT(*) n FROM clips WHERE origin='archive' AND created>=?", (now - 7 * 86400,))["n"]
        if recent >= weekly:
            return
        last = float(self.db.get("last_archive_start", 0) or 0)
        if now - last < (7 * 86400) / weekly / 2:       # espaça os vídeos antigos ao longo da semana
            return
        pick = self.db.one("""SELECT c.* FROM contents c JOIN sources s ON s.id=c.source_id WHERE c.kind='archive' AND c.status='discovered'
                              AND s.authorized=1 AND s.enabled=1 AND c.duration BETWEEN 120 AND 14400 ORDER BY RANDOM() LIMIT 1""")
        if not pick:
            return
        self.db.set("last_archive_start", now)
        self.db.x("UPDATE contents SET status='queued' WHERE id=?", (pick["id"],))
        self.q.enqueue("acquire", {"content_id": pick["id"], "deadline": now + 48 * 3600}, priority=8, dedupe_key=f"acquire:{pick['id']}",
                       content_id=pick["id"], max_attempts=6)
