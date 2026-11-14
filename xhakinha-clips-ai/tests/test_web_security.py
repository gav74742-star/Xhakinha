import json
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

from xhakinha import auth
from xhakinha.publish.base import sign_media_token
from xhakinha.web.app import create_app

XH = {"X-Requested-With": "XHK"}


@pytest.fixture()
def server(engine, settings):
    app = create_app(settings, engine=engine, run_engine=False)
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            requests.get(base + "/api/health", timeout=1); break
        except Exception:
            time.sleep(0.05)
    yield base
    srv.should_exit = True; t.join(5)


@pytest.fixture()
def admin(server, engine):
    auth.create_user(engine.db, "admin", "uma-senha-bem-longa-123")
    s = requests.Session(); s.headers.update(XH)
    r = s.post(server + "/api/login", json={"username": "admin", "password": "uma-senha-bem-longa-123"})
    assert r.status_code == 200
    return s


def test_api_requires_login_and_no_default_admin(server, engine):
    for p in ("/api/status", "/api/settings", "/api/clips", "/api/publications", "/api/logs", "/api/backups"):
        assert requests.get(server + p).status_code == 401
    r = requests.post(server + "/api/login", json={"username": "admin", "password": "admin"}, headers=XH)
    assert r.status_code == 503                      # nenhum admin/senha padrão existe


def test_password_hashing_scrypt_and_min_length():
    h = auth.hash_password("uma-senha-bem-longa-123")
    assert h.startswith("scrypt$") and "uma-senha" not in h
    assert auth.verify_password("uma-senha-bem-longa-123", h) and not auth.verify_password("outra", h)
    with pytest.raises(ValueError):
        auth.hash_password("curta")


def test_login_cookie_flags_bruteforce_lock_and_csrf(server, engine):
    auth.create_user(engine.db, "admin", "uma-senha-bem-longa-123")
    assert requests.post(server + "/api/login", json={"username": "admin", "password": "uma-senha-bem-longa-123"}).status_code == 403   # sem anti-CSRF
    r = requests.post(server + "/api/login", json={"username": "admin", "password": "uma-senha-bem-longa-123"}, headers=XH)
    c = r.headers["set-cookie"].lower()
    assert "httponly" in c and "samesite=lax" in c
    for _ in range(8):
        requests.post(server + "/api/login", json={"username": "admin", "password": "errada"}, headers=XH)
    assert requests.post(server + "/api/login", json={"username": "admin", "password": "uma-senha-bem-longa-123"}, headers=XH).status_code == 429


def test_forged_token_rejected(server, engine):
    import jwt
    forged = jwt.encode({"sub": "admin", "exp": int(time.time()) + 999}, "changeme", algorithm="HS256")
    r = requests.get(server + "/api/status", cookies={"xh_session": forged})
    assert r.status_code == 401
    none_tok = jwt.encode({"sub": "admin", "exp": int(time.time()) + 999}, "", algorithm="none")
    assert requests.get(server + "/api/status", cookies={"xh_session": none_tok}).status_code == 401


def test_mutations_need_csrf_header(server, admin):
    s = requests.Session(); s.cookies.update(admin.cookies)
    assert s.post(server + "/api/control", json={"action": "pause"}).status_code == 403
    assert admin.post(server + "/api/control", json={"action": "pause"}).status_code == 200
    assert admin.post(server + "/api/control", json={"action": "resume"}).status_code == 200


