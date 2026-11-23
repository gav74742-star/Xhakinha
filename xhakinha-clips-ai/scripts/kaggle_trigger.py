"""Decide se vale iniciar uma nova sessão Kaggle e, se sim, faz `kaggle kernels push` (que inicia a execução).
Regras: não inicia se há cérebro ativo, se o Kaggle está com breaker aberto/pausado ou se a cota semanal estimada acabou."""
from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path
import sys

import requests


def decide(state: dict) -> tuple[bool, str]:
    brain = next((l for l in state["locks"] if l["name"] == "brain"), None)
    if brain and brain["owner"] and brain["lease_until"] > state["now"]:
        return False, "cérebro ativo"
    if any(e["status"] == "active" and e["provider"].startswith("kaggle") for e in state["executors"]):
        return False, "já existe sessão Kaggle ativa"
    for p in state["providers"]:
        if p["name"] == "kaggle-gpu":
            if p["breaker"] == "open" or p["paused"]:
                return False, "kaggle-gpu indisponível (breaker/pausa)"
            lim = p["config"].get("week_seconds", 0) * p["config"].get("safety", 0.9)
            if lim and p["usage_s"]["week"] >= lim * 0.95:
                return False, "cota semanal estimada de GPU quase no fim"
    return True, "sem cérebro e há capacidade"



def validate_kaggle_payload() -> None:
    root = Path("kaggle")
    metadata = json.loads((root / "kernel-metadata.json").read_text())
    notebook = json.loads((root / "kaggle_worker.ipynb").read_text())

    if metadata.get("id") != "GAv7489/xhakinha-worker":
        raise SystemExit("BLOQUEADO: identificador Kaggle incorreto.")

    if metadata.get("code_file") != "kaggle_worker.ipynb":
        raise SystemExit("BLOQUEADO: arquivo principal incorreto.")

    count = len(notebook.get("cells", []))
    if count != 32:
        raise SystemExit(
            f"BLOQUEADO: notebook tem {count} células; são exigidas 32. "
            "Nenhum push foi realizado."
        )

    print("Notebook autorizado validado: 32 células.")

def main() -> int:
    r = requests.get(os.environ["XH_COORD_URL"].rstrip("/") + "/api/state", timeout=30,
                     headers={"Authorization": "Bearer " + os.environ["XH_ADMIN_TOKEN"], "X-Requested-With": "XHK"})
    r.raise_for_status()
    ok, why = decide(r.json())
    print("decisão:", ok, "-", why)
    if not ok:
        return 0
    validate_kaggle_payload()
    return subprocess.call(["kaggle", "kernels", "push", "-p", "kaggle"])


if __name__ == "__main__":
    sys.exit(main())
