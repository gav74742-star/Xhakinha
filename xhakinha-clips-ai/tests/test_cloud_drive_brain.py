"""Drive (SIM: pasta local e servidor HTTP falso imitando a API v3), snapshots, checkpoints, executor, cérebro.
REAL: ffmpeg/análise/render de vídeo SINTÉTICO, SQLite, threads, HTTP local. NÃO REAL: Google Drive, Kaggle, Colab."""
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

from fake_drive import FakeDrive
from xhakinha import analysis
from xhakinha.cloud.api import create_app
from xhakinha.cloud.brain import Brain
from xhakinha.cloud.checkpoint import ChunkStore
from xhakinha.cloud.client import HttpClient, LocalClient
from xhakinha.cloud.coordinator import Coordinator, Fenced
from xhakinha.cloud.drive_storage import (DriveFull, GoogleDriveBackend, IntegrityError, LocalDirBackend,
                                           md5_file, sha256_file)
from xhakinha.cloud.executor import Executor
from xhakinha.cloud.handlers import default_handlers
from xhakinha.cloud.snapshots import CorruptState, SnapshotManager
from xhakinha.retry import PermanentError, QuotaExceeded, TransientError

CFG = {"chunk_min": 0.5, "chunk_overlap_s": 5, "min_clip_score": 30, "clip_min_s": 15, "clip_pref_max_s": 40,
       "clip_hard_max_s": 60, "clips_per_hour_of_video": 6}


@pytest.fixture()
def ldrive(tmp_path):
    d = LocalDirBackend(tmp_path / "drive"); d.ensure_layout(); return d


@pytest.fixture()
def fdrive():
    f = FakeDrive()
    yield f
    f.close()


def gdrive(f, chunk=256 * 1024):
    return GoogleDriveBackend("cid", "cs", "rt", api=f.base, token_url=f.base + "/token", chunk=chunk)


# ---------------------------------------------------------------- Drive (pasta local = SIM)
def test_upload_verificado_idempotente_e_sem_sobrescrita(ldrive, tmp_path):
    f = tmp_path / "a.bin"; f.write_bytes(b"x" * 1000)
    r = ldrive.upload(f, "clips/pending/a.bin"); assert not r["reused"]
    assert ldrive.upload(f, "clips/pending/a.bin")["reused"]
    f.write_bytes(b"y" * 1000)
    with pytest.raises(IntegrityError):
        ldrive.upload(f, "clips/pending/a.bin")


def test_falha_de_upload_nao_deixa_arquivo_parcial_e_retentativa_funciona(ldrive, tmp_path):
    f = tmp_path / "a.bin"; f.write_bytes(b"z" * 5000)
    ldrive.fail_next_uploads = 1
    with pytest.raises(TransientError):
        ldrive.upload(f, "originals/a.bin")
    assert ldrive.stat("originals/a.bin") is None
    assert ldrive.upload(f, "originals/a.bin")["md5"] == md5_file(f)


def test_upload_corrompido_e_detectado_pelo_md5(ldrive, tmp_path):
    f = tmp_path / "a.bin"; f.write_bytes(b"q" * 100)
    ldrive.corrupt_next_upload = True
    with pytest.raises(TransientError, match="verificação"):
        ldrive.upload(f, "originals/c.bin")


def test_drive_sem_espaco_recusa_sem_tocar_no_existente(tmp_path):
    d = LocalDirBackend(tmp_path / "d", capacity=1_000_000); d.ensure_layout(); d.min_free_bytes = 0
    a = tmp_path / "a"; a.write_bytes(b"1" * 600_000); d.upload(a, "originals/a")
    b = tmp_path / "b"; b.write_bytes(b"2" * 600_000)
    with pytest.raises(DriveFull):
        d.upload(b, "originals/b")
    assert d.stat("originals/a") and not d.stat("originals/b")


def test_download_atomico_com_hash(ldrive, tmp_path):
    f = tmp_path / "a.bin"; f.write_bytes(b"d" * 3000); ldrive.upload(f, "originals/a.bin")
    out = ldrive.download("originals/a.bin", tmp_path / "o" / "a.bin", sha256=sha256_file(f))
    assert out.read_bytes() == f.read_bytes() and not list((tmp_path / "o").glob("*.part"))
    with pytest.raises(IntegrityError):
        ldrive.download("originals/a.bin", tmp_path / "o" / "b.bin", sha256="0" * 64)
    assert not (tmp_path / "o" / "b.bin").exists()


