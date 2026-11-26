import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from kaggle_trigger import validate_kaggle_payload  # noqa: E402


def _payload(tmp_path):
    target = tmp_path / "kaggle"
    target.mkdir()
    (target / "kernel-metadata.json").write_text(
        (ROOT / "kaggle/kernel-metadata.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (target / "kaggle_worker.ipynb").write_text(
        (ROOT / "kaggle/kaggle_worker.ipynb").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return target


def _notebook(root):
    path = root / "kaggle_worker.ipynb"
    return path, json.loads(path.read_text(encoding="utf-8"))


def _write(path, notebook):
    path.write_text(json.dumps(notebook, ensure_ascii=False), encoding="utf-8")


def test_payload_oficial_passa_sem_contagem_fixa():
    result = validate_kaggle_payload(ROOT / "kaggle")
    assert result["physical_cells"] >= result["code_cells"] > 0
    assert result["main_calls"] == 1
    assert result["remote_gate_validated"] is True


def test_payload_aceita_quantidade_diferente_de_celulas(tmp_path):
    root = _payload(tmp_path)
    path, nb = _notebook(root)
    code = next(c for c in nb["cells"] if c.get("cell_type") == "code")
    index = nb["cells"].index(code)
    nb["cells"].insert(index, {
        "cell_type": "markdown", "metadata": {}, "source": "## Diagnóstico adicional"
    })
    nb["cells"].insert(-1, {
        "cell_type": "code", "execution_count": None, "metadata": {},
        "outputs": [], "source": ["# célula auxiliar segura\n", "pass\n"]
    })
    _write(path, nb)
    result = validate_kaggle_payload(root)
    assert result["physical_cells"] == len(nb["cells"])
    assert result["code_cells"] == sum(c.get("cell_type") == "code" for c in nb["cells"])


def test_payload_rejeita_sintaxe_python_invalida(tmp_path):
    root = _payload(tmp_path)
    path, nb = _notebook(root)
    first = next(c for c in nb["cells"] if c.get("cell_type") == "code")
    first["source"] = ["def sintaxe_quebrada(:\n", "    pass\n"]
    _write(path, nb)
    with pytest.raises(SystemExit, match="sintaxe inválida"):
        validate_kaggle_payload(root)


def test_payload_rejeita_inicio_ativado_por_padrao(tmp_path):
    root = _payload(tmp_path)
    path, nb = _notebook(root)
    last = [c for c in nb["cells"] if c.get("cell_type") == "code"][-1]
    source = "".join(last["source"])
    last["source"] = source.replace(
        "INICIAR_KAGGLE_WORKER = False", "INICIAR_KAGGLE_WORKER = True", 1
    )
    _write(path, nb)
    with pytest.raises(SystemExit, match="gate de inicialização incompleto"):
        validate_kaggle_payload(root)


def test_payload_rejeita_main_fora_do_ultimo_gate(tmp_path):
    root = _payload(tmp_path)
    path, nb = _notebook(root)
    first = next(c for c in nb["cells"] if c.get("cell_type") == "code")
    first["source"] = list(first["source"]) + ["\nmain()  # chamada indevida\n"]
    _write(path, nb)
    with pytest.raises(SystemExit, match="exatamente um main"):
        validate_kaggle_payload(root)


def test_payload_rejeita_gate_final_ausente(tmp_path):
    root = _payload(tmp_path)
    path, nb = _notebook(root)
    last = [c for c in nb["cells"] if c.get("cell_type") == "code"][-1]
    source = "".join(last["source"])
    last["source"] = source.replace('XH.get("etapa15d_ok") is not True', 'True', 1)
    _write(path, nb)
    with pytest.raises(SystemExit, match="gate de inicialização incompleto"):
        validate_kaggle_payload(root)


class _Response:
    def __init__(self, data=None):
        self.data = data or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def _prepare_main(monkeypatch, fence=123):
    import kaggle_trigger

    monkeypatch.setenv("XH_COORD_URL", "https://coordenador.test")
    monkeypatch.setenv("XH_ADMIN_TOKEN", "token-teste")
    monkeypatch.setattr(
        kaggle_trigger.requests, "get",
        lambda *a, **k: _Response({"launch_enabled": True}),
    )
    monkeypatch.setattr(kaggle_trigger, "decide", lambda state: (True, "teste"))
    monkeypatch.setattr(
        kaggle_trigger, "validate_kaggle_payload",
        lambda: {"remote_gate_validated": True},
    )
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/acquire"):
            return _Response({"fence": fence})
        return _Response({"ok": True})

    monkeypatch.setattr(kaggle_trigger.requests, "post", post)
    return kaggle_trigger, calls


def test_main_nao_dispara_se_bloqueio_ja_pertence_a_outro(monkeypatch):
    import kaggle_trigger

    kaggle_trigger, calls = _prepare_main(monkeypatch, fence=None)
    monkeypatch.setattr(
        kaggle_trigger.subprocess, "call",
        lambda *a, **k: pytest.fail("Kaggle não deveria ser chamado"),
    )

    assert kaggle_trigger.main() == 0
    assert len(calls) == 1
    assert calls[0][0].endswith("/acquire")


def test_main_libera_bloqueio_apos_envio(monkeypatch):
    kaggle_trigger, calls = _prepare_main(monkeypatch)
    monkeypatch.setattr(kaggle_trigger.subprocess, "call", lambda *a: 7)

    assert kaggle_trigger.main() == 7
    assert len(calls) == 2
    assert calls[0][0].endswith("/acquire")
    assert calls[1][0].endswith("/release")
    acquired = calls[0][1]["json"]
    released = calls[1][1]["json"]
    assert released["name"] == acquired["name"] == "kaggle-launch"
    assert released["owner"] == acquired["owner"]
    assert released["fence"] == 123


def test_main_libera_bloqueio_se_envio_lancar_erro(monkeypatch):
    kaggle_trigger, calls = _prepare_main(monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(kaggle_trigger.subprocess, "call", fail)

    with pytest.raises(RuntimeError, match="falha simulada"):
        kaggle_trigger.main()

    assert len(calls) == 2
    assert calls[1][0].endswith("/release")


def test_main_bloqueia_se_autorizacao_for_revogada_apos_trava(monkeypatch):
    kaggle_trigger, calls = _prepare_main(monkeypatch)
    states = iter([
        {"launch_enabled": True},
        {"launch_enabled": False},
    ])
    monkeypatch.setattr(
        kaggle_trigger.requests, "get",
        lambda *a, **k: _Response(next(states)),
    )
    monkeypatch.setattr(
        kaggle_trigger.subprocess, "call",
        lambda *a, **k: pytest.fail("Kaggle não deveria ser chamado"),
    )

    assert kaggle_trigger.main() == 0
    assert len(calls) == 2
    assert calls[0][0].endswith("/acquire")
    assert calls[1][0].endswith("/release")
