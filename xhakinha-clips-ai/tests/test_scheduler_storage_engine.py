import json
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import make_content
from xhakinha import learning, scheduler
from xhakinha.analysis import BASE_WEIGHTS, CRITERIA


def mk_clip(engine, score=80, content_id=None, origin="vod", category="risada/piada", status="awaiting", created=None, path=True, dur=30):
    db = engine.db
    now = created or time.time()
    cid = content_id or db.x("INSERT INTO contents(source_id,ext_id,kind,status,discovered) VALUES(NULL,?,?,?,0)", (f"e{random.random()}", "vod", "processed"))
    p = None
    if path:
        p = engine.s.clips_dir / f"c{random.random()}.mp4"; p.write_bytes(b"x" * 100)
    return db.x("INSERT INTO clips(content_id,start,end,score,scores,category,status,path,duration,origin,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, random.random() * 1000, 0, score, json.dumps({k: 50 for k in CRITERIA}), category, status, str(p) if p else None, dur, origin, now, now))


# ------------------------------------------------------------------ fuso/dia
def test_day_bounds_default_timezone_and_dst_day_length(settings):
    a, b = scheduler.day_bounds(ZoneInfo("America/Sao_Paulo"), datetime(2026, 11, 14, 15, tzinfo=ZoneInfo("America/Sao_Paulo")).timestamp())
    assert b - a == 86400 and datetime.fromtimestamp(a, ZoneInfo("America/Sao_Paulo")).hour == 0
    ny = ZoneInfo("America/New_York")                            # dia com mudança de horário (23h)
    a, b = scheduler.day_bounds(ny, datetime(2026, 3, 8, 12, tzinfo=ny).timestamp())
    assert b - a == 23 * 3600
    # 23:59 e 00:00 caem em dias diferentes (e NÃO em UTC)
    sp = ZoneInfo("America/Sao_Paulo")
    k1 = scheduler.day_key(sp, datetime(2026, 11, 14, 23, 59, tzinfo=sp).timestamp()); k2 = scheduler.day_key(sp, datetime(2026, 11, 15, 0, 0, tzinfo=sp).timestamp())
    assert (k1, k2) == ("2026-11-14", "2026-11-15")


# ------------------------------------------------------------------ metas e agenda
def test_plan_respects_max_interval_window_and_quality(engine, settings):
    db, tz = engine.db, settings.tz
    for sc in [90, 85, 80, 75, 70, 65, 62, 61, 40, 30] * 3:          # 30 clipes, 6 deles abaixo da nota mínima
        mk_clip(engine, sc)
    db.set("daily_min", 2); db.set("daily_normal", 4); db.set("daily_max", 5)
    now = datetime(2026, 11, 14, 6, 0, tzinfo=tz).timestamp()
    out = scheduler.plan(db, tz, now, random.Random(1))
    for p in ("instagram", "tiktok"):
        rows = db.q("SELECT p.*, c.score FROM publications p JOIN clips c ON c.id=p.clip_id WHERE p.platform=? ORDER BY scheduled_at", (p,))
        assert 1 <= len(rows) <= 5 and len(rows) <= out[p]["target"] <= 5
        assert all(r["score"] >= 60 for r in rows)                       # nada abaixo da qualidade mínima
        ts = [r["scheduled_at"] for r in rows]
        assert all(b - a >= 45 * 60 - 1 for a, b in zip(ts, ts[1:]))      # intervalo mínimo
        assert all(8 <= datetime.fromtimestamp(t, tz).hour < 24 for t in ts)
    again = scheduler.plan(db, tz, now, random.Random(2))                 # idempotente
    assert db.one("SELECT COUNT(*) n FROM publications WHERE platform='tiktok'")["n"] == len(rows)


def test_no_low_quality_filler_when_stock_is_small(engine, settings):
    db, tz = engine.db, settings.tz
    for sc in (90, 85, 50, 40):
        mk_clip(engine, sc)
    db.set("daily_normal", 10)
    scheduler.plan(db, tz, datetime(2026, 11, 14, 6, tzinfo=tz).timestamp(), random.Random(1))
    n = db.one("SELECT COUNT(*) n FROM publications WHERE platform='tiktok'")["n"]
    assert n == 2                                                           # só os 2 bons; não inventa nem publica ruins p/ bater meta


def test_cap_stops_publishing_but_not_production_and_new_day_resumes(engine, settings, synth_video):
    db, tz = engine.db, settings.tz
    db.set("daily_max", 1); db.set("daily_normal", 1); db.set("daily_min", 1)
    now = time.time()
    c1, c2 = mk_clip(engine, 90), mk_clip(engine, 88)
    db.x("INSERT INTO publications(clip_id,platform,status,published_at,created,updated) VALUES(?,?,?,?,?,?)", (c1, "tiktok", "published", now, now, now))
    assert scheduler.cap_reached(db, "tiktok", tz)
    res = scheduler.plan(db, tz, now)
    assert res["tiktok"]["new_scheduled"] == 0                                # meta máxima: nada novo
    cid = make_content(engine, synth_video); db.set("min_clip_score", 40)
    engine.q.enqueue("process", {"content_id": cid, "max_clips": 1}, content_id=cid); engine.run_one()
    assert db.one("SELECT COUNT(*) n FROM clips WHERE content_id=?", (cid,))["n"] == 1      # produção continuou
    stock = db.one("SELECT COUNT(*) n FROM clips WHERE status='awaiting' AND path IS NOT NULL")["n"]
    assert stock >= 2                                                         # estoque cresce
    assert not scheduler.cap_reached(db, "tiktok", tz, now + 86400)          # novo dia: libera


def test_daily_cycle_returns_stale_schedules_to_stock(engine, settings):
    db, tz = engine.db, settings.tz
    c = mk_clip(engine, 90)
    today0 = datetime.now(tz).replace(hour=0, minute=5, second=0, microsecond=0).timestamp()   # relativo ao relógio real (antes era data fixa)
    yesterday = today0 - 3600
    db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,created,updated) VALUES(?,?,?,?,?,?)", (c, "tiktok", "scheduled", yesterday, yesterday, yesterday))
    scheduler.refresh_clip_status(db, c)
    r = scheduler.daily_cycle(db, tz, today0)
    assert r["returned"] == 1 and db.one("SELECT status FROM clips WHERE id=?", (c,))["status"] in ("awaiting", "scheduled")
    assert db.get("last_cycle_day") == datetime.fromtimestamp(today0, tz).strftime("%Y-%m-%d")