def test_originais_e_clipes_nunca_podem_ser_apagados(ldrive, tmp_path):
    f = tmp_path / "a"; f.write_bytes(b"1"); ldrive.upload(f, "originals/a"); ldrive.upload(f, "clips/pending/a")
    for p in ("originals/a", "clips/pending/a", "clips/published/a"):
        with pytest.raises(PermissionError):
            ldrive.delete(p)
    ldrive.upload(f, "tmp/x"); ldrive.delete("tmp/x")


# ---------------------------------------------------------------- backend Google Drive (API falsa = SIM do servidor)
def test_google_backend_upload_resumivel_multichunk_download_md5(fdrive, tmp_path):
    g = gdrive(fdrive); g.ensure_layout()
    f = tmp_path / "big.bin"; f.write_bytes(bytes(range(256)) * 4096 * 3)          # ~3 MB, vários chunks de 256 KiB
    r = g.upload(f, "originals/big.bin")
    assert r["md5"] == md5_file(f) and sum(1 for m, p in fdrive.requests if m == "PUT") >= 12
    assert g.upload(f, "originals/big.bin")["reused"]
    out = g.download("originals/big.bin", tmp_path / "back.bin")
    assert out.read_bytes() == f.read_bytes()


def test_google_backend_retoma_upload_apos_erro_503(fdrive, tmp_path):
    g = gdrive(fdrive); g.ensure_layout()
    f = tmp_path / "x.bin"; f.write_bytes(b"k" * (700 * 1024))
    fdrive.fail_put_once = 2
    assert g.upload(f, "clips/pending/x.bin")["md5"] == md5_file(f)
    assert sum(1 for fl in fdrive.files.values() if fl["name"] == "x.bin") == 1     # sem duplicata


def test_google_backend_drive_cheio_e_quota(fdrive, tmp_path):
    fdrive.limit = 1000
    g = gdrive(fdrive); g.ensure_layout(); g.min_free_bytes = 0
    f = tmp_path / "x.bin"; f.write_bytes(b"k" * 5000)
    with pytest.raises(DriveFull):
        g.upload(f, "originals/x.bin")
    assert g.quota()["limit"] == 1000


def test_google_backend_token_revogado_e_erro_claro(fdrive):
    fdrive.fail_token = True
    g = gdrive(fdrive)
    with pytest.raises(PermanentError, match="refresh token"):
        g.quota()


def test_google_backend_listagem_json_e_delete_para_lixeira(fdrive, tmp_path):
    g = gdrive(fdrive); g.ensure_layout()
    g.write_json("checkpoints/abc/x.json", {"a": 1})
    assert g.read_json("checkpoints/abc/x.json") == {"a": 1}
    assert [x["path"] for x in g.listdir("checkpoints/abc")] == ["checkpoints/abc/x.json"]
    g.delete("checkpoints/abc/x.json")
    assert g.stat("checkpoints/abc/x.json") is None
    with pytest.raises(PermissionError):
        g.delete("originals/x")


# ---------------------------------------------------------------- snapshots
def mkdb(p, rows=3):
    c = sqlite3.connect(str(p)); c.execute("CREATE TABLE contents(id)"); c.execute("CREATE TABLE clips(id)")
    c.executemany("INSERT INTO contents VALUES(?)", [(i,) for i in range(rows)]); c.commit(); return c


def test_snapshot_roundtrip_verificado(ldrive, tmp_path):
    c = mkdb(tmp_path / "s.db", 5)
    sm = SnapshotManager(ldrive, tmp_path / "t")
    m = sm.create(c, 1)
    assert m["counts"]["contents"] == 5
    dest = tmp_path / "r" / "x.db"
    assert sm.restore_latest(dest)["file"] == m["file"]
    assert sqlite3.connect(str(dest)).execute("select count(*) from contents").fetchone()[0] == 5


