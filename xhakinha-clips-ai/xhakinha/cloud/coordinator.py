"""Coordenador central: cadastro de executores/provedores, fila com leases + fencing token,
cotas, circuit breaker, checkpoints, locks distribuídos, reconciliação. Escrita serializada (um lock +
SQLite; cada operação crítica é UM statement atômico ou BEGIN IMMEDIATE).

Fencing: cada `claim` incrementa `tasks.fence`. Toda escrita (renew/checkpoint/complete/fail) exige
(owner, fence) atuais. Um executor antigo que volta depois do lease expirar recebe `Fenced` e NÃO
consegue alterar estado nem confirmar resultado; seus arquivos ficam órfãos (nunca referenciados)."""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from ..retry import backoff_delay
from . import quota

SCHEMA = (Path(__file__).with_name("schema.sql")).read_text()
LEASE_S = 300.0
EXECUTOR_TIMEOUT_S = 180.0
PREFER_WAIT_S = 600.0


class Fenced(Exception):
    """O executor perdeu a posse da tarefa/lock (lease expirou e outro assumiu)."""


class Coordinator:
    def __init__(self, path: str | Path = ":memory:", clock: Callable[[], float] = time.time):
        self.clock = clock
        self.path = str(path)
        self.lock = threading.RLock()
        self.c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA busy_timeout=5000")
        self.c.executescript(SCHEMA)
        for name, cfg in quota.PROVIDER_DEFAULTS.items():
            self.c.execute("INSERT OR IGNORE INTO providers(name,config) VALUES(?,?)", (name, json.dumps(cfg)))

    # ------------------------------------------------------------ util
    def _q(self, sql, a=()):
        return [dict(r) for r in self.c.execute(sql, a).fetchall()]

    def _one(self, sql, a=()):
        r = self.c.execute(sql, a).fetchone()
        return dict(r) if r else None

    def event(self, level: str, comp: str, msg: str) -> None:
        self.c.execute("INSERT INTO events(ts,level,component,message) VALUES(?,?,?,?)", (self.clock(), level, comp, msg[:500]))

    def integrity(self) -> dict:
        with self.lock:
            r = self.c.execute("PRAGMA integrity_check").fetchone()[0]
        return {"ok": r == "ok", "detail": r}

    # ------------------------------------------------------------ provedores / cotas
    def provider(self, name: str) -> dict:
        p = self._one("SELECT * FROM providers WHERE name=?", (name,))
        if not p:
            cfg = dict(quota.PROVIDER_DEFAULTS.get("colab"))
            self.c.execute("INSERT INTO providers(name,config) VALUES(?,?)", (name, json.dumps(cfg)))
            p = self._one("SELECT * FROM providers WHERE name=?", (name,))
        p["cfg"] = json.loads(p["config"])
        return p

    def set_provider_config(self, name: str, **cfg) -> None:
        with self.lock:
            p = self.provider(name)
            p["cfg"].update(cfg)
            self.c.execute("UPDATE providers SET config=? WHERE name=?", (json.dumps(p["cfg"]), name))

    def pause_provider(self, name: str, seconds: float, why: str = "") -> None:
        with self.lock:
            self.provider(name)
            self.c.execute("UPDATE providers SET paused_until=? WHERE name=?", (self.clock() + seconds, name))
            self.event("warn", "quota", f"provedor {name} pausado por {seconds:.0f}s: {why}")

    def usage_of(self, provider: str, now: float | None = None) -> dict:
        now = now or self.clock()
        pk = quota.period_keys(now)
        out = {}
        for per in ("day", "week"):
            r = self._one("SELECT used FROM usage WHERE provider=? AND period=? AND unit='seconds'", (provider, pk[per]))
            out[per] = r["used"] if r else 0.0
        return out

    def add_usage(self, provider: str, seconds: float, gpu: bool = False, now: float | None = None) -> None:
        if seconds <= 0:
            return
        pk = quota.period_keys(now or self.clock())
        for per in ("day", "week"):
            self.c.execute("INSERT INTO usage(provider,period,unit,used) VALUES(?,?,?,?) "
                           "ON CONFLICT(provider,period,unit) DO UPDATE SET used=used+excluded.used",
                           (provider, pk[per], "seconds", seconds))

    def _breaker(self, provider: str, ok: bool | None) -> bool:
        p = self.provider(provider)
        now = self.clock()
        st, fl, op, oa, allow = quota.breaker_next(p["breaker"], p["failures"], p["opens"], p["opened_at"], now, ok=ok)
        if (st, fl, op, oa) != (p["breaker"], p["failures"], p["opens"], p["opened_at"]):
            self.c.execute("UPDATE providers SET breaker=?,failures=?,opens=?,opened_at=? WHERE name=?", (st, fl, op, oa, provider))
            if st != p["breaker"]:
                self.event("warn" if st == "open" else "info", "breaker", f"{provider}: {p['breaker']} → {st}")
        if ok is None and p["paused_until"] > now:
            return False
        return allow

    def provider_ok(self, provider: str) -> bool:
        with self.lock:
            return self._breaker(provider, None)

    # ------------------------------------------------------------ executores
    def register(self, executor_id: str, provider: str, caps: dict | None = None, session_seconds: float | None = None) -> dict:
        caps = caps or {}
        now = self.clock()
        with self.lock:
            self.provider(provider)
            dl = now + session_seconds if session_seconds else None
            self.c.execute(
                "INSERT INTO executors(id,provider,caps,status,registered,last_seen,session_deadline) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,caps=excluded.caps,status='active',"
                "last_seen=excluded.last_seen,session_deadline=excluded.session_deadline",
                (executor_id, provider, json.dumps(caps), "active", now, now, dl))
            self.event("info", "executor", f"registrado {executor_id} ({provider})")
            return {"executor": executor_id, "lease_s": LEASE_S}

    def heartbeat(self, executor_id: str, used_seconds: float = 0.0) -> dict:
        now = self.clock()
        with self.lock:
            ex = self._one("SELECT * FROM executors WHERE id=?", (executor_id,))
            if not ex:
                raise KeyError("executor não registrado")
            self.c.execute("UPDATE executors SET last_seen=?, status='active' WHERE id=?", (now, executor_id))
            self.add_usage(ex["provider"], used_seconds, json.loads(ex["caps"]).get("gpu", False))
            return {"ok": True, "now": now}

    def deregister(self, executor_id: str) -> None:
        with self.lock:
            self.c.execute("UPDATE executors SET status='stopped' WHERE id=?", (executor_id,))
            for t in self._q("SELECT id,fence FROM tasks WHERE owner=? AND status='running'", (executor_id,)):
                self._requeue(t["id"], "executor encerrou", penalize=False, outcome="released")

    # ------------------------------------------------------------ tarefas
    def submit(self, kind: str, payload: dict | None = None, *, key: str | None = None, priority: int = 5,
               needs: list[str] | None = None, prefer: list[str] | None = None, est_seconds: float = 60.0,
               run_after: float = 0.0, max_attempts: int = 6) -> dict:
        """Idempotente por `key`: reenviar a mesma chave devolve a tarefa existente (sem duplicar)."""
        now = self.clock()
        tid = uuid.uuid4().hex[:16]
        with self.lock:
            if key:
                ex = self._one("SELECT * FROM tasks WHERE key=?", (key,))
                if ex:
                    return ex | {"duplicate": True}
            self.c.execute(
                "INSERT INTO tasks(id,kind,key,payload,priority,needs,prefer,est_seconds,run_after,max_attempts,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, kind, key, json.dumps(payload or {}), priority, json.dumps(needs or []), json.dumps(prefer or []),
                 est_seconds, run_after or 0, max_attempts, now, now))
            return self._one("SELECT * FROM tasks WHERE id=?", (tid,))

    def get_task(self, tid: str) -> dict | None:
        with self.lock:
            return self._one("SELECT * FROM tasks WHERE id=?", (tid,))

    def claim(self, executor_id: str, time_left: float | None = None) -> dict | None:
        """Atribui NO MÁXIMO uma tarefa; atômico. Nenhum executor pega tarefa já atribuída."""
        now = self.clock()
        with self.lock:
            ex = self._one("SELECT * FROM executors WHERE id=?", (executor_id,))
            if not ex or ex["status"] != "active":
                return None
            caps = json.loads(ex["caps"])
            prov = ex["provider"]
            if not self._breaker(prov, None):
                return None
            p = self.provider(prov)
            if time_left is None and ex["session_deadline"]:
                time_left = ex["session_deadline"] - now
            usage = self.usage_of(prov, now)
            kinds = caps.get("kinds")
            for t in self._q("SELECT * FROM tasks WHERE status='queued' AND run_after<=? ORDER BY priority DESC, created ASC LIMIT 50", (now,)):
                if kinds is not None and t["kind"] not in kinds:
                    continue
                needs = json.loads(t["needs"])
                if "gpu" in needs and not caps.get("gpu"):
                    continue
                pref = json.loads(t["prefer"])
                if pref and prov not in pref and now - t["created"] < PREFER_WAIT_S:
                    continue                                   # preferência suave: espera o provedor preferido
                d = quota.decide(p["cfg"], usage, t["est_seconds"], t["priority"], "gpu" in needs, time_left)
                if not d.ok:
                    continue
                row = self._one(
                    "UPDATE tasks SET status='running', owner=?, fence=fence+1, lease_until=?, attempts=attempts+1, updated=? "
                    "WHERE id=? AND status='queued' RETURNING *", (executor_id, now + LEASE_S, now, t["id"]))
                if row:
                    self.c.execute("INSERT INTO history(task_id,executor,provider,fence,outcome,ts) VALUES(?,?,?,?,?,?)",
                                   (row["id"], executor_id, prov, row["fence"], "claimed", now))
                    row["uncertain_quota"] = d.uncertain
                    return row
            return None

    def _own(self, tid: str, executor_id: str, fence: int) -> dict:
        t = self._one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t or t["status"] != "running" or t["owner"] != executor_id or t["fence"] != fence:
            raise Fenced(f"{executor_id} não é mais dono de {tid} (fence {fence})")
        return t

    def renew(self, tid: str, executor_id: str, fence: int) -> float:
        now = self.clock()
        with self.lock:
            self._own(tid, executor_id, fence)
            self.c.execute("UPDATE tasks SET lease_until=?, updated=? WHERE id=?", (now + LEASE_S, now, tid))
            return now + LEASE_S

    def save_checkpoint(self, tid: str, executor_id: str, fence: int, data: dict) -> None:
        raw = json.dumps(data)
        if len(raw) > 200_000:
            raise ValueError("checkpoint grande demais: guarde o conteúdo no Drive e referencie por hash")
        now = self.clock()
        with self.lock:
            self._own(tid, executor_id, fence)
            self.c.execute("UPDATE tasks SET checkpoint=?, lease_until=?, updated=? WHERE id=?", (raw, now + LEASE_S, now, tid))

    def complete(self, tid: str, executor_id: str, fence: int, result: dict | None = None, seconds: float = 0.0) -> None:
        now = self.clock()
        with self.lock:
            t = self._own(tid, executor_id, fence)
            self.c.execute("UPDATE tasks SET status='done', result=?, lease_until=NULL, error=NULL, updated=? WHERE id=?",
                           (json.dumps(result or {}), now, tid))
            prov = self._one("SELECT provider FROM executors WHERE id=?", (executor_id,))["provider"]
            self.c.execute("INSERT INTO history(task_id,executor,provider,fence,outcome,ts,seconds) VALUES(?,?,?,?,?,?,?)",
                           (tid, executor_id, prov, fence, "done", now, seconds))
            self._breaker(prov, True)

    def fail(self, tid: str, executor_id: str, fence: int, error: str, *, kind: str = "transient",
             retry_after: float | None = None) -> str:
        """kind: transient (tarefa/erro do conteúdo) | provider (falha da nuvem → conta no breaker) |
        quota (pausa o provedor) | permanent (vai direto para dead)."""
        with self.lock:
            t = self._own(tid, executor_id, fence)
            prov = self._one("SELECT provider FROM executors WHERE id=?", (executor_id,))["provider"]
            if kind == "provider":
                self._breaker(prov, False)
            if kind == "quota":
                self.pause_provider(prov, retry_after or 3600, f"cota esgotada: {error[:80]}")
            if kind == "permanent" or t["attempts"] >= t["max_attempts"]:
                self.c.execute("UPDATE tasks SET status='dead', error=?, owner=NULL, lease_until=NULL, updated=? WHERE id=?",
                               (error[:500], self.clock(), tid))
                self.c.execute("INSERT INTO history(task_id,executor,provider,fence,outcome,ts,note) VALUES(?,?,?,?,?,?,?)",
                               (tid, executor_id, prov, fence, "dead", self.clock(), error[:200]))
                return "dead"
            delay = retry_after if retry_after is not None else backoff_delay(t["attempts"], 30.0, 3600.0)
            self._requeue(tid, error, penalize=True, delay=delay, outcome="failed", executor=executor_id, provider=prov, fence=fence)
            return "queued"

    def release(self, tid: str, executor_id: str, fence: int, note: str = "liberada") -> None:
        """Devolve a tarefa sem penalidade (ex.: sessão acabando). Mantém o checkpoint."""
        with self.lock:
            self._own(tid, executor_id, fence)
            self._requeue(tid, note, penalize=False, outcome="released", executor=executor_id, fence=fence)

    def _requeue(self, tid, why, *, penalize, delay=0.0, outcome="requeued", executor=None, provider=None, fence=None):
        now = self.clock()
        self.c.execute(
            "UPDATE tasks SET status='queued', owner=NULL, lease_until=NULL, error=?, run_after=?, "
            "attempts=CASE WHEN ? THEN attempts ELSE MAX(0,attempts-1) END, updated=? WHERE id=?",
            (why[:300], now + delay, 1 if penalize else 0, now, tid))
        self.c.execute("INSERT INTO history(task_id,executor,provider,fence,outcome,ts,note) VALUES(?,?,?,?,?,?,?)",
                       (tid, executor, provider, fence, outcome, now, why[:200]))

    def cancel_if_queued(self, key: str) -> bool:
        """Cancela por chave SOMENTE se ainda estiver na fila (tarefa em execução nunca é cancelada à força)."""
        with self.lock:
            r = self._one("UPDATE tasks SET status='cancelled', updated=? WHERE key=? AND status='queued' RETURNING id", (self.clock(), key))
            return bool(r)

    def get_by_key(self, key: str) -> dict | None:
        with self.lock:
            return self._one("SELECT * FROM tasks WHERE key=?", (key,))

    def retry_dead(self, tid: str) -> None:
        with self.lock:
            self.c.execute("UPDATE tasks SET status='queued', attempts=0, error=NULL, run_after=0 WHERE id=? AND status='dead'", (tid,))

    # ------------------------------------------------------------ recuperação
    def reap(self) -> dict:
        """Leases vencidos voltam à fila (conta tentativa); executores mudos viram 'lost'."""
        now = self.clock()
        out = {"requeued": [], "dead": [], "lost_executors": []}
        with self.lock:
            for ex in self._q("SELECT id FROM executors WHERE status='active' AND last_seen<?", (now - EXECUTOR_TIMEOUT_S,)):
                self.c.execute("UPDATE executors SET status='lost' WHERE id=?", (ex["id"],))
                out["lost_executors"].append(ex["id"])
                self.event("warn", "executor", f"{ex['id']} sem heartbeat: marcado como perdido")
            for t in self._q("SELECT * FROM tasks WHERE status='running' AND lease_until<?", (now,)):
                if t["attempts"] >= t["max_attempts"]:
                    self.c.execute("UPDATE tasks SET status='dead', error='lease expirou (limite de tentativas)', owner=NULL WHERE id=?", (t["id"],))
                    out["dead"].append(t["id"])
                else:
                    self._requeue(t["id"], "lease expirou", penalize=True, delay=5.0, outcome="lease_expired", executor=t["owner"], fence=t["fence"])
                    out["requeued"].append(t["id"])
            for lk in self._q("SELECT * FROM locks WHERE owner IS NOT NULL AND lease_until<?", (now,)):
                self.c.execute("UPDATE locks SET owner=NULL WHERE name=?", (lk["name"],))
        return out

    def reconcile(self) -> dict:
        """Detecta e repara inconsistências SEGURAS; o resto é só reportado."""
        now = self.clock()
        rep = {"fixed": [], "reported": []}
        with self.lock:
            for t in self._q("SELECT * FROM tasks WHERE status='running' AND (owner IS NULL OR lease_until IS NULL)"):
                self._requeue(t["id"], "estado inconsistente (running sem dono/lease)", penalize=False, outcome="reconciled")
                rep["fixed"].append(f"{t['id']}: running sem dono → fila")
            for t in self._q("SELECT * FROM tasks WHERE status='queued' AND owner IS NOT NULL"):
                self.c.execute("UPDATE tasks SET owner=NULL, lease_until=NULL WHERE id=?", (t["id"],))
                rep["fixed"].append(f"{t['id']}: fila com dono residual limpo")
            for t in self._q("SELECT * FROM tasks WHERE status='running' AND owner NOT IN (SELECT id FROM executors WHERE status='active')"):
                self.c.execute("UPDATE tasks SET lease_until=? WHERE id=?", (min(t["lease_until"] or now, now), t["id"]))
                rep["fixed"].append(f"{t['id']}: dono inativo → lease expira já")
            for t in self._q("SELECT id FROM tasks WHERE status='done' AND (result IS NULL OR result='')"):
                rep["reported"].append(f"{t['id']}: concluída sem resultado")
            n = self._one("SELECT count(*) n FROM tasks WHERE status='running' AND fence<1")["n"]
            if n:
                rep["reported"].append(f"{n} tarefas running com fence<1")
            integ = self.integrity()
            if not integ["ok"]:
                rep["reported"].append("integrity_check falhou: " + integ["detail"])
        if rep["fixed"]:
            self.event("warn", "reconcile", f"{len(rep['fixed'])} correções")
        return rep

    # ------------------------------------------------------------ locks distribuídos (com fencing)
    def lock_acquire(self, name: str, owner: str, ttl: float = 120.0) -> int | None:
        now = self.clock()
        with self.lock:
            self.c.execute("INSERT OR IGNORE INTO locks(name,owner,fence,lease_until) VALUES(?,NULL,0,0)", (name,))
            row = self._one("UPDATE locks SET owner=?, fence=fence+1, lease_until=? "
                            "WHERE name=? AND (owner IS NULL OR lease_until<? ) RETURNING fence", (owner, now + ttl, name, now))
            if row:
                return row["fence"]
            cur = self._one("SELECT * FROM locks WHERE name=?", (name,))
            if cur["owner"] == owner:                          # reentrada do mesmo dono: renova sem novo fence
                self.c.execute("UPDATE locks SET lease_until=? WHERE name=?", (now + ttl, name))
                return cur["fence"]
            return None

    def lock_renew(self, name: str, owner: str, fence: int, ttl: float = 120.0) -> None:
        now = self.clock()
        with self.lock:
            cur = self._one("SELECT * FROM locks WHERE name=?", (name,))
            if not cur or cur["owner"] != owner or cur["fence"] != fence or cur["lease_until"] < now:
                raise Fenced(f"lock {name} perdido por {owner}")
            self.c.execute("UPDATE locks SET lease_until=? WHERE name=?", (now + ttl, name))

    def lock_check(self, name: str, owner: str, fence: int) -> bool:
        with self.lock:
            cur = self._one("SELECT * FROM locks WHERE name=?", (name,))
            return bool(cur and cur["owner"] == owner and cur["fence"] == fence and cur["lease_until"] >= self.clock())

    def lock_release(self, name: str, owner: str, fence: int) -> None:
        with self.lock:
            self.c.execute("UPDATE locks SET owner=NULL, lease_until=0 WHERE name=? AND owner=? AND fence=?", (name, owner, fence))

    # ------------------------------------------------------------ visão geral
    def counts(self) -> dict:
        with self.lock:
            return {r["status"]: r["n"] for r in self._q("SELECT status, count(*) n FROM tasks GROUP BY status")}

    def state(self) -> dict:
        now = self.clock()
        with self.lock:
            provs = []
            for p in self._q("SELECT * FROM providers ORDER BY name"):
                cfg = json.loads(p["config"])
                u = self.usage_of(p["name"], now)
                provs.append({"name": p["name"], "breaker": p["breaker"], "failures": p["failures"],
                              "paused": p["paused_until"] > now, "usage_s": u, "config": cfg, "uncertain": not cfg.get("known", False),
                              "last_error": p["last_error"]})
            exs = self._q("SELECT id,provider,status,last_seen,session_deadline FROM executors ORDER BY last_seen DESC LIMIT 30")
            for e in exs:
                e["seen_ago_s"] = round(now - (e["last_seen"] or now))
                e["time_left_s"] = round(e["session_deadline"] - now) if e["session_deadline"] else None
            tasks = self._q("SELECT id,kind,status,priority,owner,fence,attempts,lease_until,error,updated FROM tasks "
                            "WHERE status IN ('running','queued','dead') ORDER BY priority DESC, created LIMIT 60")
            return {"now": now, "counts": self.counts(), "providers": provs, "executors": exs, "tasks": tasks,
                    "locks": self._q("SELECT name,owner,fence,lease_until FROM locks"),
                    "events": self._q("SELECT ts,level,component,message FROM events ORDER BY id DESC LIMIT 40"),
                    "history": self._q("SELECT task_id,executor,provider,outcome,ts,seconds,note FROM history ORDER BY id DESC LIMIT 40")}
