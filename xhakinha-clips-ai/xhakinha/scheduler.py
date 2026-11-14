"""Metas diárias (fuso configurável), horários inteligentes por plataforma e organização do estoque."""
from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timedelta

from . import captions, learning
from .db import Store

IN_FLIGHT = ("uploading", "processing", "awaiting_confirmation")
PLATFORM_MAX_S = {"instagram": 180, "tiktok": 600}


def day_bounds(tz, now: float | None = None) -> tuple[float, float]:
    """[00:00, 24:00) no fuso configurado — correto em mudanças de data/horário de verão."""
    d = datetime.fromtimestamp(now or time.time(), tz)
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    nxt = start + timedelta(days=1)
    return start.timestamp(), datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).timestamp()


def day_key(tz, now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), tz).strftime("%Y-%m-%d")


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def counts_today(db: Store, platform: str, tz, now: float | None = None) -> dict:
    a, b = day_bounds(tz, now)
    r = lambda sql, *args: db.one(sql, args)["n"]
    return {
        "published": r("SELECT COUNT(*) n FROM publications WHERE platform=? AND status='published' AND published_at>=? AND published_at<?", platform, a, b),
        "in_flight": r("SELECT COUNT(*) n FROM publications WHERE platform=? AND status IN ('uploading','processing','awaiting_confirmation')", platform),
        "scheduled": r("SELECT COUNT(*) n FROM publications WHERE platform=? AND status IN ('scheduled','retry_wait') AND scheduled_at>=? AND scheduled_at<?", platform, a, b),
        "produced": db.one("SELECT COUNT(*) n FROM clips WHERE created>=? AND created<?", (a, b))["n"],
    }


def cap_reached(db: Store, platform: str, tz, now: float | None = None, exclude_pub: int | None = None) -> bool:
    mx = int(db.get("daily_max"))
    a, b = day_bounds(tz, now)
    n = db.one("SELECT COUNT(*) n FROM publications WHERE platform=? AND id IS NOT ? AND ((status='published' AND published_at>=? AND published_at<?) "
               "OR status IN ('uploading','processing','awaiting_confirmation'))", (platform, exclude_pub, a, b))["n"]
    return n >= mx


def eligible_clips(db: Store, platform: str) -> list[dict]:
    minsc = float(db.get("min_publish_score"))
    return db.q("""SELECT c.*, ct.title source_title, ct.kind ckind, s.name source_name FROM clips c
                   JOIN contents ct ON ct.id=c.content_id LEFT JOIN sources s ON s.id=ct.source_id
                   WHERE c.status IN ('awaiting','scheduled') AND c.path IS NOT NULL AND c.score>=?
                   AND c.duration<=? AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.clip_id=c.id AND p.platform=?)""",
                (minsc, PLATFORM_MAX_S.get(platform, 90), platform))


def target_for_today(db: Store, platform: str, tz, now: float | None = None) -> dict:
    mn, nm, mx = int(db.get("daily_min")), int(db.get("daily_normal")), int(db.get("daily_max"))
    why = []
    t = nm
    rows = [r for r in learning.latest_metrics(db) if r["platform"] == platform]
    if len(rows) >= learning.MIN_SAMPLES:
        cut = (now or time.time()) - 7 * 86400
        rec = [learning.engagement(r) for r in rows if (r["published_at"] or 0) >= cut]
        old = [learning.engagement(r) for r in rows if (r["published_at"] or 0) < cut]
        if rec and old and sum(old):
            f = max(0.8, min(1.25, (sum(rec) / len(rec)) / (sum(old) / len(old))))
            t = round(nm * f)
            why.append(f"desempenho recente x{f:.2f}")
    elig = len(eligible_clips(db, platform))
    c = counts_today(db, platform, tz, now)
    already = c["published"] + c["in_flight"] + c["scheduled"]
    t = max(mn if elig + already >= mn else 0, min(t, mx))
    if elig + already < t:
        t = elig + already
        why.append("limitado pelo estoque de clipes bons (qualidade > quantidade)")
    return {"min": mn, "normal": nm, "max": mx, "target": max(0, min(t, mx)), "reasons": why, **c}


def refresh_clip_status(db: Store, clip_id: int) -> str:
    cl = db.one("SELECT status FROM clips WHERE id=?", (clip_id,))
    if not cl or cl["status"] in ("processing", "archived"):
        return cl["status"] if cl else ""
    st = [p["status"] for p in db.q("SELECT status FROM publications WHERE clip_id=?", (clip_id,))]
    if not st:
        new = "awaiting"
    elif all(s == "published" for s in st):
        new = "published"
    elif any(s == "failed" for s in st):
        new = "publish_failed"
    elif any(s == "retry_wait" for s in st):
        new = "retry_wait"
    elif any(s in ("scheduled", *IN_FLIGHT) for s in st):
        new = "scheduled"
    elif all(s in ("cancelled", "published") for s in st):
        new = "awaiting"
    else:
        new = "awaiting"
    db.x("UPDATE clips SET status=?, updated=? WHERE id=?", (new, time.time(), clip_id))
    return new