def test_snapshot_corrompido_cai_para_versao_anterior_e_nunca_inicia_vazio(ldrive, tmp_path):   # inconsistência de banco
    c = mkdb(tmp_path / "s.db", 2)
    sm = SnapshotManager(ldrive, tmp_path / "t")
    sm.create(c, 1); time.sleep(1.1)
    c.execute("INSERT INTO contents VALUES(99)"); c.commit()
    m2 = sm.create(c, 2)
    (ldrive.root / m2["file"]).write_bytes(b"lixo")                                   # corrompe o mais novo
    dest = tmp_path / "r.db"
    got = sm.restore_latest(dest)
    assert got["epoch"] == 1 and sqlite3.connect(str(dest)).execute("select count(*) from contents").fetchone()[0] == 2
    for f in (ldrive.root / "db/snapshots").glob("*.gz"):
        f.write_bytes(b"lixo")
    with pytest.raises(CorruptState):
        sm.restore_latest(tmp_path / "r2.db")
    for f in (ldrive.root / "db/snapshots").glob("*.json"):
        f.unlink()
    with pytest.raises(CorruptState):
        sm.restore_latest(tmp_path / "r3.db")


def test_cerebro_antigo_fenced_nao_publica_manifesto(ldrive, tmp_path):
    c = mkdb(tmp_path / "s.db"); sm = SnapshotManager(ldrive, tmp_path / "t")
    with pytest.raises(Fenced):
        sm.create(c, 1, still_owner=lambda: False)
    assert sm.manifests() == []                                                       # arquivo órfão, mas sem ponteiro


def test_snapshot_de_epoch_antigo_nao_supera_o_novo(ldrive, tmp_path):
    c = mkdb(tmp_path / "s.db"); sm = SnapshotManager(ldrive, tmp_path / "t")
    sm.create(c, 5); time.sleep(1.1); sm.create(c, 4)
    assert sm.manifests()[0]["epoch"] == 5


# ---------------------------------------------------------------- checkpoints
def test_chunkstore_primeiro_a_gravar_vence_e_lista(ldrive):
    cs = ChunkStore(ldrive, "a" * 16)
    assert cs.save("analysis", 0, 30, [1]) and not cs.save("analysis", 0, 30, [2])
    assert cs.load("analysis", 0, 30) == [1] and cs.done("analysis") == [(0.0, 30.0)]
    with pytest.raises(ValueError):
        ChunkStore(ldrive, "../x")


# ---------------------------------------------------------------- processamento distribuído (REAL: ffmpeg/análise)
def put_original(drive, synth_video):
    key = sha256_file(synth_video)[:32]
    drive.upload(synth_video, f"originals/{key}.mp4")
    return key


def submit_process(co, key, mx=2):
    return co.submit("process_content", {"content_key": key, "drive_path": f"originals/{key}.mp4", "cfg": CFG, "used": [],
                                         "max_clips": mx, "whisper": False}, key=f"process:{key}", est_seconds=200)


class Clock:
    def __init__(self): self.t = 1_800_000_000.0
    def __call__(self): return self.t


def test_kaggle_interrompido_colab_retoma_dos_checkpoints(ldrive, synth_video, tmp_path, monkeypatch):
    ck = Clock(); co = Coordinator(":memory:", clock=ck); cl = LocalClient(co)
    key = put_original(ldrive, synth_video); submit_process(co, key)
    real, calls = analysis.analyze_range, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise TransientError("sessão Kaggle caiu")
        return real(*a, **k)
    monkeypatch.setattr(analysis, "analyze_range", flaky)
    kaggle = Executor("kaggle1", "kaggle-gpu", cl, ldrive, default_handlers(), workdir=tmp_path / "k", caps={"gpu": True}, session_seconds=40000, clock=ck)
    kaggle.start(); assert kaggle.run_one() and kaggle.stats["failed"] == 1
    done_before = len(ChunkStore(ldrive, key).done("analysis"))
    assert done_before == 2                                                           # 2 pedaços salvos no Drive antes da queda
    monkeypatch.setattr(analysis, "analyze_range", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1])
    ck.t += 4000
    colab = Executor("colab1", "colab", cl, ldrive, default_handlers(), workdir=tmp_path / "c", session_seconds=20000, clock=ck)
    colab.start(); before = calls["n"]
    assert colab.run_one() and colab.stats["done"] == 1
    t = co.get_by_key(f"process:{key}"); res = json.loads(t["result"])
    assert res["chunks_reused"] == 2 and res["executor"] == "colab1"
    assert calls["n"] - before == res["chunks"] - 2                                    # só os pedaços restantes foram recalculados
    assert res["clips"] and all(ldrive.stat(c["path"]) for c in res["clips"])
    assert (tmp_path / "c").exists() and ldrive.stat(res["clips"][0]["path"].replace(".mp4", ".json"))