def test_security_headers_present(server):
    r = requests.get(server + "/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"


def test_settings_validation_and_protected_keys(server, admin, engine):
    assert admin.put(server + "/api/settings", json={"secret_key": "x"}).status_code == 400
    assert admin.put(server + "/api/settings", json={"daily_min": 20, "daily_normal": 10}).status_code == 400
    assert admin.put(server + "/api/settings", json={"handle_instagram": "bad handle!"}).status_code == 400
    assert admin.put(server + "/api/settings", json={"daily_min": 5, "daily_normal": 8, "daily_max": 12, "handle_tiktok": "@xhakinha_teste"}).status_code == 200
    assert engine.db.get("daily_normal") == 8


def test_no_secret_leaks_in_api_db_or_logs(server, admin, engine, fake):
    engine.s.graph_base = fake.base + "/ig"
    r = admin.post(server + "/api/accounts/instagram", json={"access_token": "SUPERSECRETIGTOKEN123", "ig_user_id": "1789"})
    assert r.status_code == 200 and r.json()["username"] == "conta_teste"
    engine.db.event("info", "t", "Authorization: Bearer SUPERSECRETIGTOKEN123 access_token=SUPERSECRETIGTOKEN123")
    for p in ("/api/accounts", "/api/settings", "/api/status", "/api/logs", "/api/attempts"):
        assert "SUPERSECRETIGTOKEN123" not in admin.get(server + p).text, p
    raw = open(engine.s.db_path, "rb").read()
    assert b"SUPERSECRETIGTOKEN123" not in raw                      # token cifrado em repouso (Fernet)
    assert engine.db.get_secret("account:instagram")["access_token"] == "SUPERSECRETIGTOKEN123"


def test_upload_validation(server, admin, engine, synth_video):
    h = {**XH, "X-Authorized": "1"}
    assert admin.put(server + "/api/upload?name=a.exe", data=b"MZ", headers=h).status_code == 400
    assert admin.put(server + "/api/upload?name=fake.mp4", data=b"not a video at all" * 100, headers=h).status_code == 400
    assert admin.put(server + "/api/upload?name=ok.mp4", data=open(synth_video, "rb").read(), headers={**XH, "X-Authorized": "0"}).status_code == 400
    r = admin.put(server + "/api/upload?name=../../etc/ok.mp4", data=open(synth_video, "rb").read(), headers=h)
    assert r.status_code == 200
    c = engine.db.one("SELECT * FROM contents WHERE id=?", (r.json()["content_id"],))
    assert c["kind"] == "upload" and Path(c["local_path"]).is_file() and engine.st.inside_managed(Path(c["local_path"]))
    assert engine.db.one("SELECT COUNT(*) n FROM jobs WHERE type='process'")["n"] == 1
    assert not list((engine.s.production_dir / "uploads").glob("*.part"))     # sem lixo


def test_upload_size_limit(server, admin, engine):
    engine.s.max_upload_mb = 1
    r = admin.put(server + "/api/upload?name=big.mp4", data=b"\0" * (2 * 1024 * 1024), headers={**XH, "X-Authorized": "1"})
    assert r.status_code == 400 and "maior" in r.json()["error"]


def test_source_requires_authorization_attestation_and_url_whitelist(server, admin):
    assert admin.post(server + "/api/sources", json={"platform": "youtube", "name": "x", "ref": "@x", "authorized": False}).status_code == 400
    assert admin.post(server + "/api/sources", json={"platform": "url", "name": "x", "ref": "https://evil.example/v", "authorized": True}).status_code == 400
    r = admin.post(server + "/api/sources", json={"platform": "youtube", "name": "Meu canal", "ref": "@meucanal", "authorized": True})
    assert r.status_code == 200
    assert admin.post(server + "/api/sources", json={"platform": "youtube", "name": "dup", "ref": "@meucanal", "authorized": True}).status_code == 400


def test_public_media_signed_url_rules(server, engine, synth_video):
    now = time.time()
    clip = engine.db.x("INSERT INTO clips(content_id,start,end,score,status,created,updated) VALUES(0,0,1,80,'awaiting',?,?)", (now, now)) if False else None
    f = engine.s.production_dir / "ready" / "pub1_instagram.mp4"
    f.write_bytes(open(synth_video, "rb").read())
    cid = engine.db.x("INSERT INTO clips(content_id,start,end,score,status,created,updated) VALUES(NULL,0,1,80,'awaiting',?,?)", (now, now))
    pid = engine.db.x("INSERT INTO publications(clip_id,platform,status,file_path,created,updated) VALUES(?,?,?,?,?,?)", (cid, "instagram", "uploading", str(f), now, now))
    tok = sign_media_token(engine.s.secret_key, pid)
    r = requests.get(f"{server}/media/{tok}.mp4", headers={"Range": "bytes=0-1"})
    assert r.status_code in (200, 206) and r.headers["content-type"].startswith("video/mp4")
    assert requests.get(f"{server}/media/{tok[:-2]}xx.mp4").status_code == 403                       # assinatura inválida
    assert requests.get(f"{server}/media/{sign_media_token(engine.s.secret_key, pid, ttl_s=-10)}.mp4").status_code == 403   # expirado
    assert requests.get(f"{server}/media/{sign_media_token('outra-chave' * 4, pid)}.mp4").status_code == 403
    engine.db.x("UPDATE publications SET status='published' WHERE id=?", (pid,))
    assert requests.get(f"{server}/media/{tok}.mp4").status_code == 404                              # após publicar, URL deixa de servir


def test_backup_path_traversal_blocked(server, admin):
    assert admin.post(server + "/api/backup").status_code == 200
    names = admin.get(server + "/api/backups").json()
    assert names and admin.get(server + "/api/backups/" + names[0]["name"]).status_code == 200
    assert admin.get(server + "/api/backups/..%2F..%2F.env").status_code in (404, 400)
    assert admin.get(server + "/api/backups/xhakinha-1.db/../x").status_code == 404


def test_pwa_assets_real_and_not_empty(server):
    import io
    from PIL import Image
    m = requests.get(server + "/static/manifest.webmanifest").json()
    assert m["display"] == "standalone" and len(m["icons"]) >= 3
    for ic in m["icons"]:
        r = requests.get(server + ic["src"]); assert r.status_code == 200
        w, h = Image.open(io.BytesIO(r.content)).size
        assert f"{w}x{h}" == ic["sizes"] and len(r.content) > 1000          # ícone NÃO vazio
    sw = requests.get(server + "/sw.js")
    assert sw.status_code == 200 and "addEventListener('fetch'" in sw.text and "/api/" in sw.text
    assert "XHAKINHA" in requests.get(server + "/").text


def test_every_button_endpoint_exists(server, admin):
    """O painel só chama estas rotas; todas precisam responder (nada de botão sem função)."""
    js = (Path(__file__).parents[1] / "xhakinha/web/static/app.js").read_text()
    import re
    paths = set(re.findall(r"'(/api/[a-z/_]+)", js))
    for p in sorted(paths):
        r = admin.get(server + p.rstrip("/"))
        assert r.status_code != 404 or p.endswith("/"), p       # 405 (método) é aceitável; 404 não
