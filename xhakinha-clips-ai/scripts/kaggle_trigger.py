"""Dispara Kaggle somente quando há tarefa GPU elegível."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import requests

from xhakinha.cloud import quota
from xhakinha.cloud.coordinator import PREFER_WAIT_S


def _decode_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except (TypeError, ValueError):
            return []
    return []


def decide(state: dict) -> tuple[bool, str]:
    now = float(state.get("now", 0))

    brain = next(
        (lock for lock in state.get("locks", []) if lock.get("name") == "brain"),
        None,
    )
    if brain and brain.get("owner") and float(brain.get("lease_until") or 0) > now:
        return False, "cérebro ativo"

    if any(
        executor.get("status") == "active"
        and str(executor.get("provider", "")).startswith("kaggle")
        for executor in state.get("executors", [])
    ):
        return False, "já existe sessão Kaggle ativa"

    provider = next(
        (item for item in state.get("providers", [])
         if item.get("name") == "kaggle-gpu"),
        None,
    )
    if not provider:
        return False, "provedor kaggle-gpu não cadastrado"

    if provider.get("breaker") == "open" or provider.get("paused"):
        return False, "kaggle-gpu indisponível (breaker/pausa)"

    config = provider.get("config") or {}
    usage = provider.get("usage_s") or {}

    for task in state.get("tasks", []):
        if task.get("status") != "queued":
            continue

        if float(task.get("run_after") or 0) > now:
            continue

        needs = _decode_list(task.get("needs"))
        if "gpu" not in needs:
            continue

        prefer = _decode_list(task.get("prefer"))
        created_value = task.get("created")
        created = float(now if created_value is None else created_value)
        if (
            prefer
            and "kaggle-gpu" not in prefer
            and now - created < PREFER_WAIT_S
        ):
            continue

        decision = quota.decide(
            config,
            usage,
            float(task.get("est_seconds") or 0),
            int(task.get("priority") or 0),
            True,
            float(config.get("session_seconds") or 0) or None,
        )
        if decision.ok:
            return True, f"tarefa GPU elegível: {task.get('id', 'sem-id')}"

    return False, "nenhuma tarefa GPU elegível na fila"


def main() -> int:
    base = os.environ["XH_COORD_URL"].rstrip("/")
    headers = {
        "Authorization": "Bearer " + os.environ["XH_ADMIN_TOKEN"],
        "X-Requested-With": "XHK",
    }

    recovery = requests.post(
        base + "/api/admin/reap", json={}, timeout=30, headers=headers
    )
    recovery.raise_for_status()
    print("recuperação:", recovery.json())

    response = requests.get(base + "/api/state", timeout=30, headers=headers)
    response.raise_for_status()

    ok, reason = decide(response.json())
    print("decisão:", ok, "-", reason)
    if not ok:
        return 0

    return subprocess.call(["kaggle", "kernels", "push", "-p", "kaggle"])


if __name__ == "__main__":
    sys.exit(main())
