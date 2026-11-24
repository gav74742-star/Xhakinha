"""Ponto de entrada de uma SESSÃO (Kaggle, Colab, GitHub Actions ou local):
   1) detecta o provedor e se há GPU; 2) monta Drive + cliente do coordenador a partir de variáveis de ambiente;
   3) tenta assumir o papel de CÉREBRO (lock exclusivo); 4) em paralelo, trabalha como EXECUTOR de tarefas pesadas.
Nada aqui assume que a sessão sobrevive: tudo que importa já está no Drive/coordenador."""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

from ..config import load_settings, redact
from .brain import Brain
from .client import HttpClient
from .drive_storage import DriveBackend, LocalDirBackend
from .executor import Executor
from .handlers import default_handlers
from .healthcheck import gpu_info, local_report
from .b2_storage import B2Backend

log = logging.getLogger("xh.runner")


def detect_provider(env=os.environ) -> str:
    if env.get("XH_PROVIDER"):
        return env["XH_PROVIDER"]
    if env.get("GITHUB_ACTIONS"):
        return "github"
    if env.get("COLAB_RELEASE_TAG") or env.get("COLAB_GPU") is not None:
        return "colab"
    if env.get("KAGGLE_KERNEL_RUN_TYPE") or Path("/kaggle/working").exists():
        return "kaggle-gpu" if gpu_info()["gpu"] else "kaggle-cpu"
    return "local"


def drive_from_env(env=os.environ) -> DriveBackend:
    """Seleciona Backblaze B2 ou armazenamento local; Google Drive desativado."""
    mode = str(env.get("XH_STORAGE_BACKEND", "")).strip().lower()

    if mode == "b2":
        region = env.get("B2_REGION", "").strip()
        endpoint = env.get("B2_ENDPOINT_URL", "").strip()
        if not endpoint and region:
            endpoint = f"https://s3.{region}.backblazeb2.com"
        return B2Backend(
            endpoint,
            region,
            env.get("B2_KEY_ID", ""),
            env.get("B2_APPLICATION_KEY", ""),
            env.get("B2_BUCKET", ""),
            max_gb=float(env.get("XH_B2_MAX_GB", "8")),
        )

    if mode == "local":
        root = Path(env.get("XH_LOCAL_RUNTIME_DIR", "/tmp/xhakinha-runtime"))
        return LocalDirBackend(root)

    if mode == "gdrive":
        raise SystemExit(
            "Google Drive foi desativado neste projeto. Use XH_STORAGE_BACKEND=b2."
        )

    raise SystemExit(
        "Armazenamento não configurado. Use XH_STORAGE_BACKEND=b2; "
        "Google Drive está desativado."
    )


def main(env=os.environ) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    provider = detect_provider(env)
    hours = float(env.get("XH_SESSION_HOURS", "10.5"))
    budget = hours * 3600
    if env.get("XH_DRY_HEALTH"):
        print(json.dumps(local_report(), indent=2)); return {}
    drive = drive_from_env(env)
    client = HttpClient(env["XH_COORD_URL"], env["XH_COORD_TOKEN"])
    ident = f"{provider}-{socket.gethostname()[:12]}-{int(time.time()) % 100000}"
    gpu = gpu_info()["gpu"]
    if gpu:
        os.environ.setdefault("XH_WHISPER_DEVICE", "cuda")
    s = load_settings()
    work = s.data_dir / "exec"
    ex = Executor(ident, provider, client, drive, default_handlers(), workdir=work, caps={"gpu": gpu}, session_seconds=budget)
    out: dict = {"provider": provider, "executor": ident}
    th = threading.Thread(target=lambda: out.__setitem__("executor_stats", ex.run(stop_margin_s=420, idle_exit_s=None)), daemon=True)
    th.start()
    if env.get("XH_ROLE", "auto") in ("auto", "brain"):
        brain = Brain(s, drive, client, ident)
        try:
            out["brain"] = brain.run(budget)
        except Exception as e:
            out["brain_error"] = redact(f"{type(e).__name__}: {e}")
            log.error("cérebro falhou: %s", out["brain_error"])
            ex.stop.wait(max(0, budget - 600))              # continua como executor até o fim da sessão
    else:
        ex.stop.wait(budget)
    ex.stop.set(); th.join(timeout=60)
    print(json.dumps(out, default=str, indent=2))
    return out


if __name__ == "__main__":
    main()
