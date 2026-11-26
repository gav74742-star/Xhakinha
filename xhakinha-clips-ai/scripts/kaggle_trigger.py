from __future__ import annotations

import ast
import json
import os
import subprocess
import uuid
from pathlib import Path

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
        (item for item in state.get("providers", []) if item.get("name") == "kaggle-gpu"),
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
        if prefer and "kaggle-gpu" not in prefer and now - created < PREFER_WAIT_S:
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


def _cell_source(cell: dict) -> str:
    source = cell.get("source", "")
    if isinstance(source, list):
        return "".join(source)
    return str(source)


def _main_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "main"
    ]


def validate_kaggle_payload(root: Path | str = Path("kaggle")) -> dict:
    """Validate the authorized Kaggle payload without enforcing a cell count.

    The only execution gate is remote D1 authorization in the last code cell.
    It guards the sole main() invocation in the else branch and rechecks the
    B2/Turso/15B/15C/15D prerequisites before allowing startup.
    """
    root = Path(root)
    try:
        metadata = json.loads((root / "kernel-metadata.json").read_text(encoding="utf-8"))
        notebook = json.loads((root / "kaggle_worker.ipynb").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"BLOQUEADO: payload Kaggle inválido: {type(exc).__name__}: {exc}") from exc

    if metadata.get("id") != "GAv7489/xhakinha-worker":
        raise SystemExit("BLOQUEADO: identificador Kaggle incorreto.")
    if metadata.get("code_file") != "kaggle_worker.ipynb":
        raise SystemExit("BLOQUEADO: arquivo principal incorreto.")
    if notebook.get("nbformat") != 4 or not isinstance(notebook.get("cells"), list):
        raise SystemExit("BLOQUEADO: notebook não está no formato nbformat 4.")

    code_cells = []
    trees = []
    for index, cell in enumerate(notebook["cells"], start=1):
        cell_type = cell.get("cell_type")
        if cell_type not in ("code", "markdown"):
            raise SystemExit(f"BLOQUEADO: tipo de célula não suportado na posição {index}.")
        if cell_type != "code":
            continue
        source = _cell_source(cell)
        try:
            tree = ast.parse(source, filename=f"kaggle-cell-{len(code_cells) + 1}")
        except SyntaxError as exc:
            raise SystemExit(
                f"BLOQUEADO: sintaxe inválida na célula de código {len(code_cells) + 1}: {exc}"
            ) from exc
        code_cells.append(source)
        trees.append(tree)

    if not code_cells:
        raise SystemExit("BLOQUEADO: notebook não contém células de código.")

    last_source = code_cells[-1]
    if "INICIAR_KAGGLE_WORKER" in last_source:
        raise SystemExit("BLOQUEADO: gate manual obsoleto.")
    required_markers = (
        'AUTORIZACAO_REMOTA = remote_state.get("launch_enabled") is True',
        'urllib.request.urlopen(req, timeout=20)',
        'coord_url + "/api/state"',
        "if not AUTORIZACAO_REMOTA:",
        'XH.get("etapa15_preflight_ok") is not True',
        'XH.get("etapa15c_ok") is not True',
        'XH.get("snapshot_bootstrap_ok") is not True',
        'XH.get("sources_check_ok") is not True',
        'XH.get("etapa15d_ok") is not True',
        'os.environ.get("XH_STORAGE_BACKEND", "").lower() != "b2"',
        'os.environ.get("XH_STATE_BACKEND", "").lower() != "turso"',
    )
    missing = [marker for marker in required_markers if marker not in last_source]
    if missing:
        raise SystemExit("BLOQUEADO: gate de inicialização incompleto: " + "; ".join(missing))

    all_main_calls = [call for tree in trees for call in _main_calls(tree)]
    if len(all_main_calls) != 1:
        raise SystemExit(
            f"BLOQUEADO: esperado exatamente um main() protegido; encontrados {len(all_main_calls)}."
        )
    if not _main_calls(trees[-1]):
        raise SystemExit("BLOQUEADO: main() precisa estar somente no último bloco de código.")

    guard = next(
        (node for node in trees[-1].body
         if isinstance(node, ast.If)
         and isinstance(node.test, ast.UnaryOp)
         and isinstance(node.test.op, ast.Not)
         and isinstance(node.test.operand, ast.Name)
         and node.test.operand.id == "AUTORIZACAO_REMOTA"),
        None,
    )
    if guard is None:
        raise SystemExit("BLOQUEADO: não foi encontrado o gate remoto D1 de inicialização.")
    if _main_calls(ast.Module(body=guard.body, type_ignores=[])):
        raise SystemExit("BLOQUEADO: main() aparece no ramo que deveria bloquear a inicialização.")
    if len(_main_calls(ast.Module(body=guard.orelse, type_ignores=[]))) != 1:
        raise SystemExit("BLOQUEADO: main() não está exclusivamente no ramo autorizado do gate.")

    info = {
        "physical_cells": len(notebook["cells"]),
        "code_cells": len(code_cells),
        "main_calls": len(all_main_calls),
        "remote_gate_validated": 'AUTORIZACAO_REMOTA = remote_state.get("launch_enabled") is True' in last_source,
    }
    print(
        "Notebook autorizado validado: "
        f"{info['physical_cells']} células físicas, "
        f"{info['code_cells']} células de código; gate remoto D1 validado."
    )
    return info


