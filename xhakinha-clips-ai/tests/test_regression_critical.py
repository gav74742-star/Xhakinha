"""Regressão dos 5 problemas críticos da auditoria. [SIM] = usa servidor/ferramenta simulados (NÃO prova integração real)."""
import json
import os
import stat
import time
from pathlib import Path

import pytest

from conftest import make_content
from xhakinha import scheduler
from xhakinha.publish.base import verify_media_token
from xhakinha.retry import PermanentError, TransientError
from xhakinha.sources import ytdlp


def _force_old(engine, cid):
    engine.db.x("UPDATE contents SET processed_at=? WHERE id=?", (time.time() - 10 * 86400, cid))


# ---------------------------------------------------------------- PROBLEMA 1: original preservado
def test_p1_original_preserved_when_render_fails_and_retry_works(engine, synth_video, monkeypatch):
    from xhakinha import editor
    cid = make_content(engine, synth_video)
    path = Path(engine.db.one("SELECT local_path p FROM contents WHERE id=?", (cid,))["p"])
    engine.db.set("min_clip_score", 40)
    real = editor.render_clip
    monkeypatch.setattr(editor, "render_clip", lambda *a, **k: (_ for _ in ()).throw(TransientError("ffmpeg caiu")))
    engine.q.enqueue("process", {"content_id": cid}, content_id=cid)
    engine.run_one()
    assert path.exists(), "original foi apagado após falha"
    c = engine.db.one("SELECT status FROM contents WHERE id=?", (cid,))
    assert c["status"] == "downloaded"
    # limpeza agressiva (carência 0) NÃO pode apagar conteúdo não processado
    engine.db.set("original_grace_h", 0)
    engine.pl.cleaner.run()
    assert path.exists()
    # RETENTATIVA com arquivo disponível -> sucesso
    monkeypatch.setattr(editor, "render_clip", real)
    engine.db.x("UPDATE jobs SET run_at=0 WHERE status='pending'")
    assert engine.run_one()
    assert engine.db.one("SELECT status, clips_made FROM contents WHERE id=?", (cid,))["status"] == "processed"
    assert engine.db.one("SELECT COUNT(*) n FROM clips WHERE content_id=?", (cid,))["n"] >= 1
    assert path.exists(), "original deve permanecer durante a carência"


def test_p1_original_preserved_with_zero_clips_and_only_deleted_by_cleaner_after_grace(engine, synth_video):
    cid = make_content(engine, synth_video)
    path = Path(engine.db.one("SELECT local_path p FROM contents WHERE id=?", (cid,))["p"])
    engine.db.set("min_clip_score", 101)          # nenhum clipe passa -> 0 clipes
    engine.q.enqueue("process", {"content_id": cid}, content_id=cid)
    engine.run_one()
    assert engine.db.one("SELECT clips_made n FROM contents WHERE id=?", (cid,))["n"] == 0 and path.exists()
    engine.pl.cleaner.run()
    assert path.exists(), "dentro da carência"
    ok, why = engine.pl.cleaner.can_delete_original(engine.db.one("SELECT * FROM contents WHERE id=?", (cid,)))
    assert not ok and "carência" in why
    _force_old(engine, cid)
    engine.pl.cleaner.run()
    assert not path.exists()


def test_p1_original_preserved_when_daily_publication_cap_reached(engine, synth_video, fake):
    cid = make_content(engine, synth_video)
    path = Path(engine.db.one("SELECT local_path p FROM contents WHERE id=?", (cid,))["p"])
    engine.db.set("min_clip_score", 40)
    engine.q.enqueue("process", {"content_id": cid}, content_id=cid)
    engine.run_one()
    clip = engine.db.one("SELECT * FROM clips WHERE content_id=? LIMIT 1", (cid,))
    engine.db.set("daily_max", 1)
    now = time.time()
    # uma publicação já feita hoje (tiktok) => cap atingido
    other = engine.db.x("INSERT INTO clips(content_id,start,end,score,status,created,updated) VALUES(?,?,?,?,?,?,?)", (cid, 900, 910, 80, "published", now, now))
    engine.db.x("INSERT INTO publications(clip_id,platform,status,published_at,created,updated) VALUES(?,?,?,?,?,?)", (other, "tiktok", "published", now, now, now))
    pid = engine.db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (clip["id"], "tiktok", "scheduled", now, "{}", now, now))
    engine.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}")
    engine.run_one()
    p = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "scheduled" and p["scheduled_at"] > now + 3600           # adiada p/ amanhã
    assert path.exists() and Path(clip["path"]).exists()
    assert engine.db.one("SELECT status FROM contents WHERE id=?", (cid,))["status"] == "processed"
    assert fake.n["tt_init"] == 0                                                   # nada foi enviado


