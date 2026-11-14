"""API HTTP do coordenador (Starlette). Auto-hospedável; o Worker Cloudflare (cloudflare/worker.js) expõe a MESMA API."""
from __future__ import annotations

import hmac
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .coordinator import Coordinator, Fenced

DASH = Path(__file__).resolve().parents[2] / "dashboard"


def create_app(coord: Coordinator, exec_token: str, admin_token: str) -> Starlette:
    def auth(req: Request, admin=False) -> bool:
        got = req.headers.get("authorization", "")[7:]
        ok = hmac.compare_digest(got, admin_token) or (not admin and hmac.compare_digest(got, exec_token))
        return ok and req.headers.get("x-requested-with") == "XHK"

    def guard(fn, admin=False):
        async def h(req: Request):
            if not auth(req, admin):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            body = {}
            if req.method == "POST":
                try:
                    body = await req.json()
                except Exception:
                    return JSONResponse({"error": "json inválido"}, status_code=400)
            try:
                coord.reap()
                return JSONResponse(fn(req, body))
            except Fenced as e:
                return JSONResponse({"error": "fenced", "detail": str(e)}, status_code=409)
            except KeyError as e:
                return JSONResponse({"error": "not_found", "detail": str(e)}, status_code=404)
            except ValueError as e:
                return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=400)
        return h

    R = {
        "register": lambda r, b: coord.register(b["executor"], b["provider"], b.get("caps"), b.get("session_seconds")),
        "heartbeat": lambda r, b: coord.heartbeat(b["executor"], float(b.get("used_seconds") or 0)),
        "claim": lambda r, b: {"task": coord.claim(b["executor"], b.get("time_left"))},
        "renew": lambda r, b: {"lease_until": coord.renew(b["task"], b["executor"], b["fence"])},
        "checkpoint": lambda r, b: (coord.save_checkpoint(b["task"], b["executor"], b["fence"], b["data"]), {"ok": True})[1],
        "complete": lambda r, b: (coord.complete(b["task"], b["executor"], b["fence"], b.get("result"), float(b.get("seconds") or 0)), {"ok": True})[1],
        "fail": lambda r, b: {"status": coord.fail(b["task"], b["executor"], b["fence"], b["error"], kind=b.get("kind", "transient"), retry_after=b.get("retry_after"))},
        "release": lambda r, b: (coord.release(b["task"], b["executor"], b["fence"], b.get("note", "")), {"ok": True})[1],
        "deregister": lambda r, b: (coord.deregister(b["executor"]), {"ok": True})[1],
        "submit": lambda r, b: coord.submit(b["kind"], b.get("payload"), **{k: v for k, v in b.items() if k in
                    ("key", "priority", "needs", "prefer", "est_seconds", "run_after", "max_attempts")}),
        "bykey": lambda r, b: {"task": coord.get_by_key(b["key"])},
        "cancel": lambda r, b: {"cancelled": coord.cancel_if_queued(b["key"])},
        "lock/acquire": lambda r, b: {"fence": coord.lock_acquire(b["name"], b["owner"], float(b.get("ttl", 120)))},
        "lock/renew": lambda r, b: (coord.lock_renew(b["name"], b["owner"], b["fence"], float(b.get("ttl", 120))), {"ok": True})[1],
        "lock/release": lambda r, b: (coord.lock_release(b["name"], b["owner"], b["fence"]), {"ok": True})[1],
    }
    routes = [Route(f"/api/exec/{k}", guard(f), methods=["POST"]) for k, f in R.items()]
    routes.append(Route("/api/exec/task/{tid}", guard(lambda r, b: coord.get_task(r.path_params["tid"]) or (_ for _ in ()).throw(KeyError("tarefa"))), methods=["GET"]))
    routes.append(Route("/api/state", guard(lambda r, b: coord.state(), admin=False), methods=["GET"]))
    routes.append(Route("/api/admin/retry/{tid}", guard(lambda r, b: (coord.retry_dead(r.path_params["tid"]), {"ok": True})[1], admin=True), methods=["POST"]))
    routes.append(Route("/api/admin/reconcile", guard(lambda r, b: coord.reconcile(), admin=True), methods=["POST"]))
    routes.append(Route("/api/health", lambda r: JSONResponse({"ok": True, **coord.integrity()})))
    routes.append(Route("/", lambda r: FileResponse(DASH / "index.html")))
    return Starlette(routes=routes)