def test_tarefa_repetida_reaproveita_clipes_sem_duplicar(ldrive, synth_video, tmp_path):
    co = Coordinator(":memory:"); cl = LocalClient(co)
    key = put_original(ldrive, synth_video)
    t1 = submit_process(co, key)
    ex = Executor("e1", "local", cl, ldrive, default_handlers(), workdir=tmp_path / "w"); ex.start(); ex.run_one()
    n1 = len([f for f in ldrive.listdir("clips/pending") if f["path"].endswith(".mp4")])
    co.submit("process_content", json.loads(co.get_task(t1["id"])["payload"]), key="outra-chave")      # mesma carga, nova tarefa
    ex.run_one()
    n2 = len([f for f in ldrive.listdir("clips/pending") if f["path"].endswith(".mp4")])
    assert n1 == n2 and n1 >= 1


def test_executor_antigo_apos_perder_lease_nao_confirma_resultado(ldrive, tmp_path):
    ck = Clock(); co = Coordinator(":memory:", clock=ck); cl = LocalClient(co)
    co.submit("sleep", {"seconds": 0.3}, key="s")
    hits = []

    def slow(ctx):
        ck.t += 10_000                                    # simula pausa longa; coordenador recupera e outro assume
        co.reap(); ck.t += 10; co.register("outro", "colab", {}, 99999); t2 = co.claim("outro"); hits.append(t2["fence"])
        co.complete(t2["id"], "outro", t2["fence"], {"quem": "outro"})
        return {"quem": "antigo"}
    ex = Executor("antigo", "kaggle-cpu", cl, ldrive, {"sleep": slow}, workdir=tmp_path / "w", clock=ck)
    ex.start(); ex.run_one()
    t = co.get_by_key("s")
    assert json.loads(t["result"]) == {"quem": "outro"} and ex.stats["fenced"] == 1 and hits == [2]


def test_sessao_terminando_libera_tarefa_sem_penalidade(ldrive, tmp_path):
    co = Coordinator(":memory:"); cl = LocalClient(co); co.submit("sleep", {"seconds": 5}, key="s")
    ex = Executor("e", "local", cl, ldrive, default_handlers(), workdir=tmp_path / "w"); ex.start()
    th = threading.Thread(target=ex.run_one); th.start(); time.sleep(0.4); ex.shutdown(); th.join(5)
    t = co.get_by_key("s"); assert t["status"] == "queued" and t["attempts"] == 0


def test_drive_cheio_em_tarefa_pausa_provedor_em_vez_de_perder_dados(synth_video, tmp_path):
    d = LocalDirBackend(tmp_path / "d", capacity=synth_video.stat().st_size + 150_000); d.ensure_layout(); d.min_free_bytes = 0
    co = Coordinator(":memory:"); cl = LocalClient(co)
    key = put_original(d, synth_video); submit_process(co, key)
    ex = Executor("e", "colab", cl, d, default_handlers(), workdir=tmp_path / "w"); ex.start(); ex.run_one()
    t = co.get_by_key(f"process:{key}")
    assert t["status"] == "queued" and "Drive" in (t["error"] or "") and d.stat(f"originals/{key}.mp4")   # original intacto
    assert co.provider("colab")["paused_until"] > co.clock()