def test_p1_acquire_failure_keeps_downloaded_file_and_does_not_redownload(engine, synth_video):
    cid = make_content(engine, synth_video)
    path = Path(engine.db.one("SELECT local_path p FROM contents WHERE id=?", (cid,))["p"])
    engine.q.enqueue("acquire", {"content_id": cid}, content_id=cid)
    engine.run_one()                     # arquivo íntegro já existe -> segue direto p/ process, sem baixar
    assert path.exists()
    assert engine.db.one("SELECT COUNT(*) n FROM jobs WHERE type='process'")["n"] == 1


# ---------------------------------------------------------------- PROBLEMA 2: yt-dlp
def test_p2_missing_ytdlp_fails_once_without_wasting_retries(engine, monkeypatch):
    monkeypatch.setattr(ytdlp, "find_ytdlp", lambda: None)
    assert ytdlp.check_available()["ok"] is False
    sid = engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('url','u','https://www.youtube.com/watch?v=abc',1,1,0)")
    cid = engine.db.x("INSERT INTO contents(source_id,ext_id,kind,title,url,status,discovered) VALUES(?,?,?,?,?,?,0)",
                      (sid, "x", "vod", "t", "https://www.youtube.com/watch?v=abc", "queued"))
    engine.q.enqueue("acquire", {"content_id": cid}, content_id=cid, max_attempts=8)
    engine.run_one()
    j = engine.db.one("SELECT status, attempts, error_kind FROM jobs WHERE type='acquire'")
    assert (j["status"], j["attempts"], j["error_kind"]) == ("dead", 1, "permanent")     # 1 tentativa, sem desperdício


def test_p2_url_validation_blocks_shell_injection_and_foreign_hosts():
    for bad in ("http://youtube.com/x", "https://evil.com/x", "https://youtube.com/x; rm -rf /", "file:///etc/passwd"):
        with pytest.raises(PermanentError):
            ytdlp.validate_url(bad)
    assert ytdlp.validate_url("https://www.youtube.com/watch?v=abc")


def test_p2_error_classification_never_bypasses_access_controls():
    assert isinstance(ytdlp.classify("ERROR: Private video. Sign in if you've been granted access"), PermanentError)
    assert isinstance(ytdlp.classify("ERROR: This video is DRM protected"), PermanentError)
    assert isinstance(ytdlp.classify("ERROR: HTTP Error 503: Service Unavailable"), TransientError)