def main() -> int:
    base = os.environ["XH_COORD_URL"].rstrip("/")
    headers = {
        "Authorization": "Bearer " + os.environ["XH_ADMIN_TOKEN"],
        "X-Requested-With": "XHK",
    }
    response = requests.get(base + "/api/state", timeout=30, headers=headers)
    response.raise_for_status()

    state = response.json()
    if state.get("launch_enabled") is not True:
        print("DISPARO BLOQUEADO: autorização Kaggle desativada no D1.")
        return 0

    ok, reason = decide(state)
    print("decisão:", ok, "-", reason)
    if not ok:
        return 0

    payload = validate_kaggle_payload()
    if not payload["remote_gate_validated"]:
        print("DISPARO BLOQUEADO: notebook sem gate remoto validado.")
        return 0
    owner = (
        f"github-actions:{os.environ.get('GITHUB_RUN_ID', 'local')}:"
        f"{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}:{os.getpid()}:{uuid.uuid4().hex}"
    )
    lock_name = "kaggle-launch"
    acquired = requests.post(
        base + "/api/exec/lock/acquire",
        headers=headers,
        json={"name": lock_name, "owner": owner, "ttl": 900},
        timeout=30,
    )
    acquired.raise_for_status()
    fence = acquired.json().get("fence")
    if fence is None:
        print("DISPARO BLOQUEADO: outro processo possui kaggle-launch.")
        return 0

    try:
        response = requests.get(base + "/api/state", timeout=30, headers=headers)
        response.raise_for_status()
        locked_state = response.json()

        if locked_state.get("launch_enabled") is not True:
            print("DISPARO BLOQUEADO: autorização revogada antes do envio.")
            return 0

        still_ok, still_reason = decide(locked_state)
        print("revalidação com bloqueio:", still_ok, "-", still_reason)
        if not still_ok:
            return 0

        payload = validate_kaggle_payload()
        if not payload["remote_gate_validated"]:
            print("DISPARO BLOQUEADO: gate remoto não validado.")
            return 0

        return subprocess.call(["kaggle", "kernels", "push", "-p", "kaggle"])
    finally:
        try:
            released = requests.post(
                base + "/api/exec/lock/release",
                headers=headers,
                json={"name": lock_name, "owner": owner, "fence": fence},
                timeout=20,
            )
            released.raise_for_status()
            print("Bloqueio kaggle-launch liberado.")
        except requests.RequestException as exc:
            print("AVISO: não foi possível confirmar a liberação do bloqueio:", exc)


if __name__ == "__main__":
    raise SystemExit(main())