def _pick_slots(db: Store, platform: str, tz, now: float, n: int, taken: list[float], rng: random.Random) -> list[float]:
    wsh, wsm = _hm(db.get("window_start"))
    weh, wem = _hm(db.get("window_end"))
    gap = float(db.get("min_interval_min")) * 60
    d0, _ = day_bounds(tz, now)
    w_start, w_end = max(d0 + wsh * 3600 + wsm * 60, now + 120), d0 + weh * 3600 + wem * 60
    if w_end <= w_start or n <= 0:
        return []
    bh = learning.best_hours(db, platform, tz)["scores"]
    hours = [h for h in range(24) if d0 + (h + 1) * 3600 > w_start and d0 + h * 3600 < w_end]
    slots: list[float] = []

    def ok(t: float) -> bool:
        return all(abs(t - x) >= gap for x in taken + slots) and w_start <= t <= w_end

    explore = 0.2
    attempts = 0
    while len(slots) < n and attempts < 200:
        attempts += 1
        if rng.random() < explore:
            h = rng.choice(hours)                                   # experimentação contínua
        else:
            ws = [max(0.05, bh.get(h, 0.2)) for h in hours]
            h = rng.choices(hours, weights=ws)[0]
        t = d0 + h * 3600 + rng.uniform(0, 3300)
        if ok(t):
            slots.append(t)
    return sorted(slots)


def plan(db: Store, tz, now: float | None = None, rng: random.Random | None = None) -> dict:
    """Cria publicações agendadas até a meta do dia (por plataforma). Idempotente. Nunca estoura o máximo."""
    now = now or time.time()
    rng = rng or random.Random()
    out = {}
    a, b = day_bounds(tz, now)
    for platform in db.get("platforms_enabled"):
        tg = target_for_today(db, platform, tz, now)
        remaining = tg["target"] - (tg["published"] + tg["in_flight"] + tg["scheduled"])
        remaining = min(remaining, tg["max"] - (tg["published"] + tg["in_flight"] + tg["scheduled"]))
        made = 0
        if remaining > 0:
            taken = [r["x"] for r in db.q("SELECT scheduled_at x FROM publications WHERE platform=? AND scheduled_at>=? AND scheduled_at<? "
                                          "AND status IN ('scheduled','retry_wait','uploading','processing','published')", (platform, a - 1, b))]
            taken += [r["x"] for r in db.q("SELECT published_at x FROM publications WHERE platform=? AND published_at>=? AND published_at<?", (platform, a, b))]
            slots = _pick_slots(db, platform, tz, now, remaining, [t for t in taken if t], rng)
            elig = eligible_clips(db, platform)
            weekly = int(db.get("archive_clips_per_week"))
            week_cut = now - 7 * 86400
            arch_recent = db.one("SELECT COUNT(*) n FROM publications p JOIN clips c ON c.id=p.clip_id WHERE c.origin='archive' AND p.created>=? AND p.status!='cancelled'", (week_cut,))["n"]
            arch_today_max = math.ceil(weekly / 7) if weekly else 0
            prev_content, prev_arch, arch_today = None, False, 0
            for t in slots:
                cands = []
                for c in elig:
                    pr = c["score"] * learning.category_multiplier(db, c["category"], platform)
                    pr += min(10.0, (now - c["created"]) / 86400 * 1.5)          # clipes antigos não ficam esquecidos
                    if c["content_id"] == prev_content:
                        pr -= 8                                                # variedade
                    if c["origin"] == "archive":
                        if arch_recent >= weekly or arch_today >= arch_today_max or prev_arch:
                            continue
                    cands.append((pr, c))
                if not cands:
                    break
                cands.sort(key=lambda x: -x[0])
                c = cands[0][1]
                elig.remove(c)
                cap = captions.generate(c["category"], c.get("transcript") or "", c.get("source_name") or "", platform,
                                        credit=bool(db.get("credit_line")))
                st = {"stage": "init" if platform == "tiktok" else "container", "caption": cap["description"], "hashtags": cap["hashtags"],
                      "caption_by": cap["generated_by"]}
                try:
                    db.x("INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                         (c["id"], platform, "scheduled", t, json.dumps(st), now, now))
                except Exception:
                    continue
                refresh_clip_status(db, c["id"])
                prev_content, prev_arch = c["content_id"], c["origin"] == "archive"
                if c["origin"] == "archive":
                    arch_today += 1
                    arch_recent += 1
                made += 1
        out[platform] = {**tg, "new_scheduled": made}
    return out


def daily_cycle(db: Store, tz, now: float | None = None) -> dict:
    """Meia-noite: devolve ao estoque o que ficou agendado de ontem e reprograma o novo dia."""
    now = now or time.time()
    a, _ = day_bounds(tz, now)
    stale = db.q("SELECT id, clip_id FROM publications WHERE status='scheduled' AND scheduled_at<? AND file_path IS NULL AND remote_id IS NULL", (a,))
    for p in stale:
        db.x("DELETE FROM publications WHERE id=?", (p["id"],))
        refresh_clip_status(db, p["clip_id"])
    db.set("last_cycle_day", day_key(tz, now))
    res = plan(db, tz, now)
    db.event("info", "scheduler", f"Novo ciclo diário {day_key(tz, now)}: {len(stale)} agendamentos antigos devolvidos ao estoque", plan=res)
    return {"returned": len(stale), "plan": res}


def due(db: Store, now: float | None = None) -> list[dict]:
    now = now or time.time()
    return db.q("SELECT * FROM publications WHERE status IN ('scheduled','retry_wait') AND COALESCE(next_try, scheduled_at)<=? ORDER BY scheduled_at", (now,))