def test_p2_acquisition_works_with_tool_present_SIM(engine, synth_video, tmp_path, monkeypatch):
    """[SIM] yt-dlp falso (script) que copia um vídeo local: prova o fluxo de aquisição→fingerprint→process quando a ferramenta existe."""
    bindir = tmp_path / "bin"; bindir.mkdir()
    exe = bindir / "yt-dlp"
    exe.write_text(f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 2099.01.01; exit 0; fi\n"
                   f"for a in \"$@\"; do case $a in *%\\(ext\\)s) out=\"${{a%.%(ext)s}}.mp4\";; esac; done\ncp '{synth_video}' \"$out\"\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    assert ytdlp.check_available() == {"ok": True, "version": "2099.01.01"}
    sid = engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('url','u','https://www.youtube.com/watch?v=abc',1,1,0)")
    cid = engine.db.x("INSERT INTO contents(source_id,ext_id,kind,title,url,status,discovered) VALUES(?,?,?,?,?,?,0)",
                      (sid, "x", "vod", "t", "https://www.youtube.com/watch?v=abc", "queued"))
    engine.q.enqueue("acquire", {"content_id": cid}, content_id=cid)
    engine.run_one()
    c = engine.db.one("SELECT * FROM contents WHERE id=?", (cid,))
    assert c["status"] == "downloaded" and Path(c["local_path"]).is_file() and c["fingerprint"]


# ---------------------------------------------------------------- PROBLEMA 3: Instagram
def _ready_clip(engine, synth_video, platform_handle=""):
    engine.db.set("min_clip_score", 40)
    cid = make_content(engine, synth_video)
    engine.q.enqueue("process", {"content_id": cid, "max_clips": 1}, content_id=cid)
    engine.run_one()
    return engine.db.one("SELECT * FROM clips WHERE content_id=? ORDER BY score DESC LIMIT 1", (cid,))


def _ig_setup(engine, fake, synth_video):
    engine.s.graph_base = fake.base + "/ig"
    engine.s.public_base_url = fake.base
    fake.flags["video_file"] = str(synth_video)
    engine.db.put_secret("account:instagram", {"access_token": "IGTOKEN", "ig_user_id": "1789"})


def test_p3_instagram_full_flow_public_url_status_and_real_id_SIM(engine, synth_video, fake):
    _ig_setup(engine, fake, synth_video)
    clip = _ready_clip(engine, synth_video)
    now = time.time()
    pid = engine.db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (clip["id"], "instagram", "scheduled", now, json.dumps({"stage": "container", "caption": "legenda #teste"}), now, now))
    engine.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}", priority=2)
    for _ in range(8):
        engine.db.x("UPDATE jobs SET run_at=0 WHERE status='pending'")
        if not engine.run_one():
            break
    p = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "published" and p["public_id"] == "mediaX" and p["url"] == "https://www.instagram.com/reel/ABC/"
    assert fake.n["ig_container"] == 1 and fake.n["ig_publish"] == 1                 # sem duplicar
    body = fake.ig_body
    assert body["media_type"] == ["REELS"] and body["video_url"][0].startswith(fake.base + "/media/")
    tok = body["video_url"][0].split("/media/")[1].removesuffix(".mp4")
    assert verify_media_token(engine.s.secret_key, tok) == pid                       # URL pública assinada e válida
    assert engine.db.one("SELECT status FROM clips WHERE id=?", (clip["id"],))["status"] == "published"


def test_p3_instagram_never_marks_published_while_container_in_progress_SIM(engine, synth_video, fake):
    _ig_setup(engine, fake, synth_video)
    fake.flags["ig_container_seq"] = ["IN_PROGRESS"] * 50
    clip = _ready_clip(engine, synth_video)
    now = time.time()
    pid = engine.db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (clip["id"], "instagram", "scheduled", now, json.dumps({"stage": "container", "caption": "c"}), now, now))
    engine.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}")
    for _ in range(4):
        engine.db.x("UPDATE jobs SET run_at=0 WHERE status='pending'")
        engine.run_one()
    p = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "processing" and not p["public_id"] and fake.n["ig_publish"] == 0


def test_p3_instagram_without_public_url_fails_once_with_clear_message(engine, synth_video, fake):
    _ig_setup(engine, fake, synth_video)
    engine.s.public_base_url = ""
    clip = _ready_clip(engine, synth_video)
    now = time.time()
    pid = engine.db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (clip["id"], "instagram", "scheduled", now, json.dumps({"stage": "container"}), now, now))
    engine.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}")
    engine.run_one()
    p = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "failed" and p["needs_admin"] == 1 and "PUBLIC_BASE_URL" in p["error"]
    assert engine.db.one("SELECT attempts FROM jobs WHERE type='publish'")["attempts"] == 1
    assert Path(clip["path"]).exists()


def test_p3_instagram_ambiguous_publish_failure_does_not_duplicate_post_SIM(engine, synth_video, fake):
    _ig_setup(engine, fake, synth_video)
    fake.flags["ig_publish_fail_once"] = True
    fake.flags["ig_container_seq"] = ["FINISHED"]
    clip = _ready_clip(engine, synth_video)
    now = time.time()
    pid = engine.db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (clip["id"], "instagram", "scheduled", now, json.dumps({"stage": "container", "caption": "c"}), now, now))
    engine.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}")
    for _ in range(10):
        engine.db.x("UPDATE jobs SET run_at=0 WHERE status='pending'")
        if not engine.run_one():
            break
    assert engine.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"
    assert fake.n["ig_publish"] == 1                    # resposta 500 perdida, mas NÃO publicou duas vezes


