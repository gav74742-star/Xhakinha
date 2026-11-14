"""Executor genérico (Kaggle/Colab/GitHub/local): registra, envia heartbeat, pega UMA tarefa por vez,
renova o lease em segundo plano, executa o handler e devolve o resultado. Se perder o lease (Fenced),
o handler é sinalizado e NADA que ele fizer depois é confirmado pelo coordenador."""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import redact
from ..retry import NotReadyYet, PermanentError, QuotaExceeded, TransientError
from .coordinator import Fenced

log = logging.getLogger("xh.executor")


@dataclass
class TaskContext:
    task: dict
    executor_id: str
    client: object
    drive: object
    workdir: Path
    cancel: threading.Event = field(default_factory=threading.Event)
    payload: dict = field(default_factory=dict)
    checkpoint: dict = field(default_factory=dict)

    @property
    def fence(self) -> int:
        return self.task["fence"]

    def check(self) -> None:
        if self.cancel.is_set():
            raise Fenced("tarefa cancelada (lease perdido ou sessão acabando)")

    def save_checkpoint(self, data: dict) -> None:
        self.check()
        self.client.checkpoint(self.task["id"], self.executor_id, self.fence, data)


class Executor:
    def __init__(self, executor_id: str, provider: str, client, drive, handlers: dict[str, Callable[[TaskContext], dict]], *,
                 workdir: Path, caps: dict | None = None, session_seconds: float | None = None,
                 heartbeat_s: float = 30.0, poll_s: float = 15.0, clock=time.time, sleep=time.sleep):
        self.id, self.provider, self.client, self.drive = executor_id, provider, client, drive
        self.handlers = handlers
        self.workdir = Path(workdir)
        self.caps = {"kinds": sorted(handlers), **(caps or {})}
        self.session_seconds = session_seconds
        self.heartbeat_s, self.poll_s, self.clock, self.sleep = heartbeat_s, poll_s, clock, sleep
        self.started = clock()
        self.stop = threading.Event()
        self.current: TaskContext | None = None
        self._hb: threading.Thread | None = None
        self._last_hb = clock()
        self.stats = {"done": 0, "failed": 0, "fenced": 0, "released": 0}

    def time_left(self) -> float | None:
        return None if self.session_seconds is None else self.session_seconds - (self.clock() - self.started)

    def start(self) -> None:
        self.client.register(self.id, self.provider, self.caps, self.session_seconds)
        self._hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb.start()

    def _heartbeat_loop(self) -> None:
        while not self.stop.wait(self.heartbeat_s):
            self.beat()

    def beat(self) -> None:
        now = self.clock()
        used, self._last_hb = now - self._last_hb, now
        try:
            self.client.heartbeat(self.id, used)
            cur = self.current
            if cur and not cur.cancel.is_set():
                self.client.renew(cur.task["id"], self.id, cur.fence)
        except Fenced:
            if self.current:
                self.current.cancel.set()
        except Exception as e:                               # coordenador fora: continua trabalhando, tenta no próximo beat
            log.warning("heartbeat falhou: %s", redact(str(e)))

    def run_one(self) -> bool:
        """Tenta pegar e executar uma tarefa. True se havia tarefa."""
        t = self.client.claim(self.id, self.time_left())
        if not t:
            return False
        import json
        ctx = TaskContext(task=t, executor_id=self.id, client=self.client, drive=self.drive,
                          workdir=self.workdir / t["id"], payload=json.loads(t["payload"]),
                          checkpoint=json.loads(t["checkpoint"]) if t.get("checkpoint") else {})
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        self.current = ctx
        t0 = self.clock()
        try:
            h = self.handlers.get(t["kind"])
            if not h:
                raise PermanentError(f"sem handler para '{t['kind']}'")
            result = h(ctx)
            ctx.check()
            self.client.complete(t["id"], self.id, ctx.fence, result or {}, self.clock() - t0)
            self.stats["done"] += 1
        except Fenced:
            self.stats["fenced"] += 1                          # outro executor assumiu: descartamos tudo em silêncio
        except NotReadyYet as e:
            self._safe(lambda: self.client.release(t["id"], self.id, ctx.fence, "não pronto: " + str(e)))
            self.stats["released"] += 1
        except QuotaExceeded as e:
            self._safe(lambda: self.client.fail(t["id"], self.id, ctx.fence, str(e), kind="quota", retry_after=e.retry_after))
            self.stats["failed"] += 1
        except PermanentError as e:
            self._safe(lambda: self.client.fail(t["id"], self.id, ctx.fence, str(e), kind="permanent"))
            self.stats["failed"] += 1
        except TransientError as e:
            self._safe(lambda: self.client.fail(t["id"], self.id, ctx.fence, str(e), kind="provider", retry_after=e.retry_after))
            self.stats["failed"] += 1
        except Exception as e:
            log.error("erro na tarefa %s: %s", t["id"], redact(traceback.format_exc()))
            self._safe(lambda: self.client.fail(t["id"], self.id, ctx.fence, f"{type(e).__name__}: {e}"))
            self.stats["failed"] += 1
        finally:
            self.current = None
        return True

    def _safe(self, fn) -> None:
        try:
            fn()
        except Fenced:
            self.stats["fenced"] += 1
        except Exception as e:
            log.warning("falha ao reportar: %s", redact(str(e)))

    def run(self, *, stop_margin_s: float = 300.0, idle_exit_s: float | None = None) -> dict:
        """Loop principal até o fim da sessão (com margem) ou `stop`. Não pega tarefa que não caiba (o
        coordenador também recusa via cota de sessão)."""
        self.start()
        idle_since = None
        try:
            while not self.stop.is_set():
                tl = self.time_left()
                if tl is not None and tl <= stop_margin_s:
                    break
                if self.run_one():
                    idle_since = None
                    continue
                idle_since = idle_since or self.clock()
                if idle_exit_s is not None and self.clock() - idle_since >= idle_exit_s:
                    break
                self.sleep(self.poll_s)
        finally:
            self.shutdown()
        return self.stats

    def shutdown(self) -> None:
        self.stop.set()
        cur = self.current
        if cur:
            cur.cancel.set()
            self._safe(lambda: self.client.release(cur.task["id"], self.id, cur.fence, "sessão encerrando"))
        self._safe(lambda: self.client.deregister(self.id))
