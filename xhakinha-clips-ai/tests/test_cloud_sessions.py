"""Continuidade ENTRE SESSÕES (Kaggle efêmero): reidratação de clipes, diário de publicações, pausa de ingestão.
REAL: Engine/SQLite/ffmpeg (vídeo sintético), snapshots, lock do cérebro, nosso código de publicação e de recuperação.
SIM: Drive = pasta local; TikTok = servidor falso (tests/fake_platforms.py). Não prova integração com serviços reais."""
import json
import shutil
import time
from pathlib import Path

import pytest

from conftest import SECRET, make_content
from test_regression_critical import _drain, _ready_clip, _tt_pub, _tt_setup
from xhakinha.cloud.brain import Brain
from xhakinha.cloud.client import LocalClient
from xhakinha.cloud.coordinator import Coordinator
from xhakinha.cloud.drive_storage import LocalDirBackend
from xhakinha.cloud.snapshots import SnapshotManager
from xhakinha.config import Settings
from xhakinha.retry import NotReadyYet, TransientError
from xhakinha.storage import sha256_file

TOKEN = "TT-SECRET-TOKEN-XYZ-123"


class Cloud:
    """Coordenador + Drive persistentes; cada `session()` é uma máquina nova com disco vazio."""

    def __init__(self, tmp, fake, capacity=None):
        self.tmp, self.fake = tmp, fake
        self.drive = LocalDirBackend(tmp / "drive", capacity=capacity); self.drive.min_free_bytes = 0
        self.co = Coordinator(":memory:"); self.cl = LocalClient(self.co)
        self.n = 0

    def session(self, **kw) -> Brain:
        self.n += 1
        s = Settings(secret_key=SECRET, data_dir=self.tmp / f"sess{self.n}", env="test", cookie_secure=False,
                     retry_base_s=1, retry_cap_s=5, max_workers=2, min_free_gb=0.01, whisper_enabled=False)
        s.validate(); s.ensure_dirs()
        b = Brain(s, self.drive, self.cl, f"brain-{self.n}", remote_processing=False, **kw)
        assert b.acquire() and b.fence, "não conseguiu o lock do cérebro"
        b.prepare()
        b.engine.db.set("whisper", 0)
        s.tiktok_api = self.fake.base
        return b

    def crash(self, b: Brain) -> None:
        """Sessão morre: disco local some, lock NÃO é liberado, sem snapshot final."""
        b.engine.db.conn.close()
        shutil.rmtree(b.s.data_dir, ignore_errors=True)
        self.co.c.execute("UPDATE locks SET lease_until=0 WHERE name='brain'")      # o lease vence (ninguém o renova mais)


@pytest.fixture()
def cloud(tmp_path, fake):
    return Cloud(tmp_path, fake)


def seed(b: Brain, fake, synth_video):
    """Sessão 1: processa um vídeo, gera clipe, sincroniza no Drive, agenda publicação TikTok e salva snapshot."""
    eng = b.engine
    _tt_setup(eng, fake, audited=1)
    eng.db.put_secret("account:tiktok", {"access_token": TOKEN, "refresh_token": "R", "expires_at": time.time() + 9999, "username": "conta"})
    clip = _ready_clip(eng, synth_video)
    pid = _tt_pub(eng, clip)
    assert b.sync_files() >= 2
    b.snapshot()
    return clip, pid