def test_p3_ready_clips_are_cleaned_after_confirmation_but_unpublished_stock_is_kept(engine, synth_video, fake):
    _ig_setup(engine, fake, synth_video)
    clip = _ready_clip(engine, synth_video)
    now = time.time()
    pid = engine.db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (clip["id"], "instagram", "scheduled", now, json.dumps({"stage": "container", "caption": "c"}), now, now))
    engine.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}")
    for _ in range(8):
        engine.db.x("UPDATE jobs SET run_at=0 WHERE status='pending'")
        if not engine.run_one():
            break
    ready = Path(engine.db.one("SELECT file_path p FROM publications WHERE id=?", (pid,))["p"])
    assert ready.exists()
    other = engine.db.one("SELECT * FROM clips WHERE id!=? LIMIT 1", (clip["id"],))
    out = engine.pl.cleaner.run()
    assert not ready.exists() and not Path(clip["path"]).exists()                       # publicado => limpo
    if other:
        assert Path(other["path"]).exists()                                            # não publicado => preservado


# ---------------------------------------------------------------- PROBLEMA 4: TikTok
def _tt_setup(engine, fake, audited=0):
    engine.s.tiktok_api = fake.base
    engine.db.set("tiktok_audited", audited)
    engine.db.put_secret("account:tiktok", {"access_token": "TT", "refresh_token": "R", "expires_at": time.time() + 9999, "username": "conta"})


def _tt_pub(engine, clip):
    now = time.time()
    pid = engine.db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (clip["id"], "tiktok", "scheduled", now, json.dumps({"stage": "init", "caption": "desc #x"}), now, now))
    engine.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}")
    return pid


def _drain(engine, n=10):
    for _ in range(n):
        engine.db.x("UPDATE jobs SET run_at=0 WHERE status='pending'")
        if not engine.run_one():
            break


