import re
from pathlib import Path
from types import SimpleNamespace

from xhakinha.cloud import kaggle_runner


ROOT = Path(__file__).resolve().parents[1]


def test_libsql_is_pinned_in_both_manifests():
    for filename in ("requirements.txt", "requirements-kaggle.txt"):
        lines = (ROOT / filename).read_text(encoding="utf-8").splitlines()
        found = [
            line.strip()
            for line in lines
            if re.match(r"^libsql(?:\s|[<>=!~]|$)", line.strip(), re.I)
        ]
        assert found == ["libsql==0.1.11"], (filename, found)


def test_kaggle_runner_repairs_missing_libsql(monkeypatch):
    original_import = kaggle_runner.importlib.import_module
    attempts = 0
    commands = []

    def fake_import(name, *args, **kwargs):
        nonlocal attempts
        if name == "libsql":
            attempts += 1
            if attempts == 1:
                raise ModuleNotFoundError("libsql ausente")
            return object()
        return original_import(name, *args, **kwargs)

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(kaggle_runner.importlib, "import_module", fake_import)
    monkeypatch.setattr(kaggle_runner.metadata, "version", lambda name: "0.1.11")
    monkeypatch.setattr(kaggle_runner.subprocess, "run", fake_run)

    kaggle_runner.ensure_libsql_driver()

    assert attempts == 2
    assert len(commands) == 1
    assert commands[0][-1] == "libsql==0.1.11"


def test_kaggle_runner_defaults_to_turso_and_checks_driver_first():
    source = (ROOT / "xhakinha/cloud/kaggle_runner.py").read_text(encoding="utf-8")
    assert 'os.environ.setdefault("XH_STATE_BACKEND", "turso")' in source
    assert source.index("    ensure_libsql_driver()") < source.index(
        "    from .runner import main as run"
    )

def test_kaggle_runner_reports_pip_install_failure(monkeypatch):
    import pytest

    def fail_import(name, *args, **kwargs):
        raise ModuleNotFoundError("libsql ausente")

    def fail_pip(command, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stderr="erro simulado do pip",
            stdout="",
        )

    monkeypatch.setattr(kaggle_runner.importlib, "import_module", fail_import)
    monkeypatch.setattr(kaggle_runner.subprocess, "run", fail_pip)

    with pytest.raises(RuntimeError, match="erro simulado do pip"):
        kaggle_runner.ensure_libsql_driver()