# ======================================================================= cenário principal (itens 1–8)
def test_sessao_morre_nova_sessao_reidrata_e_publica_sem_duplicar(cloud, fake, synth_video):
    fake.flags["tt_status_seq"] = ["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]
    b1 = cloud.session()
    clip, pid = seed(b1, fake, synth_video)                               # 1 processar  2 enviar ao Drive
    row = b1.engine.db.one("SELECT * FROM clips WHERE id=?", (clip["id"],))
    assert row["drive_path"] and cloud.drive.stat(row["drive_path"])
    drive_before = sorted((f["path"], f["size"]) for f in cloud.drive.listdir("clips") + cloud.drive.listdir("originals"))
    cloud.crash(b1)                                                       # 3 sessão Kaggle encerra (sem aviso)

    b2 = cloud.session()                                                  # 4 outra sessão inicia
    db2 = b2.engine.db
    c2 = db2.one("SELECT * FROM clips WHERE id=?", (clip["id"],))
    assert c2["id"] == clip["id"] and "sess1" not in c2["path"]           # 5 mesmo id/registro, novo arquivo local
    assert Path(c2["path"]).is_file() and sha256_file(Path(c2["path"])) == clip["sha256"] and b2.report["rehydrated"] == 1
    assert b2.sync_files() == 0                                           # nada reenviado
    assert sorted((f["path"], f["size"]) for f in cloud.drive.listdir("clips") + cloud.drive.listdir("originals")) == drive_before

    assert b2.engine.run_one()                                            # 6 retoma a publicação (job veio no snapshot)
    p = db2.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "processing" and p["remote_id"] == "pid1" and fake.n["tt_upload"] == 1
    cloud.crash(b2)                                                       # 7 nova queda NO MEIO da publicação, sem snapshot

    b3 = cloud.session()                                                  # 8 nova tentativa: diário do Drive evita reiniciar
    db3 = b3.engine.db
    p3 = db3.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p3["remote_id"] == "pid1" and json.loads(p3["state"])["stage"] == "poll" and b3.report["journal_applied"] >= 1
    _drain(b3.engine)
    p3 = db3.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p3["status"] == "published" and p3["url"] == "https://www.tiktok.com/@conta/video/7123456789"
    assert fake.n["tt_init"] == 1 and fake.n["tt_upload"] == 1            # UM post, UM upload, em três sessões
    assert db3.one("SELECT status FROM clips WHERE id=?", (clip["id"],))["status"] == "published"


def test_publicacao_concluida_antes_da_queda_nao_e_refeita(cloud, fake, synth_video):
    fake.flags["tt_status_seq"] = ["PUBLISH_COMPLETE"]
    b1 = cloud.session(); clip, pid = seed(b1, fake, synth_video); cloud.crash(b1)
    b2 = cloud.session(); _drain(b2.engine)
    assert b2.engine.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"
    cloud.crash(b2)                                                       # caiu ANTES de salvar snapshot com 'published'
    b3 = cloud.session()
    assert b3.engine.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"   # veio do diário
    _drain(b3.engine)
    assert fake.n["tt_init"] == 1 and fake.n["tt_upload"] == 1
    assert b3.engine.db.one("SELECT status FROM jobs WHERE dedupe_key=?", (f"pub:{pid}",))["status"] == "done"


def test_falha_temporaria_durante_publicacao_retoma_sem_duplicar(cloud, fake, synth_video):
    """Drive fora do ar no meio da publicação: o passo seguinte NÃO acontece (diário obrigatório) e a retomada não duplica."""
    fake.flags["tt_status_seq"] = ["PUBLISH_COMPLETE"]
    b1 = cloud.session(); clip, pid = seed(b1, fake, synth_video); cloud.crash(b1)
    b2 = cloud.session(); eng = b2.engine
    cloud.drive.fail_next_uploads = 1                                     # o primeiro registro do diário falha
    eng.run_one()
    p = eng.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "retry_wait" and "diário" in p["error"] and fake.n["tt_upload"] == 0    # parou ANTES do envio irreversível
    _drain(eng)
    assert eng.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"
    assert fake.n["tt_init"] == 1 and fake.n["tt_upload"] == 1


def test_upload_iniciado_antes_da_queda_nao_reenvia_arquivo(cloud, fake, synth_video):
    fake.flags["tt_status_seq"] = ["PROCESSING_UPLOAD", "PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]
    b1 = cloud.session(); clip, pid = seed(b1, fake, synth_video)
    eng = b1.engine; eng.run_one()                                        # init + upload + poll
    st = json.loads(eng.db.one("SELECT state FROM publications WHERE id=?", (pid,))["state"])
    assert st["upload_started"] and fake.n["tt_upload"] == 1
    # simula banco que "esqueceu" a etapa (snapshot antigo): volta para 'upload' mantendo upload_started
    eng.db.x("UPDATE publications SET state=? WHERE id=?", (json.dumps({**st, "stage": "upload"}), pid))
    eng.db.x("UPDATE jobs SET run_at=0 WHERE status='pending'")
    _drain(eng)
    assert fake.n["tt_upload"] == 1 and fake.n["tt_init"] == 1            # não reenviou o vídeo
    assert eng.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"


# ======================================================================= erros temporários x permanentes
def test_erro_permanente_vai_para_admin_sem_retentativas(cloud, fake, synth_video):
    b = cloud.session(); clip, pid = seed(b, fake, synth_video)
    fake.flags["tt_error_code"] = "spam_risk_too_many_posts"
    _drain(b.engine, 6)
    p = b.engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "failed" and p["needs_admin"] == 1 and fake.n["tt_init"] == 1


def test_erro_temporario_tem_limite_de_tentativas_e_depois_exige_admin(cloud, fake, synth_video):
    b = cloud.session(); clip, pid = seed(b, fake, synth_video)
    fake.flags["tt_error_code"] = "rate_limit_exceeded"
    _drain(b.engine, 40)
    p = b.engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "failed" and p["needs_admin"] == 1
    j = b.engine.db.one("SELECT * FROM jobs WHERE dedupe_key=?", (f"pub:{pid}",))
    assert j["status"] == "dead" and j["attempts"] == j["max_attempts"] and fake.n["tt_init"] == j["max_attempts"]   # limite respeitado, não infinito
    assert Path(b.engine.db.one("SELECT path FROM clips WHERE id=?", (clip["id"],))["path"]).is_file()   # clipe preservado


# ======================================================================= reidratação: bordas
def test_download_falha_agora_publicacao_espera_e_depois_publica(cloud, fake, synth_video, monkeypatch):
    fake.flags["tt_status_seq"] = ["PUBLISH_COMPLETE"]
    b1 = cloud.session(); clip, pid = seed(b1, fake, synth_video); cloud.crash(b1)
    b2 = cloud.session(); eng = b2.engine
    Path(eng.db.one("SELECT path FROM clips WHERE id=?", (clip["id"],))["path"]).unlink()     # disco perdeu o clipe
    real, n = cloud.drive.download, {"n": 0}

    def flaky(*a, **k):
        n["n"] += 1
        if n["n"] == 1:
            raise TransientError("rede")
        return real(*a, **k)
    monkeypatch.setattr(cloud.drive, "download", flaky)
    eng.run_one()
    p = eng.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "retry_wait" and fake.n["tt_init"] == 0          # temporário: aguarda, NÃO marca como falha permanente
    _drain(eng)
    assert eng.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"


def test_copia_do_drive_corrompida_nao_publica_e_nao_vira_arquivo_local(cloud, fake, synth_video):
    b1 = cloud.session(); clip, pid = seed(b1, fake, synth_video)
    dp = b1.engine.db.one("SELECT drive_path FROM clips WHERE id=?", (clip["id"],))["drive_path"]
    cloud.crash(b1)
    (cloud.drive.root / dp).write_bytes(b"corrompido")
    b2 = cloud.session(); db2 = b2.engine.db
    c2 = db2.one("SELECT * FROM clips WHERE id=?", (clip["id"],))
    assert "hash" in c2["error"] or "confere" in c2["error"]
    _drain(b2.engine, 4)
    p = db2.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "failed" and p["needs_admin"] == 1 and fake.n["tt_init"] == 0
    assert not list((b2.s.data_dir / "clips").rglob("*.mp4*"))            # nada de arquivo ruim no disco


def test_clipe_ausente_no_drive_e_clipe_nunca_enviado_sao_reportados(cloud, fake, synth_video):
    b1 = cloud.session(); clip, pid = seed(b1, fake, synth_video)
    dp = b1.engine.db.one("SELECT drive_path FROM clips WHERE id=?", (clip["id"],))["drive_path"]
    cloud.crash(b1); (cloud.drive.root / dp).unlink()
    b2 = cloud.session(); db2 = b2.engine.db
    assert "não existe" in db2.one("SELECT error FROM clips WHERE id=?", (clip["id"],))["error"]
    db2.x("UPDATE clips SET drive_path=NULL WHERE id=?", (clip["id"],))
    c = db2.one("SELECT * FROM clips WHERE id=?", (clip["id"],))
    assert b2.rehydrate_clip(c) == "no_drive_copy" and "sem cópia" in db2.one("SELECT error FROM clips WHERE id=?", (clip["id"],))["error"]


def test_snapshot_antigo_sem_coluna_drive_path_e_migrado(tmp_path):
    from xhakinha.db import Store
    s = Settings(secret_key=SECRET, data_dir=tmp_path / "d", env="test", cookie_secure=False)
    s.ensure_dirs()
    st = Store(s)
    st.conn.execute("ALTER TABLE clips DROP COLUMN drive_path"); st.conn.execute("ALTER TABLE contents DROP COLUMN drive_path"); st.conn.close()
    st2 = Store(Settings(secret_key=SECRET, data_dir=tmp_path / "d", env="test", cookie_secure=False))
    assert "drive_path" in {r["name"] for r in st2.q("PRAGMA table_info(clips)")} and "drive_path" in {r["name"] for r in st2.q("PRAGMA table_info(contents)")}


# ======================================================================= armazenamento / ingestão
def test_ingestao_pausa_com_drive_no_limite_mas_publicacao_segue_e_retoma_com_histerese(tmp_path, fake, synth_video, monkeypatch):
    fake.flags["tt_status_seq"] = ["PUBLISH_COMPLETE"]
    cloud = Cloud(tmp_path, fake, capacity=4_000_000_000)                 # 4 GB livres: ok
    b = cloud.session(); eng = b.engine
    monkeypatch.setattr("xhakinha.engine.psutil.cpu_percent", lambda interval=None: 10)
    monkeypatch.setattr(
        "xhakinha.engine.psutil.virtual_memory",
        lambda: type("Mem", (), {"available": 80, "total": 100})(),
    )
    _tt_setup(eng, fake, audited=1)
    clip = _ready_clip(eng, synth_video); pid = _tt_pub(eng, clip); b.sync_files()
    assert b.check_storage()["free_gb"] > 3 and not eng.db.get("ingest_paused")
    cloud.drive.capacity = 1_000_000_000                                  # só ~1 GB livre (< 1,5 GB de segurança)
    b.check_storage()
    assert eng.db.get("ingest_paused") == 1 and str(eng.db.get("ingest_paused_reason")).startswith("drive:")
    # 1) aquisição adiada, sem baixar nada e sem apagar
    cid = make_content(eng, synth_video, status="queued"); eng.db.x("UPDATE contents SET local_path=NULL WHERE id=?", (cid,))
    monkeypatch.setattr("xhakinha.pipeline.make_connector", lambda *a, **k: pytest.fail("não podia baixar com ingestão pausada"))
    with pytest.raises(NotReadyYet):
        eng.pl.h_acquire({"payload": {"content_id": cid}})
    # 2) monitor/arquivo morto não são agendados
    eng.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created,interval_s) VALUES('youtube','Canal','UC1',1,1,0,0)")
    eng.db.x("DELETE FROM jobs"); eng.tick()
    assert not eng.db.q("SELECT 1 FROM jobs WHERE type IN ('monitor','archive_scan','acquire')")
    # 3) publicação de clipe já disponível continua
    eng.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}")
    _drain(eng)
    assert eng.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"
    # 4) histerese: 2 GB livres ainda < 2,5 GB => continua pausada; 4 GB => retoma
    cloud.drive.capacity = 2_000_000_000; b.check_storage(); assert eng.db.get("ingest_paused") == 1
    cloud.drive.capacity = 4_000_000_000; b.check_storage(); assert not eng.db.get("ingest_paused")
    eng.db.x("DELETE FROM jobs"); eng.tick()
    assert eng.db.q("SELECT 1 FROM jobs WHERE type='monitor'")
    # 5) pausa MANUAL do administrador não é desfeita automaticamente
    eng.db.set("ingest_paused", 1); eng.db.set("ingest_paused_reason", "manual"); b.check_storage()
    assert eng.db.get("ingest_paused") == 1