def test_old_clips_not_forgotten_priority_bonus(engine, settings):
    db, tz = engine.db, settings.tz
    old = mk_clip(engine, 70, created=time.time() - 12 * 86400)
    new = mk_clip(engine, 75)
    db.set("daily_normal", 1); db.set("daily_min", 1); db.set("daily_max", 1)
    scheduler.plan(db, tz, datetime(2026, 11, 14, 6, tzinfo=tz).timestamp(), random.Random(3))
    assert db.one("SELECT clip_id c FROM publications WHERE platform='tiktok'")["c"] == old


def test_archive_clips_limited_weekly_and_not_consecutive(engine, settings):
    db, tz = engine.db, settings.tz
    for _ in range(10):
        mk_clip(engine, 85, origin="archive")
    for _ in range(10):
        mk_clip(engine, 80, origin="vod")
    db.set("archive_clips_per_week", 5); db.set("daily_normal", 10); db.set("daily_max", 12); db.set("daily_min", 8)
    scheduler.plan(db, tz, datetime(2026, 11, 14, 6, tzinfo=tz).timestamp(), random.Random(5))
    rows = db.q("SELECT c.origin o FROM publications p JOIN clips c ON c.id=p.clip_id WHERE p.platform='tiktok' ORDER BY scheduled_at")
    arch = [r["o"] == "archive" for r in rows]
    assert 0 < sum(arch) <= 1                                        # ceil(5/7)=1 por dia
    assert not any(a and b for a, b in zip(arch, arch[1:]))