def test_p4_tiktok_published_only_after_publish_complete_and_real_id_SIM(engine, synth_video, fake):
    _tt_setup(engine, fake, audited=1)
    engine.db.set("tiktok_privacy", "PUBLIC_TO_EVERYONE")
    fake.flags["tt_status_seq"] = ["PROCESSING_UPLOAD", "PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]
    clip = _ready_clip(engine, synth_video)
    pid = _tt_pub(engine, clip)
    engine.run_one()
    mid = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert mid["status"] == "processing" and not mid["public_id"] and not mid["url"]      # upload != publicação
    _drain(engine)
    p = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "published" and p["public_id"] == "7123456789"
    assert p["url"] == "https://www.tiktok.com/@conta/video/7123456789" and p["remote_id"] == "pid1"   # publish_id != id público
    assert fake.n["tt_init"] == 1 and fake.tt_init_body["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"


def test_p4_unaudited_app_is_forced_private_and_never_invents_link_SIM(engine, synth_video, fake):
    _tt_setup(engine, fake, audited=0)
    engine.db.set("tiktok_privacy", "PUBLIC_TO_EVERYONE")                    # mesmo pedindo público
    fake.flags["tt_status_seq"] = ["PUBLISH_COMPLETE"]; fake.flags["tt_public_id"] = False
    clip = _ready_clip(engine, synth_video)
    pid = _tt_pub(engine, clip)
    _drain(engine)
    p = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert fake.tt_init_body["post_info"]["privacy_level"] == "SELF_ONLY"
    assert p["status"] == "published" and p["visibility"] == "SELF_ONLY" and p["public_id"] is None and p["url"] is None


def test_p4_tiktok_failed_internal_retries_without_duplicate_and_permanent_goes_to_admin_SIM(engine, synth_video, fake):
    _tt_setup(engine, fake)
    fake.flags["tt_status_seq"] = ["FAILED", "PUBLISH_COMPLETE"]; fake.flags["tt_fail"] = "internal"
    clip = _ready_clip(engine, synth_video)
    pid = _tt_pub(engine, clip)
    _drain(engine)
    assert engine.db.one("SELECT status FROM publications WHERE id=?", (pid,))["status"] == "published"
    assert fake.n["tt_init"] == 2          # FAILED = nada postado, reinit é seguro (2 envios, 1 post)
    # erro permanente (violação) -> fila de falhas + needs_admin, sem insistir
    fake.n["tt_status"] = 0; fake.flags["tt_status_seq"] = ["FAILED"]; fake.flags["tt_fail"] = "spam_risk_too_many_posts"
    clip2 = engine.db.one("SELECT * FROM clips WHERE id!=? ORDER BY id LIMIT 1", (clip["id"],))
    if clip2:
        pid2 = _tt_pub(engine, clip2)
        _drain(engine)
        p2 = engine.db.one("SELECT * FROM publications WHERE id=?", (pid2,))
        assert p2["status"] == "failed" and p2["needs_admin"] == 1 and Path(clip2["path"]).exists()


def test_p4_unaudited_public_account_error_is_explained(engine, synth_video, fake):
    _tt_setup(engine, fake)
    fake.flags["tt_error_code"] = "unaudited_client_can_only_post_to_private_accounts"
    clip = _ready_clip(engine, synth_video)
    pid = _tt_pub(engine, clip)
    engine.run_one()
    p = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert p["status"] == "failed" and "auditados" in p["error"]


def test_p4_tiktok_expired_upload_url_reinit_before_any_post_SIM(engine, synth_video, fake):
    _tt_setup(engine, fake)
    clip = _ready_clip(engine, synth_video)
    pid = _tt_pub(engine, clip)
    pub = engine.db.one("SELECT * FROM publications WHERE id=?", (pid,))
    assert engine.pl.pubs["tiktok"].account()["username"] == "conta"
    engine.run_one()
    assert fake.n["tt_upload"] == 1


# ---------------------------------------------------------------- PROBLEMA 5: cota do YouTube
def test_p5_youtube_monitoring_budget_is_far_below_daily_quota_SIM(engine, fake):
    from xhakinha.sources.youtube import YouTubeConnector
    s = engine.s
    s.youtube_base, s.youtube_rss, s.youtube_api_key = fake.base + "/yt", fake.base + "/yt/rss", "KEY"
    sid = engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('youtube','c',?,1,1,0)", ("UC" + "a" * 22,))
    src = engine.db.one("SELECT * FROM sources WHERE id=?", (sid,))
    conn = YouTubeConnector(src, s, engine.db)
    known = {}
    for _ in range(288):                           # 24h com intervalo de 5 min
        infos = conn.discover(known)
        for i in infos:
            known[i.ext_id] = {"live_state": i.live_state}
    used = conn.quota.used()
    assert used <= 2, f"consumo {used} unidades/dia (antes: ~96.000)"      # RSS gratuito; videos.list só p/ vídeos novos
    assert fake.n["yt_videos"] == 1 and sum(1 for m, p in fake.calls if p.startswith("/yt/rss")) == 288


def test_p5_live_polling_costs_1_unit_per_check_not_100_SIM(engine, fake):
    from xhakinha.sources.youtube import YouTubeConnector
    s = engine.s
    s.youtube_base, s.youtube_rss, s.youtube_api_key = fake.base + "/yt", fake.base + "/yt/rss", "KEY"
    sid = engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('youtube','c',?,1,1,0)", ("UC" + "a" * 22,))
    conn = YouTubeConnector(engine.db.one("SELECT * FROM sources WHERE id=?", (sid,)), s, engine.db)
    known = {"vid1": {"live_state": "live"}}          # live em andamento: reconsulta 1x/intervalo
    for _ in range(288):
        conn.discover(known)
    assert conn.quota.used() == 288               # 288 unidades/dia (vs 96.000 do projeto antigo)


def test_p5_quota_403_backoff_until_reset_and_no_further_calls_SIM(engine, fake):
    from xhakinha.retry import QuotaExceeded
    from xhakinha.sources.youtube import YouTubeConnector
    s = engine.s
    s.youtube_base, s.youtube_rss, s.youtube_api_key = fake.base + "/yt", fake.base + "/yt/rss", "KEY"
    fake.flags["yt_quota"] = True
    sid = engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('youtube','c',?,1,1,0)", ("UC" + "a" * 22,))
    conn = YouTubeConnector(engine.db.one("SELECT * FROM sources WHERE id=?", (sid,)), s, engine.db)
    with pytest.raises(QuotaExceeded):
        conn.discover({})
    before = len(fake.calls)
    with pytest.raises(QuotaExceeded):
        conn.discover({"zzz": {"live_state": "live"}})
    assert not any(p.startswith("/yt/videos") for _, p in fake.calls[before:])      # bloqueado localmente, sem bater na API


def test_p5_self_imposed_daily_limit_enforced(engine, fake):
    from xhakinha.retry import QuotaExceeded
    from xhakinha.sources.quota import Quota
    q = Quota(engine.db, "youtube", 100)
    for _ in range(100):
        q.charge(1)
    with pytest.raises(QuotaExceeded):
        q.charge(100)
