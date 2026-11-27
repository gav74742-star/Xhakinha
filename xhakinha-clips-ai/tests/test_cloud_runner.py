"""Runner/Kaggle: compatibilidade do notebook, decisão do gatilho, sessão completa contra coordenador HTTP local.
REAL: processo Python, HTTP localhost, SQLite. SIMULADO: armazenamento local para testes. NÃO TESTADO: execução dentro do Kaggle de verdade."""
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from kaggle_trigger import decide, validate_kaggle_payload  # noqa: E402
from xhakinha.cloud import runner
from xhakinha.cloud.api import create_app
from xhakinha.cloud.coordinator import Coordinator
from xhakinha.cloud.drive_storage import LocalDirBackend
from xhakinha.cloud.healthcheck import local_report
from xhakinha.cloud.snapshots import SnapshotManager

ROOT = Path(__file__).resolve().parents[1]


def test_notebooks_sao_json_valido_e_celulas_de_codigo_compilam():
    for nbp in ("notebooks/colab_worker.ipynb",):
        nb = json.loads((ROOT / nbp).read_text())
        assert nb["nbformat"] == 4
        for c in nb["cells"]:
            if c["cell_type"] == "code":
                compile("".join(c["source"]), nbp, "exec")
    meta = json.loads((ROOT / "kaggle/kernel-metadata.json").read_text())
    assert meta["code_file"] == "kaggle_worker.ipynb" and meta["enable_internet"] == "true" and meta["is_private"] == "true"
    canonical = json.loads((ROOT / "kaggle/kaggle_worker.ipynb").read_text())
    assert canonical["nbformat"] == 4
    result = validate_kaggle_payload(ROOT / "kaggle")
    assert result["physical_cells"] == len(canonical["cells"])
    assert result["main_calls"] == 1
    for cell in canonical["cells"]:
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), "kaggle/kaggle_worker.ipynb", "exec")


def test_provedor_detectado_pelo_ambiente():
    assert runner.detect_provider({"GITHUB_ACTIONS": "true"}) == "github"
    assert runner.detect_provider({"COLAB_RELEASE_TAG": "x"}) == "colab"
    assert runner.detect_provider({"XH_PROVIDER": "kaggle-gpu"}) == "kaggle-gpu"


def test_healthcheck_honesto():
    r = local_report()
    assert set(["ffmpeg", "whisper", "faces", "gpu", "disk_free_gb", "ok"]) <= set(r)
    assert isinstance(r["gpu"], bool) and r["ffmpeg"] is True            # GPU depende do executor real


def test_gatilho_so_inicia_com_tarefa_gpu_elegivel():
    tarefa = {
        "id": "gpu-1", "status": "queued", "priority": 10,
        "needs": '["gpu"]', "prefer": "[]", "est_seconds": 3000,
        "run_after": 900, "created": 900,
    }
    base = {
        "now": 1000, "locks": [], "executors": [], "tasks": [tarefa],
        "providers": [{
            "name": "kaggle-gpu", "breaker": "closed", "paused": False,
            "usage_s": {"day": 0, "week": 0},
            "config": {
                "session_seconds": 39600, "week_seconds": 97200,
                "safety": 0.9, "reserve_frac": 0.15, "known": True,
            },
        }],
    }

    assert decide(base)[0]
    assert not decide({**base, "tasks": []})[0]
    assert not decide({**base, "tasks": [{**tarefa, "run_after": 1001}]})[0]
    assert not decide({**base, "tasks": [{**tarefa, "needs": '["cpu"]'}]})[0]
    assert not decide({**base, "tasks": [{**tarefa, "prefer": '["colab"]'}]})[0]
    assert decide({**base, "tasks": [{**tarefa, "prefer": '["colab"]', "created": 0}]})[0]
    assert not decide({**base, "locks": [{"name": "brain", "owner": "x", "lease_until": 2000}]})[0]
    assert not decide({**base, "executors": [{"status": "active", "provider": "kaggle-cpu"}]})[0]
    assert not decide({**base, "providers": [{**base["providers"][0], "breaker": "open"}]})[0]
    assert not decide({**base, "providers": [{**base["providers"][0], "usage_s": {"day": 0, "week": 90000}}]})[0]


def test_sessao_completa_cerebro_cria_snapshot_e_libera_lock(tmp_path, monkeypatch):
    co = Coordinator(":memory:")
    app = create_app(co, "E" * 24, "A" * 24)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try: requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1); break
        except Exception: time.sleep(0.05)
    env = {"SECRET_KEY": "k" * 48, "DATA_DIR": str(tmp_path / "data"), "XH_ENV": "test", "COOKIE_SECURE": "0",
           "XH_PROVIDER": "local", "XH_SESSION_HOURS": "0.1",
           "XH_STORAGE_BACKEND": "local", "XH_LOCAL_RUNTIME_DIR": str(tmp_path / "drive"),
           "XH_STATE_BACKEND": "sqlite", "XH_COORD_URL": f"http://127.0.0.1:{port}", "XH_COORD_TOKEN": "E" * 24, "MIN_FREE_GB": "0.01"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    out = runner.main(env)
    assert out["brain"]["status"] == "ok" and out["brain"]["snapshots"] >= 1
    d = LocalDirBackend(tmp_path / "drive")
    assert SnapshotManager(d, tmp_path / "t").manifests()[0]["integrity"] == "ok"
    assert [l for l in co.state()["locks"] if l["name"] == "brain"][0]["owner"] is None        # lock liberado
    # segunda sessão restaura do snapshot da primeira (continuidade entre execuções efêmeras)
    out2 = runner.main(env)
    assert out2["brain"]["status"] == "ok" and out2["brain"]["restored"]
    srv.should_exit = True


def test_storage_sem_configuracao_falha_com_mensagem_clara():
    with pytest.raises(SystemExit, match="Armazenamento não configurado"):
        runner.drive_from_env({})


def test_google_drive_desativado():
    with pytest.raises(SystemExit, match="Google Drive foi desativado"):
        runner.drive_from_env({"XH_STORAGE_BACKEND": "gdrive"})