# ------------------------------------------------------------------ aprendizado
def test_learning_inactive_without_real_samples_and_active_with(engine):
    w, info = learning.learned_weights(engine.db)
    assert w == BASE_WEIGHTS and not info["active"] and info["samples"] == 0
    rnd = random.Random(0)
    now = time.time()
    for i in range(30):
        ent = rnd.uniform(30, 95)
        c = engine.db.x("INSERT INTO clips(content_id,start,end,score,scores,category,status,created,updated,duration) VALUES(NULL,?,?,?,?,?,?,?,?,?)",
                        (i, 0, 70, json.dumps({**{k: 50 for k in CRITERIA}, "entretenimento": ent}), "risada/piada", "published", now, now, 30))
        p = engine.db.x("INSERT INTO publications(clip_id,platform,status,published_at,created,updated) VALUES(?,?,?,?,?,?)", (c, "tiktok", "published", now - i * 3600, now, now))
        engine.db.x("INSERT INTO metrics(publication_id,ts,views,likes,comments,shares) VALUES(?,?,?,?,?,?)", (p, now, int(ent * 100), int(ent * 8), int(ent), int(ent * 2)))
    w, info = learning.learned_weights(engine.db)
    assert info["active"] and info["samples"] == 30 and abs(sum(w.values()) - 1) < 1e-6
    assert w["entretenimento"] > BASE_WEIGHTS["entretenimento"]       # aprendeu com dado real


def test_best_hours_prior_flagged_as_assumption_and_data_overrides(engine, settings):
    bh = learning.best_hours(engine.db, "tiktok", settings.tz)
    assert "prior" in bh["source"]
    top_prior = max(bh["scores"], key=bh["scores"].get)
    now = time.time()
    for i in range(40):                                              # dados reais: 09h local rende muito mais
        c = engine.db.x("INSERT INTO clips(content_id,start,end,score,scores,category,status,created,updated) VALUES(NULL,?,?,?,?,?,?,?,?)", (i, 0, 70, "{}", "x", "published", now, now))
        pub = datetime(2026, 11, 1 + i % 20, 9, 30, tzinfo=settings.tz).timestamp()
        p = engine.db.x("INSERT INTO publications(clip_id,platform,status,published_at,created,updated) VALUES(?,?,?,?,?,?)", (c, "tiktok", "published", pub, now, now))
        engine.db.x("INSERT INTO metrics(publication_id,ts,views,likes) VALUES(?,?,?,?)", (p, now, 50000, 3000))
    bh2 = learning.best_hours(engine.db, "tiktok", settings.tz)
    assert bh2["scores"][9] > bh["scores"][9] and "dados reais" in bh2["source"]


# ------------------------------------------------------------------ armazenamento / limpeza
def test_cleaner_never_deletes_unpublished_or_failed_clips(engine):
    ids = [mk_clip(engine, 80, status=s) for s in ("awaiting", "scheduled", "retry_wait", "publish_failed")]
    now = time.time()
    p = engine.db.x("INSERT INTO publications(clip_id,platform,status,created,updated) VALUES(?,?,?,?,?)", (ids[3], "tiktok", "failed", now, now))
    engine.db.set("unpublished_retention_days", 1)
    out = engine.pl.cleaner.run(now + 400 * 86400)
    assert out["clips"] == 0 and all(Path(engine.db.one("SELECT path p FROM clips WHERE id=?", (i,))["p"]).exists() for i in ids)


def test_clip_deleted_only_when_all_platforms_confirmed(engine):
    c = mk_clip(engine, 80)
    now = time.time()
    engine.db.x("INSERT INTO publications(clip_id,platform,status,published_at,created,updated) VALUES(?,?,?,?,?,?)", (c, "tiktok", "published", now, now, now))
    engine.db.x("INSERT INTO publications(clip_id,platform,status,created,updated) VALUES(?,?,?,?,?)", (c, "instagram", "retry_wait", now, now))
    cl = engine.db.one("SELECT * FROM clips WHERE id=?", (c,))
    assert engine.pl.cleaner.can_delete_clip(cl)[0] is False
    engine.db.x("UPDATE publications SET status='published', published_at=? WHERE platform='instagram'", (now,))
    assert engine.pl.cleaner.can_delete_clip(cl)[0] is True


