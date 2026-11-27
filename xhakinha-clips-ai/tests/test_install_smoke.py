"""Instalação limpa + inicialização real do servidor como processo + reinício com persistência."""
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
XH = {"X-Requested-With": "XHK"}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture()
def clean_copy(tmp_path):
    dst = tmp_path / "proj"
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".env", "data", "__pycache__", ".pytest_cache", "*.db"))
    return dst


def run(cmd, cwd, env=None, **kw):
    e = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY",)}
    e.update(env or {})
    return subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, **kw)


def test_setup_generates_strong_secret_and_server_boots_login_and_restart_persists(clean_copy, tmp_path):
    # 1) sem .env/SECRET_KEY o servidor RECUSA subir (nada de chave padrão)
    r = run([sys.executable, "-m", "xhakinha.main"], clean_copy, {"DATA_DIR": str(tmp_path / "d0")}, timeout=60)
    assert r.returncode != 0 and "SECRET_KEY" in (r.stdout + r.stderr)
    # 2) setup gera .env com segredo forte e permissão 600
    r = run([sys.executable, "scripts/setup.py"], clean_copy); assert r.returncode == 0, r.stderr
    envf = clean_copy / ".env"
    assert oct(envf.stat().st_mode)[-3:] == "600"
    secret = [l for l in envf.read_text().splitlines() if l.startswith("SECRET_KEY=")][0].split("=", 1)[1]
    assert len(secret) >= 48
    assert run([sys.executable, "scripts/setup.py"], clean_copy).returncode != 0          # não sobrescreve sem --force
    env = {"SECRET_KEY": secret, "DATA_DIR": str(tmp_path / "data"), "COOKIE_SECURE": "0", "PORT": str(free_port()), "HOST": "127.0.0.1", "XH_ENV": "test", "XH_STATE_BACKEND": "sqlite"}
    # 3) cria admin via CLI
    r = run([sys.executable, "-m", "xhakinha.cli", "create-admin"], clean_copy, {**env, "ADMIN_USER": "dono", "ADMIN_PASSWORD": "senha-super-longa-99"}); assert r.returncode == 0, r.stderr
    r = run([sys.executable, "-m", "xhakinha.cli", "doctor"], clean_copy, env); assert "ffmpeg: True" in r.stdout

    def boot():
        p = subprocess.Popen([sys.executable, "-m", "xhakinha.main"], cwd=clean_copy, env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{env['PORT']}"
        for _ in range(150):
            try:
                if requests.get(base + "/api/health", timeout=1).ok:
                    return p, base
            except Exception:
                time.sleep(0.1)
        p.kill(); raise AssertionError(p.stdout.read().decode())

    p, base = boot()
    try:
        s = requests.Session(); s.headers.update(XH)
        assert s.post(base + "/api/login", json={"username": "dono", "password": "senha-super-longa-99"}).ok
        st = s.get(base + "/api/status").json()
        assert st["engine"] == "running" and st["deps"]["ffmpeg"] and st["tz"] == "America/Sao_Paulo"
        assert s.put(base + "/api/settings", json={"daily_min": 3, "daily_normal": 5, "daily_max": 7}).ok
        # enfileira trabalho persistente e derruba o processo (kill -9 = queda/reinício brusco)
        import sqlite3
        db = sqlite3.connect(env["DATA_DIR"] + "/xhakinha.db")
        db.execute("INSERT INTO jobs(type,payload,status,run_at,created,updated) VALUES('cleanup','{}','running',?,?,?)", (time.time(), time.time(), time.time()))
        db.commit(); db.close()
    finally:
        p.send_signal(signal.SIGKILL); p.wait()
    p, base = boot()
    try:
        s = requests.Session(); s.headers.update(XH)
        assert s.post(base + "/api/login", json={"username": "dono", "password": "senha-super-longa-99"}).ok
        assert s.get(base + "/api/settings").json()["daily_max"] == 7                  # configuração persistiu
        time.sleep(3)
        import sqlite3
        db = sqlite3.connect(env["DATA_DIR"] + "/xhakinha.db")
        st = db.execute("SELECT status FROM jobs WHERE type='cleanup' ORDER BY id LIMIT 1").fetchone()[0]
        assert st in ("pending", "done")                                                # job 'running' foi RECUPERADO após a queda
    finally:
        p.terminate(); p.wait(timeout=15)


def test_dockerfile_and_compose_security_files():
    df = (ROOT / "Dockerfile").read_text()
    assert "USER xh" in df and "ffmpeg" in df and "yt-dlp" not in df.split("COPY")[0]       # roda sem root; yt-dlp vem do requirements
    di = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in di and "data/" in di and "*.db" in di
    assert "yt-dlp" in (ROOT / "requirements.txt").read_text() and "faster-whisper" in (ROOT / "requirements.txt").read_text()
    assert "SECRET_KEY=" in (ROOT / ".env.example").read_text() and "SECRET_KEY=\n" in (ROOT / ".env.example").read_text() + "\n"
    ex = (ROOT / ".env.example").read_text()
    assert not any(l.split("=", 1)[1].strip() for l in ex.splitlines() if "=" in l and l.split("=", 1)[0] in ("SECRET_KEY", "YOUTUBE_API_KEY", "TIKTOK_CLIENT_SECRET", "TWITCH_CLIENT_SECRET"))
    assert "https" in (ROOT / "Caddyfile").read_text() or "{$DOMAIN}" in (ROOT / "Caddyfile").read_text()


def test_no_hardcoded_secrets_or_default_passwords_in_source():
    import re
    bad = re.compile(r"(?i)(secret_key|password|senha)\s*=\s*[\"'][^\"']{4,}[\"']")
    for f in (ROOT / "xhakinha").rglob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if bad.search(line) and "WEAK" not in line and "senha" not in line.lower().split("=")[0] + "x":
                raise AssertionError(f"{f}:{i}: {line.strip()}")
