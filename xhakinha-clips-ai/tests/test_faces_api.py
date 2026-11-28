import socket
import threading
import time

import pytest
import requests
import uvicorn

from xhakinha import auth
from xhakinha.web.app import create_app

CSRF = {"X-Requested-With": "XHK"}
PASSWORD = "uma-senha-bem-longa-123"


@pytest.fixture()
def face_server(engine, settings):
    app = create_app(settings, engine=engine, run_engine=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    srv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    for _ in range(100):
        try:
            requests.get(base + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    else:
        srv.should_exit = True
        thread.join(5)
        pytest.fail("servidor de teste não iniciou")

    yield base
    srv.should_exit = True
    thread.join(5)


@pytest.fixture()
def face_admin(face_server, engine):
    auth.create_user(engine.db, "admin", PASSWORD)
    session = requests.Session()
    session.headers.update(CSRF)
    response = session.post(
        face_server + "/api/login",
        json={"username": "admin", "password": PASSWORD},
    )
    assert response.status_code == 200
    return session


def make_source(db, name, *, enabled=1, authorized=1):
    return db.x(
        "INSERT INTO sources(platform,name,ref,enabled,authorized,created) "
        "VALUES(?,?,?,?,?,?)",
        ("youtube", name, f"face-api-{name}", enabled, authorized, time.time()),
    )


def fake_photo():
    return {"photo": ("synthetic.jpg", b"synthetic-test-image", "image/jpeg")}


def create_profile(base, client, source_id):
    return client.post(
        base + "/api/faces",
        data={
            "name": "Pessoa sintética",
            "scope": "Teste autorizado de isolamento por fonte",
            "evidence": "autorização sintética de teste",
            "consent_confirmed": "true",
            "sources_confirmed": "true",
            "source_ids": str(source_id),
        },
        files=fake_photo(),
    )


def test_face_profile_creation_and_source_listing(
    face_server, face_admin, engine, monkeypatch
):
    from xhakinha.faces import FaceModel

    monkeypatch.setattr(
        FaceModel,
        "reference_embedding",
        lambda self, raw: [1.0] + [0.0] * 127,
    )
    source_a = make_source(engine.db, "api-source-a")

    response = create_profile(face_server, face_admin, source_a)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["ok"] is True
    assert result["reference_count"] == 1
    assert result["source_ids"] == [source_a]

    listing = face_admin.get(face_server + "/api/faces")
    assert listing.status_code == 200
    profiles = listing.json()
    assert len(profiles) == 1
    assert profiles[0]["source_ids"] == [source_a]
    assert profiles[0]["active"] is True


def test_face_api_rejects_unauthorized_source(
    face_server, face_admin, engine, monkeypatch
):
    from xhakinha.faces import FaceModel

    monkeypatch.setattr(
        FaceModel,
        "reference_embedding",
        lambda self, raw: [1.0] + [0.0] * 127,
    )
    unauthorized = make_source(
        engine.db, "api-source-denied", authorized=0
    )

    response = create_profile(face_server, face_admin, unauthorized)
    assert response.status_code == 400
    assert engine.db.one("SELECT id FROM face_profiles") is None


def test_face_profile_can_add_only_authorized_sources(
    face_server, face_admin, engine, monkeypatch
):
    from xhakinha.faces import FaceModel

    monkeypatch.setattr(
        FaceModel,
        "reference_embedding",
        lambda self, raw: [1.0] + [0.0] * 127,
    )
    source_a = make_source(engine.db, "api-source-first")
    source_b = make_source(engine.db, "api-source-second")
    denied = make_source(
        engine.db, "api-source-third", authorized=0
    )

    created = create_profile(face_server, face_admin, source_a)
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    rejected = face_admin.post(
        face_server + f"/api/faces/{profile_id}/sources",
        data={"source_ids": str(denied), "sources_confirmed": "true"},
    )
    assert rejected.status_code == 400

    added = face_admin.post(
        face_server + f"/api/faces/{profile_id}/sources",
        data={"source_ids": str(source_b), "sources_confirmed": "true"},
    )
    assert added.status_code == 200, added.text
    assert added.json()["source_ids"] == [source_a, source_b]

    listing = face_admin.get(face_server + "/api/faces").json()
    assert listing[0]["source_ids"] == [source_a, source_b]


def test_revoked_profile_cannot_receive_new_sources(
    face_server, face_admin, engine, monkeypatch
):
    from xhakinha.faces import FaceModel

    monkeypatch.setattr(
        FaceModel,
        "reference_embedding",
        lambda self, raw: [1.0] + [0.0] * 127,
    )
    source_a = make_source(engine.db, "api-source-revoke-a")
    source_b = make_source(engine.db, "api-source-revoke-b")

    created = create_profile(face_server, face_admin, source_a)
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    revoked = face_admin.delete(face_server + f"/api/faces/{profile_id}")
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    added = face_admin.post(
        face_server + f"/api/faces/{profile_id}/sources",
        data={"source_ids": str(source_b), "sources_confirmed": "true"},
    )
    assert added.status_code == 400