def test_cleaner_refuses_paths_outside_managed_dirs(engine, tmp_path):
    victim = tmp_path / "importante.txt"; victim.write_text("não apague")
    engine.db.x("INSERT INTO contents(source_id,ext_id,kind,status,local_path,processed_at,discovered) VALUES(NULL,'x','vod','processed',?,?,0)", (str(victim), 0))
    engine.db.set("original_grace_h", 0)
    engine.pl.cleaner.run()
    assert victim.exists()


def test_integrity_check_detects_corrupted_clip_and_blocks_publish(engine):
    c = mk_clip(engine, 80)
    p = Path(engine.db.one("SELECT path p FROM clips WHERE id=?", (c,))["p"])
    import hashlib
    engine.db.x("UPDATE clips SET sha256=?, size=? WHERE id=?", (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size, c))
    assert engine.st.verify_all_stock()["corrupted"] == []
    p.write_bytes(b"y" * 100)
    assert engine.st.verify_all_stock()["corrupted"] == [c]


def test_backup_rotation(engine):
    engine.db.set("backup_keep", 2)
    for i in range(4):
        engine.st.backup_db(); time.sleep(1.1)
    assert len(list(engine.s.backup_dir.glob("xhakinha-*.db"))) == 2


def test_disk_pressure_frees_only_safe_files(engine):
    engine.s.min_free_gb = 10 ** 6                              # força "sem espaço"
    c = mk_clip(engine, 80)
    p = engine.s.production_dir / "downloads" / "old.mp4"; p.write_bytes(b"o")
    cid = engine.db.x("INSERT INTO contents(source_id,ext_id,kind,status,local_path,processed_at,discovered) VALUES(NULL,'o','vod','processed',?,?,0)", (str(p), time.time()))
    p2 = engine.s.production_dir / "downloads" / "failed.mp4"; p2.write_bytes(b"f")
    engine.db.x("INSERT INTO contents(source_id,ext_id,kind,status,local_path,discovered) VALUES(NULL,'f','vod','downloaded',?,0)", (str(p2),))
    engine.pl.cleaner.enforce_disk()
    assert not p.exists() and p2.exists()                       # processado liberado; não processado preservado
    assert Path(engine.db.one("SELECT path p FROM clips WHERE id=?", (c,))["p"]).exists()


# ------------------------------------------------------------------ motor / monitor
def test_engine_tick_enqueues_monitor_runs_cycle_and_recovers_jobs(engine, monkeypatch):
    monkeypatch.setattr("xhakinha.engine.psutil.cpu_percent", lambda interval=None: 10)
    monkeypatch.setattr(
        "xhakinha.engine.psutil.virtual_memory",
        lambda: type("Mem", (), {"available": 80, "total": 100})(),
    )
    engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,interval_s,created) VALUES('youtube','c','@c',1,1,300,0)")
    engine.q.enqueue("x", dedupe_key="stuck"); engine.q.claim("old-process")          # processo anterior morreu com job 'running'
    assert engine.q.recover_all_running() == 1
    engine.tick()
    assert engine.db.one("SELECT COUNT(*) n FROM jobs WHERE type='monitor'")["n"] == 1
    assert engine.db.get("last_cycle_day") == scheduler.day_key(engine.s.tz)
    engine.db.set("engine_state", "paused")
    engine.db.x("DELETE FROM jobs"); engine.db.x("UPDATE sources SET last_check=0")
    engine.tick()
    assert engine.db.one("SELECT COUNT(*) n FROM jobs")["n"] == 0                       # pausado: nada novo é agendado