# ---------------------------------------------------------------- API HTTP (REAL, localhost)
def test_api_http_auth_e_fencing(tmp_path):
    co = Coordinator(":memory:")
    app = create_app(co, "EXEC-TOKEN-1234567890", "ADMIN-TOKEN-1234567890")
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try: requests.get(base + "/api/health", timeout=1); break
        except Exception: time.sleep(0.05)
    assert requests.post(base + "/api/exec/claim", json={}).status_code == 401
    assert requests.get(base + "/api/state", headers={"Authorization": "Bearer EXEC-TOKEN-1234567890"}).status_code == 401   # sem X-Requested-With
    h = HttpClient(base, "EXEC-TOKEN-1234567890")
    h.register("e1", "local", {}, 3600); h.submit("sleep", {}, key="k")
    t = h.claim("e1"); assert t["fence"] == 1
    h.checkpoint(t["id"], "e1", 1, {"p": 1}); h.complete(t["id"], "e1", 1, {"ok": 1})
    with pytest.raises(Fenced):
        h.complete(t["id"], "e1", 1, {"ok": 2})
    assert h.state()["counts"]["done"] == 1 and h.lock_acquire("brain", "e1") == 1
    assert HttpClient(base, "errado").__class__ and requests.get(base + "/api/state", headers={"Authorization": "Bearer x", "X-Requested-With": "XHK"}).status_code == 401
    srv.should_exit = True


# ---------------------------------------------------------------- cérebro
def test_cerebro_exclusivo_dois_ao_mesmo_tempo(settings, ldrive):
    co = Coordinator(":memory:"); cl = LocalClient(co)
    b1 = Brain(settings, ldrive, cl, "A"); b2 = Brain(settings, ldrive, cl, "B")
    assert b1.acquire() and not b2.acquire()
    assert b2.run(10)["status"] == "outro_cerebro_ativo"


def test_cerebro_sincroniza_ingere_remoto_e_so_publica_com_lock(settings, ldrive, synth_video, tmp_path, monkeypatch):
    """REAL: Engine/SQLite/ffmpeg. Fluxo: conteúdo → upload do original → tarefa remota → ingestão verificada → snapshot."""
    from conftest import make_content as mk
    ck = Clock(); co = Coordinator(":memory:", clock=ck); cl = LocalClient(co)
    brain = Brain(settings, ldrive, cl, "brain-1", clock=ck)
    assert brain.acquire(); brain.prepare()
    eng = brain.engine
    cid = mk(eng, synth_video)
    for k, v in CFG.items():
        eng.db.set(k, v)
    job = {"payload": {"content_id": cid, "max_clips": 2}, "type": "process"}
    from xhakinha.retry import NotReadyYet
    with pytest.raises(NotReadyYet):
        eng.pl.h_process(job)                                                        # envia ao coordenador e espera
    fp = eng.db.one("SELECT fingerprint FROM contents WHERE id=?", (cid,))["fingerprint"][:32]
    assert any(f["path"].startswith("originals/") for f in ldrive.listdir("originals")) and co.get_by_key(f"process:{fp}")
    ex = Executor("kaggle-x", "kaggle-gpu", cl, ldrive, default_handlers(), workdir=tmp_path / "w", caps={"gpu": True}, session_seconds=40000, clock=ck)
    ex.start(); assert ex.run_one() and ex.stats["done"] == 1
    eng.pl.h_process(job)                                                            # agora ingere
    c = eng.db.one("SELECT * FROM contents WHERE id=?", (cid,)); assert c["status"] == "processed"
    clips = eng.db.q("SELECT * FROM clips WHERE content_id=?", (cid,))
    assert clips and all(Path(x["path"]).is_file() and sha256_file(Path(x["path"])) == x["sha256"] for x in clips)
    eng.pl.h_process(job)                                                            # repetição: idempotente
    assert len(eng.db.q("SELECT id FROM clips WHERE content_id=?", (cid,))) == len(clips)
    assert brain.sync_files() == 0 and ldrive.stat(f"originals/{fp}.mp4")                 # nada a reenviar: sem cópias duplicadas
    assert all(x["drive_path"] and ldrive.stat(x["drive_path"]) for x in eng.db.q("SELECT drive_path FROM clips WHERE content_id=?", (cid,)))
    assert not [f for f in ldrive.listdir("clips/pending") if "/clip_" in f["path"]]
    meta = brain.snapshot(); assert meta and meta["counts"]["clips"] == len(clips)
    # publicação interrompida: perde o lock → h_publish recusa ANTES de chamar a plataforma
    called = []
    ck.t += 1000; co.reap(); co.lock_acquire("brain", "outro", 100)
    with pytest.raises(NotReadyYet):
        eng.pl.h_publish({"payload": {"publication_id": 1}, "type": "publish"})
    assert not called and brain.fenced.is_set()
    assert brain.snapshot() is None                                                  # sem lock, sem snapshot



