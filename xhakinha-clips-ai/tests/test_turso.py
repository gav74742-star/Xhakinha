"""Turso como banco canônico. TODOS estes testes usam o FAKE libsql (SIMULADO): validam a lógica do projeto,
não o comportamento do Turso real (isso é `scripts/turso_check.py`, que depende de credenciais)."""
import json
import sqlite3
import threading
import time

import pytest

import fake_libsql
from xhakinha import dbdriver, turso_tools
from xhakinha.config import ConfigError, Settings
from xhakinha.db import Store
from xhakinha.queue import JobQueue
from xhakinha.retry import QuotaExceeded
from xhakinha.sources.quota import Quota
from conftest import SECRET


def turso_settings(tmp_path, name="a", **kw):
 url = f"libsql://fake-{ ame}.turso.io"
 fake_libsql.install(url, tmp_path / f"{ ame}.db")
 s = Settings(secret_key=SECRET, data_dir=tmp_path / "data", env="test", cookie_secure=False,
 turso_url=url, turso_token="tok", state_backend="turso", **kw)
 s.validate()
    return s


@pytest.fixture()
def tstore(tmp_path, monkeypatch):
 monkeypatch.setenv("XH_TURSO_INIT", "1")
    return Store(turso_settings(tmp_path))


def test_schema_not_created_by_accident(tmp_path, monkeypatch):
 monkeypatch.delenv("XH_TURSO_INIT", raising=False)
    with pytest.raises(ConfigError, match="turso_migrate.py init"):
        Store(turso_settings(tmp_path))


def test_init_idempotent_and_versioned(tmp_path, monkeypatch):
    monkeypatch.setenv("XH_TURSO_INIT", "1")
    s = turso_settings(tmp_path)
    a = Store(s)
    a.set("x", 1)
    monkeypatch.delenv("XH_TURSO_INIT")
    b = Store(s)                                   # segunda abertura: sem flag, esquema já existe
    assert b.get("x") == 1 and b.get("schema_version") == 5


def test_config_rules(tmp_path):
    with pytest.raises(ConfigError):
        Settings(secret_key=SECRET, data_dir=tmp_path, env="test", cookie_secure=False, state_backend="turso").validate()
    with pytest.raises(ConfigError):
        Settings(secret_key=SECRET, data_dir=tmp_path, env="test", cookie_secure=False,
                 turso_url="http://evil.example", turso_token="t").validate()
    s = Settings(secret_key=SECRET, data_dir=tmp_path, env="test", cookie_secure=False, turso_url="libsql://x.turso.io", turso_token="t")
    assert s.use_turso
    s.state_backend = "sqlite"
    assert not s.use_turso                         # rollback imediato sem apagar as variáveis


def test_row_access_lastrowid_rowcount_fallbacks(tstore):
    i = tstore.x("INSERT INTO jobs(type,payload,status,priority,max_attempts,run_at,created,updated) VALUES('t','{}','pending',5,3,0,0,0)")
    assert i == 1
    r = tstore.one("SELECT * FROM jobs WHERE id=?", (i,))
    assert r["type"] == "t" and r["status"] == "pending"
    assert tstore.conn.execute("UPDATE jobs SET status='done' WHERE id=?", (i,)).rowcount == 1
    assert tstore.conn.execute("UPDATE jobs SET status='done' WHERE id=999").rowcount == 0


def test_secrets_roundtrip_on_turso(tstore):
    tstore.put_secret("tiktok_token", "abc-123")
    assert tstore.get_secret("tiktok_token") == "abc-123"
    raw = tstore.one("SELECT * FROM secrets")
    assert "abc-123" not in json.dumps([str(v) for v in tuple(raw)])      # criptografado em repouso


def test_queue_claim_atomic_two_consumers_same_db(tmp_path, monkeypatch):
    """Render e Kaggle = dois Store/threads no MESMO banco: nenhum job pode ser entregue duas vezes."""
    monkeypatch.setenv("XH_TURSO_INIT", "1")
    s = turso_settings(tmp_path)
    q = JobQueue(Store(s))
    for k in range(30):
        q.enqueue("process_content", {"k": k}, dedupe_key=f"d{k}")
    got, lock = [], threading.Lock()

    def worker(name):
        qq = JobQueue(Store(s))
        while (j := qq.claim(name)):
            with lock:
                got.append(j["id"])
    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 30 and len(set(got)) == 30
    assert q.counts()["running"] == 30


def test_queue_claim_respects_types_priority_and_attempts(tstore):
    q = JobQueue(tstore)
    q.enqueue("b", priority=5, dedupe_key="1")
    q.enqueue("a", priority=1, dedupe_key="2")
    j = q.claim("w", types=["b"])
    assert j["type"] == "b" and j["attempts"] == 1 and j["payload"] == {} and j["locked_by"] == "w"
    assert q.claim("w", types=["b"]) is None
    assert q.claim("w")["type"] == "a"
    assert q.enqueue("a", dedupe_key="2") is None            # dedupe continua valendo no Turso


def test_queue_recover_and_retry_dead(tstore):
    q = JobQueue(tstore)
    q.enqueue("a", dedupe_key="1")
    j = q.claim("w")
    assert q.recover_all_running() == 1
    j = q.claim("w")
    q._dead(j["id"], "x", "permanent")
    assert q.retry_dead(j["id"]) is True and q.retry_dead(j["id"]) is False


