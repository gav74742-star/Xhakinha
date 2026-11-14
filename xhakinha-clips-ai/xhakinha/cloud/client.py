"""Clientes do coordenador: em processo (testes/self-host) e HTTP (Kaggle/Colab/GitHub → Worker ou API)."""
from __future__ import annotations

import time

import requests

from ..retry import TransientError, call_with_retry
from .coordinator import Coordinator, Fenced


class LocalClient:
    """Fala direto com um Coordinator em memória (mesma interface do HttpClient)."""

    def __init__(self, coord: Coordinator):
        self.c = coord

    def register(self, executor_id, provider, caps=None, session_seconds=None):
        return self.c.register(executor_id, provider, caps, session_seconds)

    def heartbeat(self, executor_id, used_seconds=0.0):
        return self.c.heartbeat(executor_id, used_seconds)

    def claim(self, executor_id, time_left=None):
        return self.c.claim(executor_id, time_left)

    def renew(self, tid, ex, fence): return self.c.renew(tid, ex, fence)
    def checkpoint(self, tid, ex, fence, data): return self.c.save_checkpoint(tid, ex, fence, data)
    def complete(self, tid, ex, fence, result=None, seconds=0.0): return self.c.complete(tid, ex, fence, result, seconds)
    def fail(self, tid, ex, fence, error, kind="transient", retry_after=None): return self.c.fail(tid, ex, fence, error, kind=kind, retry_after=retry_after)
    def release(self, tid, ex, fence, note="liberada"): return self.c.release(tid, ex, fence, note)
    def submit(self, kind, payload=None, **kw): return self.c.submit(kind, payload, **kw)
    def get_task(self, tid): return self.c.get_task(tid)
    def get_by_key(self, key): return self.c.get_by_key(key)
    def cancel_if_queued(self, key): return self.c.cancel_if_queued(key)
    def deregister(self, ex): return self.c.deregister(ex)
    def lock_acquire(self, name, owner, ttl=120.0): return self.c.lock_acquire(name, owner, ttl)
    def lock_renew(self, name, owner, fence, ttl=120.0): return self.c.lock_renew(name, owner, fence, ttl)
    def lock_release(self, name, owner, fence): return self.c.lock_release(name, owner, fence)
    def state(self): return self.c.state()


class HttpClient:
    def __init__(self, base: str, token: str, timeout: float = 30.0):
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        self.s = requests.Session()

    def _call(self, path: str, body: dict | None = None, method: str = "POST") -> dict:
        def go():
            try:
                r = self.s.request(method, self.base + path, json=body, timeout=self.timeout,
                                   headers={"Authorization": "Bearer " + self.token, "X-Requested-With": "XHK"})
            except requests.RequestException as e:
                raise TransientError(f"coordenador inacessível: {e}")
            if r.status_code == 409:
                raise Fenced(r.json().get("detail", "fenced"))
            if r.status_code >= 500 or r.status_code == 429:
                raise TransientError(f"coordenador {r.status_code}")
            if r.status_code >= 400:
                raise RuntimeError(f"coordenador {r.status_code}: {r.text[:150]}")
            return r.json()
        return call_with_retry(go, attempts=4, base=1.0, cap=15.0)

    def register(self, executor_id, provider, caps=None, session_seconds=None):
        return self._call("/api/exec/register", {"executor": executor_id, "provider": provider, "caps": caps or {}, "session_seconds": session_seconds})

    def heartbeat(self, executor_id, used_seconds=0.0):
        return self._call("/api/exec/heartbeat", {"executor": executor_id, "used_seconds": used_seconds})

    def claim(self, executor_id, time_left=None):
        return self._call("/api/exec/claim", {"executor": executor_id, "time_left": time_left}).get("task")

    def renew(self, tid, ex, fence): return self._call("/api/exec/renew", {"task": tid, "executor": ex, "fence": fence})
    def checkpoint(self, tid, ex, fence, data): return self._call("/api/exec/checkpoint", {"task": tid, "executor": ex, "fence": fence, "data": data})
    def complete(self, tid, ex, fence, result=None, seconds=0.0): return self._call("/api/exec/complete", {"task": tid, "executor": ex, "fence": fence, "result": result or {}, "seconds": seconds})
    def fail(self, tid, ex, fence, error, kind="transient", retry_after=None): return self._call("/api/exec/fail", {"task": tid, "executor": ex, "fence": fence, "error": error, "kind": kind, "retry_after": retry_after})
    def release(self, tid, ex, fence, note="liberada"): return self._call("/api/exec/release", {"task": tid, "executor": ex, "fence": fence, "note": note})
    def submit(self, kind, payload=None, **kw): return self._call("/api/exec/submit", {"kind": kind, "payload": payload or {}, **kw})
    def get_task(self, tid): return self._call(f"/api/exec/task/{tid}", method="GET")
    def get_by_key(self, key): return self._call("/api/exec/bykey", {"key": key}).get("task")
    def cancel_if_queued(self, key): return self._call("/api/exec/cancel", {"key": key}).get("cancelled", False)
    def deregister(self, ex): return self._call("/api/exec/deregister", {"executor": ex})
    def lock_acquire(self, name, owner, ttl=120.0): return self._call("/api/exec/lock/acquire", {"name": name, "owner": owner, "ttl": ttl}).get("fence")
    def lock_renew(self, name, owner, fence, ttl=120.0): return self._call("/api/exec/lock/renew", {"name": name, "owner": owner, "fence": fence, "ttl": ttl})
    def lock_release(self, name, owner, fence): return self._call("/api/exec/lock/release", {"name": name, "owner": owner, "fence": fence})
    def state(self): return self._call("/api/state", method="GET")