# TESTES DE RECUPERACAO DE ORIGINAIS - XHAKINHA

def _setup_original_recovery(settings, ldrive, synth_video):
    """Prepara Brain, conteúdo e cópia remota usando somente Drive local de teste."""
    from conftest import make_content
    from xhakinha.cloud.brain import Brain
    from xhakinha.cloud.coordinator import Coordinator

    co = Coordinator(":memory:")
    client = LocalClient(co)
    brain = Brain(settings, ldrive, client, "rehydrate-test")

    assert brain.acquire()
    brain.prepare()

    cid = make_content(brain.engine, synth_video)
    content = brain.engine.db.one(
        "SELECT * FROM contents WHERE id=?", (cid,)
    )

    fingerprint = content["fingerprint"]
    drive_path = f"originals/{fingerprint[:32]}.mp4"

    local_original = Path(content["local_path"])
    ldrive.upload(local_original, drive_path)
    local_original.unlink(missing_ok=True)

    brain.engine.db.x(
        "UPDATE contents SET drive_path=?, local_path=NULL WHERE id=?",
        (drive_path, cid),
    )

    content = dict(brain.engine.db.one(
        "SELECT * FROM contents WHERE id=?", (cid,)
    ))

    return brain, content, drive_path


def test_rehydrate_original_restauracao_com_fingerprint(
    settings, ldrive, synth_video
):
    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )

    result = brain.rehydrate_original(content)

    assert result == "restored"

    restored = brain.engine.db.one(
        "SELECT local_path, drive_path, fingerprint FROM contents WHERE id=?",
        (content["id"],),
    )

    from xhakinha.storage import quick_fingerprint

    path = Path(restored["local_path"])
    assert path.is_file()
    assert restored["drive_path"] == drive_path
    assert quick_fingerprint(path, content["duration"]) == content["fingerprint"]
    assert brain.report["originals_rehydrated"] == 1


def test_rehydrate_original_arquivo_ausente_no_drive(
    settings, ldrive, synth_video
):
    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )

    remote_path = ldrive.root / drive_path
    assert remote_path.is_file()
    remote_path.unlink()

    result = brain.rehydrate_original(content)

    assert result == "missing"
    assert not Path(brain.engine.st.download_path(content["id"], "mp4")).exists()

    saved = brain.engine.db.one(
        "SELECT local_path FROM contents WHERE id=?", (content["id"],)
    )
    assert saved["local_path"] is None


def test_rehydrate_original_rejeita_arquivo_corrompido(
    settings, ldrive, synth_video
):
    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )

    remote_path = ldrive.root / drive_path
    remote_path.write_bytes(b"arquivo corrompido propositalmente")

    result = brain.rehydrate_original(content)

    assert result == "corrupt"
    assert not Path(brain.engine.st.download_path(content["id"], "mp4")).exists()

    saved = brain.engine.db.one(
        "SELECT local_path FROM contents WHERE id=?", (content["id"],)
    )
    assert saved["local_path"] is None


def test_rehydrate_original_sem_espaco_local_tenta_novamente(
    settings, ldrive, synth_video, monkeypatch
):
    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )

    monkeypatch.setattr(
        brain.engine.st, "has_space", lambda required=0: False
    )

    result = brain.rehydrate_original(content)

    assert result == "retry"
    assert ldrive.stat(drive_path) is not None
    assert not Path(brain.engine.st.download_path(content["id"], "mp4")).exists()

    saved = brain.engine.db.one(
        "SELECT local_path FROM contents WHERE id=?", (content["id"],)
    )
    assert saved["local_path"] is None


