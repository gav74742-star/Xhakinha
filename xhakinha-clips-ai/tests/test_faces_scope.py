import time

from xhakinha.faces import active_profiles


def create_source(db, name, *, enabled=1, authorized=1):
    return db.x(
        "INSERT INTO sources(platform,name,ref,enabled,authorized,created) "
        "VALUES(?,?,?,?,?,?)",
        ("youtube", name, f"test-{name}", enabled, authorized, time.time()),
    )


def create_profile(db, name, source_id, *, expires=None, revoked=None):
    now = time.time()
    profile_id = db.x(
        "INSERT INTO face_profiles("
        "name,consent_version,consent_scope,consent_evidence,consent_recorded,"
        "consent_by,consent_expires,revoked_at,threshold,reference_count,created,updated"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            name, "test-v1", "Teste de isolamento por fonte",
            "autorização de teste", now, "admin", expires, revoked,
            0.50, 1, now, now,
        ),
    )
    db.x(
        "INSERT INTO face_profile_sources(profile_id,source_id,created) "
        "VALUES(?,?,?)",
        (profile_id, source_id, now),
    )
    # Vetor sintético de teste; nenhum rosto ou dado biométrico real.
    db.put_secret(
        f"face_profile:{profile_id}",
        {"embeddings": [[1.0] + [0.0] * 127]},
    )
    return profile_id


def test_profile_is_limited_to_its_authorized_source(engine):
    db = engine.db
    source_a = create_source(db, "scope-a")
    source_b = create_source(db, "scope-b")
    disabled = create_source(db, "scope-disabled", enabled=0)
    unauthorized = create_source(db, "scope-unauthorized", authorized=0)

    profile_id = create_profile(db, "Pessoa teste", source_a)

    assert [p["id"] for p in active_profiles(db, source_id=source_a)] == [
        profile_id
    ]
    assert active_profiles(db, source_id=source_b) == []
    assert active_profiles(db, source_id=disabled) == []
    assert active_profiles(db, source_id=unauthorized) == []
    assert active_profiles(db) == []
    assert active_profiles(db, source_id=None) == []


def test_expired_and_revoked_profiles_are_not_loaded(engine):
    db = engine.db
    source_id = create_source(db, "scope-expiry")
    expired_id = create_profile(
        db, "Pessoa vencida", source_id, expires=time.time() - 60
    )
    revoked_id = create_profile(
        db, "Pessoa revogada", source_id, revoked=time.time()
    )
    valid_id = create_profile(db, "Pessoa válida", source_id)

    loaded = active_profiles(db, source_id=source_id)
    loaded_ids = [p["id"] for p in loaded]

    assert valid_id in loaded_ids
    assert expired_id not in loaded_ids
    assert revoked_id not in loaded_ids


def test_invalid_source_ids_never_load_profiles(engine):
    db = engine.db
    source_id = create_source(db, "scope-invalid")
    create_profile(db, "Pessoa teste", source_id)

    assert active_profiles(db, source_id=0) == []
    assert active_profiles(db, source_id=-1) == []
    assert active_profiles(db, source_id="inválido") == []