def test_monitor_ingest_live_vod_delay_authorization_and_connector_isolation(engine, fake):
    s = engine.s
    s.youtube_base, s.youtube_rss, s.youtube_api_key = fake.base + "/yt", fake.base + "/yt/rss", "K"
    ok = engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,interval_s,created) VALUES('youtube','ok',?,1,1,300,0)", ("UC" + "a" * 22,))
    bad = engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,interval_s,created) VALUES('twitch','bad','x',1,1,300,0)")   # sem credenciais Twitch
    unauth = engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,interval_s,created) VALUES('youtube','unauth',?,1,0,300,0)", ("UC" + "b" * 22,))
    for sid in (bad, ok, unauth):
        engine.pl.h_monitor({"payload": {"source_id": sid}})              # a falha da Twitch NÃO derruba as demais
    assert "TWITCH_CLIENT_ID" in engine.db.one("SELECT last_error e FROM sources WHERE id=?", (bad,))["e"]
    j = engine.db.one("SELECT * FROM jobs WHERE type='acquire'")
    assert j and 5 * 60 - 1 <= j["run_at"] - time.time() <= 15 * 60 + 5                  # VOD de live: processa 5–15 min depois
    assert engine.db.one("SELECT COUNT(*) n FROM jobs WHERE type='acquire'")["n"] == 1   # fonte sem atestado: nada adquirido
    assert engine.db.one("SELECT status FROM contents WHERE source_id=?", (unauth,))["status"] == "discovered"


def test_archive_planner_gradual_and_dedup(engine):
    db = engine.db
    sid = db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('youtube','c','x',1,1,0)")
    for i in range(6):
        db.x("INSERT INTO contents(source_id,ext_id,kind,title,duration,status,discovered) VALUES(?,?,?,?,?,?,0)", (sid, f"a{i}", "archive", f"v{i}", 1800, "discovered"))
    db.set("archive_clips_per_week", 5)
    engine.plan_archive(time.time())
    assert db.one("SELECT COUNT(*) n FROM contents WHERE status='queued'")["n"] == 1
    engine.plan_archive(time.time())                                                   # já há um em andamento: não empilha
    assert db.one("SELECT COUNT(*) n FROM contents WHERE status='queued'")["n"] == 1
    db.x("UPDATE contents SET status='processed' WHERE status='queued'")
    engine.plan_archive(time.time())                                                   # espaçamento ao longo da semana
    assert db.one("SELECT COUNT(*) n FROM contents WHERE status='queued'")["n"] == 0


