"""Handlers de tarefas dos executores. `process_content` é retomável: cada pedaço analisado e cada clipe
renderizado é gravado no Drive (endereçado pelo hash do conteúdo) ANTES de seguir; outro executor retoma de onde parou."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from .. import analysis, editor
from ..media import probe
from ..retry import PermanentError
from .checkpoint import ChunkStore
from .drive_storage import sha256_file
from .executor import TaskContext


def _cand_from(d: dict) -> analysis.Candidate:
    return analysis.Candidate(**d)


def process_content(ctx: TaskContext) -> dict:
    p = ctx.payload
    key, src_path = p["content_key"], p["drive_path"]
    cfg = p["cfg"]
    drive = ctx.drive
    local = ctx.workdir / f"{key}.mp4"
    if not local.is_file():
        drive.download(src_path, local, sha256=p.get("sha256"))
    ctx.check()
    meta = probe(local)
    store = ChunkStore(drive, key)
    used = [tuple(x) for x in p.get("used", [])]
    chunks = list(analysis.iter_chunks(meta["duration"], cfg["chunk_min"] * 60, cfg["chunk_overlap_s"]))
    cands: list[analysis.Candidate] = []
    reused = 0
    for i, (t0, t1) in enumerate(chunks):
        got = store.load("analysis", t0, t1) if store.has("analysis", t0, t1) else None
        if got is None:
            res = analysis.analyze_range(local, t0, t1, meta, cfg, used, p.get("weights"), p.get("whisper", True),
                                         p.get("whisper_model", "small"))
            got = [asdict(c) for c in res]
            store.save("analysis", t0, t1, got, ctx.executor_id)
        else:
            reused += 1
        cands += [_cand_from(d) for d in got]
        ctx.save_checkpoint({"chunks_done": i + 1, "chunks_total": len(chunks), "reused": reused})
    cands.sort(key=lambda c: -c.score)
    chosen = analysis.select_best(cands, float(cfg["min_clip_score"]))
    mx = p.get("max_clips")
    if mx:
        chosen = sorted(sorted(chosen, key=lambda c: -c.score)[:mx], key=lambda c: c.start)
    clips = []
    for c in chosen:
        ctx.check()
        s, e = round(c.start, 1), round(c.end, 1)
        ck = f"{key[:16]}_{int(s * 10):07d}_{int(e * 10):07d}"
        mp4, side = f"clips/pending/{ck}.mp4", f"clips/pending/{ck}.json"
        info = drive.read_json(side)
        if info is None:                                     # sidecar = marcador de "clipe completo e verificado"
            dest = ctx.workdir / f"{ck}.mp4"
            r = editor.render_clip(local, dest, s, e - s, meta)
            sha = sha256_file(dest)
            if not drive.stat(mp4):
                drive.upload(dest, mp4, expect_sha256=sha)
            info = {"key": ck, "path": mp4, "sha256": sha, "size": r["size"], "duration": r["duration"], "start": s, "end": e,
                    "score": c.score, "scores": c.scores, "category": c.category, "transcript": c.transcript}
            drive.write_json(side, info)
            dest.unlink(missing_ok=True)
        clips.append(info)
        ctx.save_checkpoint({"chunks_done": len(chunks), "chunks_total": len(chunks), "clips_done": len(clips), "clips_total": len(chosen)})
    local.unlink(missing_ok=True)                            # cache local descartável; o original continua no Drive
    return {"content_key": key, "candidates": len(cands), "chunks": len(chunks), "chunks_reused": reused, "clips": clips,
            "executor": ctx.executor_id}


def healthcheck(ctx: TaskContext) -> dict:
    from .healthcheck import local_report
    return local_report(ctx.drive)


def sleep_task(ctx: TaskContext) -> dict:                    # usado nos testes de lease/corrida
    t = float(ctx.payload.get("seconds", 0.1))
    end = time.time() + t
    while time.time() < end:
        ctx.check()
        time.sleep(0.05)
    return {"slept": t}


def default_handlers() -> dict:
    return {"process_content": process_content, "healthcheck": healthcheck, "sleep": sleep_task}