def test_drive_cheio_no_upload_pausa_ingestao_sem_apagar_nem_travar_publicacao(tmp_path, fake, synth_video):
    fake.flags["tt_status_seq"] = ["PUBLISH_COMPLETE"]
    cloud = Cloud(tmp_path, fake, capacity=None)
    b = cloud.session(); eng = b.engine
    _tt_setup(eng, fake, audited=1)
    clip = _ready_clip(eng, synth_video); pid = _tt_pub(eng, clip)
    orig_size = eng.db.one("SELECT size FROM contents c JOIN clips k ON k.content_id=c.id WHERE k.id=?", (clip["id"],)) if False else None
    cloud.drive.capacity = Path(eng.db.one("SELECT local_path p FROM contents LIMIT 1")["p"]).stat().st_size + 1000   # cabe o original, não o clipe
    assert b.sync_files() == 1
    assert eng.db.get("ingest_paused") == 1 and "cheio" in eng.db.get("ingest_paused_reason")
    assert b.sync_files() == 0                                            # recuo de 15 min: não martela o Drive
    assert Path(clip["path"]).is_file() and Path(eng.db.one("SELECT local_path p FROM contents LIMIT 1")["p"]).is_file()
    _drain(eng)
    assert eng.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"


# ======================================================================= segredos e retomada de jobs
def test_tokens_nao_aparecem_em_snapshot_diario_nem_eventos(cloud, fake, synth_video, tmp_path):
    fake.flags["tt_status_seq"] = ["PUBLISH_COMPLETE"]
    b1 = cloud.session(); clip, pid = seed(b1, fake, synth_video); _drain(b1.engine); b1.snapshot()
    sm = SnapshotManager(cloud.drive, tmp_path / "t"); out = tmp_path / "restored.db"
    sm.restore_latest(out)
    assert TOKEN.encode() not in out.read_bytes()                         # criptografado no banco
    for f in cloud.drive.root.rglob("*"):
        if f.is_file() and "originals" not in f.parts and "clips" not in f.parts:
            raw = f.read_bytes()
            if f.suffix == ".gz":
                import gzip; raw = gzip.decompress(raw)
            assert TOKEN.encode() not in raw and b"upload_url" not in raw
    ev = " ".join(e["message"] for e in b1.engine.db.q("SELECT message FROM events"))
    assert TOKEN not in ev
    cloud.crash(b1)
    b2 = cloud.session()                                                  # e ainda é legível pela sessão nova (mesma SECRET_KEY)
    assert b2.engine.db.get_secret("account:tiktok")["access_token"] == TOKEN


def test_job_running_de_sessao_morta_volta_para_a_fila(cloud, fake, synth_video):
    b1 = cloud.session(); clip, pid = seed(b1, fake, synth_video)
    b1.engine.db.x("UPDATE jobs SET status='running', locked_by='w0', locked_at=? WHERE dedupe_key=?", (time.time(), f"pub:{pid}"))
    b1.snapshot(); cloud.crash(b1)
    b2 = cloud.session()
    b2.engine.start(workers=False, scheduler_on=False)                    # mesma rotina que Brain.run chama
    j = b2.engine.db.one("SELECT * FROM jobs WHERE dedupe_key=?", (f"pub:{pid}",))
    assert j["status"] == "pending" and j["error_kind"] == "recovered"