# ----------------------------------------------------------------
# INTEGRACAO REMOTA COM ORIGINAL AUSENTE NO COORDENADOR

def test_remote_processa_original_disponivel_so_no_drive(
    settings, ldrive, synth_video
):
    from xhakinha.retry import NotReadyYet

    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )

    job = {
        "payload": {"content_id": content["id"], "max_clips": 2},
        "type": "process",
    }

    with pytest.raises(NotReadyYet):
        brain.engine.pl.h_process(job)

    key = content["fingerprint"][:32]
    task = brain.client.get_by_key(f"process:{key}")

    assert task is not None
    payload = json.loads(task["payload"])
    assert payload["drive_path"] == drive_path
    assert payload["content_key"] == key
    assert ldrive.stat(drive_path) is not None

    saved = brain.engine.db.one(
        "SELECT local_path, drive_path FROM contents WHERE id=?",
        (content["id"],),
    )
    assert saved["local_path"] is None
    assert saved["drive_path"] == drive_path


def test_fallback_local_reidrata_original_antes_de_processar(
    settings, ldrive, synth_video, monkeypatch
):
    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )

    called = []
    monkeypatch.setattr(
        brain.client,
        "get_by_key",
        lambda key: {"status": "dead", "created": brain.clock()},
    )

    def fake_process(content_id, max_clips=None):
        called.append((content_id, max_clips))
        return "processado-localmente"

    monkeypatch.setattr(
        brain.engine.pl, "process_content", fake_process
    )

    job = {
        "payload": {"content_id": content["id"], "max_clips": 2},
        "type": "process",
    }

    result = brain.engine.pl.h_process(job)

    assert result is None
    assert called == [(content["id"], 2)]

    saved = brain.engine.db.one(
        "SELECT local_path, drive_path FROM contents WHERE id=?",
        (content["id"],),
    )
    assert saved["local_path"]
    assert Path(saved["local_path"]).is_file()
    assert saved["drive_path"] == drive_path
    assert ldrive.stat(drive_path) is not None
    assert brain.report["originals_rehydrated"] == 1
    assert brain.report["fallback_local"] == 1


def test_fallback_local_adia_quando_reidratacao_precisa_retry(
    settings, ldrive, synth_video, monkeypatch
):
    from xhakinha.retry import NotReadyYet

    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )

    monkeypatch.setattr(
        brain.client,
        "get_by_key",
        lambda key: {"status": "dead", "created": brain.clock()},
    )
    monkeypatch.setattr(
        brain, "rehydrate_original", lambda c: "retry"
    )

    called = []
    monkeypatch.setattr(
        brain.engine.pl,
        "process_content",
        lambda job: called.append(job),
    )

    job = {
        "payload": {"content_id": content["id"], "max_clips": 2},
        "type": "process",
    }

    with pytest.raises(NotReadyYet, match="restauração do original"):
        brain.engine.pl.h_process(job)

    assert called == []
    assert brain.report["fallback_local"] == 0
    assert ldrive.stat(drive_path) is not None

    saved = brain.engine.db.one(
        "SELECT local_path, drive_path FROM contents WHERE id=?",
        (content["id"],),
    )
    assert saved["local_path"] is None
    assert saved["drive_path"] == drive_path

def test_fallback_local_bloqueia_original_missing_ou_corrupt(
    settings, ldrive, synth_video, monkeypatch
):
    import pytest
    from xhakinha.retry import PermanentError

    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )
    called = []

    monkeypatch.setattr(
        brain.client,
        "get_by_key",
        lambda key: {"status": "dead", "created": brain.clock()},
    )
    monkeypatch.setattr(
        brain.engine.pl,
        "process_content",
        lambda *args, **kwargs: called.append(args),
    )

    job = {
        "payload": {"content_id": content["id"], "max_clips": 2},
        "type": "process",
    }

    for status in ("missing", "corrupt"):
        monkeypatch.setattr(
            brain, "rehydrate_original",
            lambda c, result=status: result,
        )
        with pytest.raises(PermanentError, match="irrecuperável no Drive"):
            brain.engine.pl.h_process(job)

    assert called == []
    assert brain.report["fallback_local"] == 0
    assert ldrive.stat(drive_path) is not None