def test_quota_atomic_limits(tstore):
    qt = Quota(tstore, "youtube", 100)
    qt.charge(60)
    with pytest.raises(QuotaExceeded):
        qt.charge(50)
    qt.charge(40)
    assert qt.used() == 100
    with pytest.raises(QuotaExceeded):
        qt.charge(1)
    with pytest.raises(QuotaExceeded):
        Quota(tstore, "other", 10).charge(11)                # pedido maior que o limite, mesmo sem linha prévia
    qt.mark_exhausted()
    with pytest.raises(QuotaExceeded, match="esgotada"):
        qt.charge(1)


def test_quota_concurrent_never_overspends(tmp_path, monkeypatch):
    monkeypatch.setenv("XH_TURSO_INIT", "1")
    s = turso_settings(tmp_path)
    Store(s)
    ok = []

    def w():
        qt = Quota(Store(s), "yt", 50)
        for _ in range(20):
            try:
                qt.charge(5)
                ok.append(1)
            except QuotaExceeded:
                pass
    ts = [threading.Thread(target=w) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 10 and Quota(Store(s), "yt", 50).used() == 50


def test_read_reconnects_once_but_write_never_retried(tstore):
    n = fake_libsql.STATS["connects"]
    fake_libsql.FAIL_NEXT.append("stream closed by server")
    assert tstore.one("SELECT 1 AS v")["v"] == 1             # leitura: reconectou e repetiu
    assert fake_libsql.STATS["connects"] == n + 1
    fake_libsql.FAIL_NEXT.append("stream closed by server")
    with pytest.raises(RuntimeError):                        # escrita: erro sobe (poderia duplicar efeito)
        tstore.x("INSERT INTO settings(key,value) VALUES('k','1')")
    assert tstore.one("SELECT 1 FROM settings WHERE key='k'") is None


def test_event_swallows_db_unavailable(tstore):
    fake_libsql.FAIL_NEXT.append("boom")
    tstore.event("info", "x", "mensagem")                    # log de evento nunca derruba o fluxo


def test_tx_not_available_on_turso(tstore):
    with pytest.raises(NotImplementedError):
        with tstore.tx():
            pass


def test_export_sqlite_roundtrip_and_backup(tstore, tmp_path):
    tstore.set("k", {"a": 1})
    tstore.put_secret("s", "v")
    out = tmp_path / "exp.db"
    tstore.export_sqlite(out)
    c = sqlite3.connect(out)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM settings WHERE key='k'").fetchone()[0] == 1
    c.close()
    src = tstore.backup_source()
    dst = sqlite3.connect(tmp_path / "b.db")
    src.backup(dst)
    assert dst.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 1
    dst.close()


def test_import_into_empty_then_refuse_second(tmp_path, monkeypatch):
    monkeypatch.setenv("XH_TURSO_INIT", "1")
    local = Store(Settings(secret_key=SECRET, data_dir=tmp_path / "loc", env="test", cookie_secure=False))
    q = JobQueue(local)
    q.enqueue("a", dedupe_key="1")
    q.enqueue("a", dedupe_key="2")
    local.set("z", 5)
    local.conn.execute("PRAGMA wal_checkpoint(FULL)")
    t = Store(turso_settings(tmp_path, "imp"))
    copied = turso_tools.import_sqlite(t.conn, local.s.db_path, out=lambda *_: None)
    assert copied["jobs"] == 2 and t.get("z") == 5
    with pytest.raises(RuntimeError, match="RECUSADA"):
        turso_tools.import_sqlite(t.conn, local.s.db_path, out=lambda *_: None)    # nunca mistura/sobrescreve
    assert JobQueue(t).counts()["pending"] == 2


def test_probe_passes_on_fake_and_cleans_up(tmp_path):
    url = "libsql://fake-probe.turso.io"
    fake_libsql.install(url, tmp_path / "p.db")
    import sys
    lines = []
    assert turso_tools.probe(url, "tok", module=sys.modules["libsql"], out=lines.append) is True
    c = dbdriver.LibsqlConn(url, "tok", module=sys.modules["libsql"])
    assert turso_tools.counts(c) == {}                       # nada sobrou
    assert not any("tok" == l for l in lines)


def test_probe_reports_readonly_token(tmp_path, monkeypatch):
    url = "libsql://fake-ro.turso.io"
    fake_libsql.install(url, tmp_path / "ro.db")
    import sys
    mod = sys.modules["libsql"]
    real = mod.connect

    class RO:
        def __init__(self, c): self._c = c
        def execute(self, sql, p=()):
            if sql.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DROP")):
                raise RuntimeError("Hrana: `api error: status=403 Forbidden`, token is read-only")
            return self._c.execute(sql, p)
        def __getattr__(self, n): return getattr(self._c, n)
    mod.connect = lambda u, auth_token=None: RO(real(u, auth_token))
    lines = []
    assert turso_tools.probe(url, "tok", module=mod, out=lines.append) is False
    assert any("FALHOU" in l and "criar tabela" in l for l in lines)


def test_brain_does_not_restore_snapshot_over_turso(tmp_path, monkeypatch):
    from xhakinha.cloud.brain import Brain
    import inspect
    src = inspect.getsource(Brain.prepare)
    assert "use_turso" in src and "restore_latest" in src      # guarda explícita