# ------------------------------------------------------------------ BOTÃO TESTE REAL (lógica, fontes/ferramentas simuladas)
def test_realtest_button_flow_SIM(engine, fake, synth_video, tmp_path, monkeypatch):
    import os
    from xhakinha.realtest import RealTest
    s = engine.s
    s.youtube_base, s.youtube_rss, s.youtube_api_key, s.tiktok_api = fake.base + "/yt", fake.base + "/yt/rss", "K", fake.base
    fake.flags["tt_status_seq"] = ["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]
    bindir = tmp_path / "bin"; bindir.mkdir(); exe = bindir / "yt-dlp"
    exe.write_text(f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 1; exit 0; fi\nfor a in \"$@\"; do case $a in *%\\(ext\\)s) out=\"${{a%.%(ext)s}}.mp4\";; esac; done\ncp '{synth_video}' \"$out\"\n")
    exe.chmod(0o755); monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    engine.db.set("min_clip_score", 30)
    engine.db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('youtube','Canal Teste',?,1,1,0)", ("UC" + "a" * 22,))
    engine.db.put_secret("account:tiktok", {"access_token": "T", "refresh_token": "R", "expires_at": time.time() + 9999, "username": "u"})
    fake.n["tt_status"] = 0
    rt = RealTest(engine)
    import xhakinha.realtest as R
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    rid = engine.db.x("INSERT INTO realtests(started,status) VALUES(?,?)", (time.time(), "running"))
    rt._run(rid, "tiktok")
    r = rt.latest()
    assert r["status"] == "done", r["steps"]
    names = [x["name"] for x in r["steps"]]
    assert all(x["ok"] for x in r["steps"]), r["steps"]
    res = r["result"]
    assert res["platform"] == "tiktok" and res["send_status"] == "published" and res["visibility"] == "SELF_ONLY"      # modo privado
    assert res["description"] and res["hashtags"] and res["preview"] and res["diagnostic"]["precisa_correcao"] == []
    c = engine.db.one("SELECT * FROM contents LIMIT 1 OFFSET 0")
    assert c["protected"] == 1 and Path(c["local_path"]).exists()                       # original intocado
    assert engine.db.one("SELECT COUNT(*) n FROM clips WHERE origin='realtest'")["n"] == 1   # só 1 clipe


def test_realtest_without_sources_or_accounts_reports_not_tested(engine):
    from xhakinha.realtest import RealTest
    rt = RealTest(engine)
    rid = engine.db.x("INSERT INTO realtests(started,status) VALUES(?,?)", (time.time(), "running"))
    rt._run(rid, None)
    r = rt.latest()
    assert r["status"] == "failed" and r["result"]["diagnostic"]["precisa_correcao"] and "Nenhuma fonte" in r["steps"][-1]["detail"]


def test_monitor_recovers_queued_content_without_acquire_job(engine):
    from xhakinha.sources.base import ContentInfo

    db = engine.db
    sid = db.x(
        "INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES(?,?,?,?,?,?)",
        ("youtube", "recovery-test", "UC" + "a" * 22, 1, 1, 0),
    )
    cid = db.x(
        "INSERT INTO contents(source_id,ext_id,kind,title,url,status,discovered,meta) VALUES(?,?,?,?,?,?,0,?)",
        (sid, "recovery-video", "vod", "Recovery test", "https://www.youtube.com/watch?v=recovery-video", "queued", "{}"),
    )
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    info = ContentInfo(
        ext_id="recovery-video",
        title="Recovery test",
        url="https://www.youtube.com/watch?v=recovery-video",
        kind="vod",
    )
    engine.pl._ingest(src, [info], {})
    jobs = db.one("SELECT COUNT(*) n FROM jobs WHERE type=? AND content_id=?", ("acquire", cid))
    assert jobs["n"] == 1

def test_monitor_recovers_queued_content_with_historical_acquire_jobs(engine):
    from xhakinha.sources.base import ContentInfo

    db = engine.db
    sid = db.x(
        "INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES(?,?,?,?,?,?)",
        ("youtube", "recovery-history-test", "UC" + "b" * 22, 1, 1, 0),
    )
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))

    for previous_status in ("dead", "done", "pending", "running"):
        ext_id = f"recovery-{previous_status}"
        cid = db.x(
            "INSERT INTO contents(source_id,ext_id,kind,title,url,status,discovered,meta) "
            "VALUES(?,?,?,?,?,?,0,?)",
            (sid, ext_id, "vod", "Recovery test",
             f"https://www.youtube.com/watch?v={ext_id}", "queued", "{}"),
        )
        db.x(
            "INSERT INTO jobs(type,payload,status,priority,max_attempts,run_at,"
            "dedupe_key,content_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("acquire", "{}", previous_status, 3, 8, 0,
             f"acquire:{cid}", cid, 0, 0),
        )

        info = ContentInfo(
            ext_id=ext_id,
            title="Recovery test",
            url=f"https://www.youtube.com/watch?v={ext_id}",
            kind="vod",
        )
        engine.pl._ingest(src, [info], {})

        jobs = db.q(
            "SELECT status FROM jobs WHERE type='acquire' AND content_id=? ORDER BY id",
            (cid,),
        )
        statuses = [job["status"] for job in jobs]

        if previous_status in ("dead", "done"):
            assert len(jobs) == 2, (previous_status, statuses)
            assert statuses.count("pending") == 1, (previous_status, statuses)
            assert previous_status in statuses, (previous_status, statuses)
        else:
            assert len(jobs) == 1, (previous_status, statuses)
            assert statuses == [previous_status], (previous_status, statuses)