def test_fallback_local_substitui_copia_local_com_fingerprint_invalido(
    settings, ldrive, synth_video, monkeypatch
):
    from xhakinha.storage import quick_fingerprint

    brain, content, drive_path = _setup_original_recovery(
        settings, ldrive, synth_video
    )

    # Recria um arquivo local inválido no destino esperado da restauração.
    dest = brain.engine.st.download_path(content["id"], "mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"arquivo local incompleto e invalido")

    brain.engine.db.x(
        "UPDATE contents SET local_path=? WHERE id=?",
        (str(dest), content["id"]),
    )

    called = []
    monkeypatch.setattr(
        brain.client,
        "get_by_key",
        lambda key: {"status": "dead", "created": brain.clock()},
    )

    def fake_process(content_id, max_clips=None):
        called.append((content_id, max_clips))

    monkeypatch.setattr(
        brain.engine.pl, "process_content", fake_process
    )

    job = {
        "payload": {"content_id": content["id"], "max_clips": 2},
        "type": "process",
    }
    brain.engine.pl.h_process(job)

    saved = brain.engine.db.one(
        "SELECT local_path, drive_path FROM contents WHERE id=?",
        (content["id"],),
    )

    assert called == [(content["id"], 2)]
    assert saved["local_path"] == str(dest)
    assert dest.is_file()
    assert quick_fingerprint(dest, content["duration"]) == content["fingerprint"]
    assert saved["drive_path"] == drive_path
    assert ldrive.stat(drive_path) is not None
    assert brain.report["originals_rehydrated"] == 1
    assert brain.report["fallback_local"] == 1

# ----------------------------------------------------------------
# INTEGRIDADE: BLOQUEIO DE UPLOAD DE ORIGINAIS DIVERGENTES

def _setup_original_without_drive(settings, ldrive, synth_video):
    from conftest import make_content
    from xhakinha.cloud.brain import Brain
    from xhakinha.cloud.coordinator import Coordinator

    co = Coordinator(":memory:")
    client = LocalClient(co)
    brain = Brain(settings, ldrive, client, "integrity-upload-test")
    assert brain.acquire()
    brain.prepare()

    cid = make_content(brain.engine, synth_video)
    content = dict(brain.engine.db.one(
        "SELECT * FROM contents WHERE id=?", (cid,)
    ))
    return brain, content


def test_upload_originals_bloqueia_fingerprint_divergente(
    settings, ldrive, synth_video
):
    brain, content = _setup_original_without_drive(
        settings, ldrive, synth_video
    )
    local_path = Path(content["local_path"])
    expected_path = (
        f"originals/{content['fingerprint'][:32]}{local_path.suffix}"
    )

    # Altera o arquivo sem atualizar o fingerprint persistido.
    local_path.write_bytes(b"conteudo local adulterado")

    uploaded = brain.sync_files()

    saved = brain.engine.db.one(
        "SELECT drive_path FROM contents WHERE id=?",
        (content["id"],),
    )
    events = brain.engine.db.q(
        "SELECT * FROM events WHERE component='integrity'"
    )

    assert uploaded == 0
    assert saved["drive_path"] is None
    assert ldrive.stat(expected_path) is None
    assert events


def test_upload_imediato_bloqueia_fingerprint_divergente(
    settings, ldrive, synth_video
):
    from xhakinha.retry import PermanentError

    brain, content = _setup_original_without_drive(
        settings, ldrive, synth_video
    )
    local_path = Path(content["local_path"])
    expected_path = (
        f"originals/{content['fingerprint'][:32]}{local_path.suffix}"
    )

    # O registro continua apontando para o fingerprint original.
    local_path.write_bytes(b"conteudo local adulterado")

    job = {
        "payload": {"content_id": content["id"], "max_clips": 2},
        "type": "process",
    }

    with pytest.raises(PermanentError, match="fingerprint divergente"):
        brain.engine.pl.h_process(job)

    saved = brain.engine.db.one(
        "SELECT drive_path FROM contents WHERE id=?",
        (content["id"],),
    )

    assert saved["drive_path"] is None
    assert ldrive.stat(expected_path) is None
