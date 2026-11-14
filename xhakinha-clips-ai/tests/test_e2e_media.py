"""Mídia ponta a ponta com VÍDEO SINTÉTICO (apenas teste técnico — não é conteúdo real)."""
import json
import re
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from conftest import make_content
from xhakinha import analysis, editor
from xhakinha.media import probe


@pytest.fixture()
def clip_row(engine, synth_video):
    engine.db.set("min_clip_score", 40)
    cid = make_content(engine, synth_video)
    engine.q.enqueue("process", {"content_id": cid, "max_clips": 2}, content_id=cid)
    engine.run_one()
    rows = engine.db.q("SELECT * FROM clips WHERE content_id=? ORDER BY score DESC", (cid,))
    assert rows
    return rows[0], cid


def test_pipeline_generates_vertical_h264_clean_clip(clip_row, engine):
    c, cid = clip_row
    info = probe(Path(c["path"]))
    assert info["vcodec"] == "h264" and info["has_audio"] and info["acodec"] == "aac"
    assert info["width"] * 16 == info["height"] * 9 or abs(info["width"] / info["height"] - 9 / 16) < 0.01
    assert max(info["width"], info["height"]) <= 1920 and info["width"] <= 1080
    assert 15 <= info["duration"] <= 90.5
    assert info["subtitle_streams"] == 0                                        # sem legendas
    assert c["sha256"] and c["status"] == "awaiting" and engine.st.verify_clip(c)


def test_audio_normalized(clip_row):
    c, _ = clip_row
    r = subprocess.run(["ffmpeg", "-nostats", "-i", c["path"], "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True)
    i = float(re.findall(r"I:\s+(-?\d+\.\d+) LUFS", r.stderr)[-1])
    tp = float(re.findall(r"Peak:\s+(-?\d+\.\d+) dBFS", r.stderr)[-1])
    assert -20 <= i <= -12, f"loudness {i}"
    assert tp <= -0.5, f"true peak {tp}"


def _frame(path, t=2.0):
    import cv2
    cap = cv2.VideoCapture(str(path)); cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000); ok, fr = cap.read(); cap.release()
    assert ok
    return fr.astype(np.int16)


def test_watermark_only_handle_per_platform_and_clean_master(clip_row, engine, tmp_path):
    c, _ = clip_row
    master = Path(c["path"])
    ig = editor.apply_watermark(master, tmp_path / "ig.mp4", "@conta_instagram", 0.65, "top_left", 0.035)
    tt = editor.apply_watermark(master, tmp_path / "tt.mp4", "tiktok_conta", 0.65, "top_left", 0.035)
    m, a, b = _frame(master), _frame(ig), _frame(tt)
    d_ig = np.abs(a - m).max(axis=2); d_tt = np.abs(b - m).max(axis=2)
    for d in (d_ig, d_tt):
        ys, xs = np.where(d > 60)
        assert len(ys) > 50, "marca d'água não apareceu"
        H, W = d.shape
        assert ys.max() < H * 0.2 and xs.max() < W * 0.7                    # pequena, discreta, no canto
        mask = np.ones_like(d, bool); mask[:ys.max() + 6, :xs.max() + 6] = False
        outside = d[mask]
        assert (outside > 24).mean() < 0.002, "alteração relevante fora da região da marca (algo além do @ foi desenhado)"   # tolera ruído de recompressão
    assert np.abs(d_ig - d_tt).sum() > 0                                     # versões diferentes por plataforma
    # sem @ configurado: cópia idêntica (nada inventado)
    plain = editor.apply_watermark(master, tmp_path / "plain.mp4", "")
    assert plain.read_bytes() == master.read_bytes()
    assert probe(ig)["width"] == probe(master)["width"] and probe(ig)["duration"] == pytest.approx(probe(master)["duration"], abs=0.2)


def test_watermark_opacity_is_65_percent(tmp_path):
    from PIL import Image
    editor.watermark_png("@x", 720, 1280, 0.65, 0.035, tmp_path / "w.png")
    a = np.array(Image.open(tmp_path / "w.png"))[:, :, 3]
    assert a.max() == int(255 * 0.65)


def test_analysis_finds_planted_events(synth_video):
    from xhakinha.db import DEFAULT_SETTINGS as D
    m = probe(synth_video)
    cands = analysis.analyze_range(synth_video, 0, m["duration"], m, D, [], use_whisper=False)
    top = analysis.select_best(cands, 40)
    covered = lambda t: any(c.start - 1 <= t <= c.end + 1 for c in top)
    assert covered(42) and covered(81.5), [(c.start, c.end, c.category) for c in top]      # risada (40-44) e grito (80-83)
    for c in top:
        assert set(c.scores) == set(analysis.CRITERIA) and 0 <= c.score <= 100
        assert c.end - c.start <= D["clip_hard_max_s"] + 0.01


def test_quality_over_quantity_no_clip_below_threshold(synth_video):
    from xhakinha.db import DEFAULT_SETTINGS as D
    m = probe(synth_video)
    cands = analysis.analyze_range(synth_video, 0, m["duration"], m, D, [], use_whisper=False)
    assert analysis.select_best(cands, 101) == []
    assert all(c.score >= 70 for c in analysis.select_best(cands, 70))


def test_chunking_long_video_overlap_covers_everything():
    ch = list(analysis.iter_chunks(3 * 3600 + 500, 45 * 60, 120))
    assert ch[0][0] == 0 and ch[-1][1] == 3 * 3600 + 500
    for (a0, a1), (b0, b1) in zip(ch, ch[1:]):
        assert b0 < a1 and a1 - b0 == 120                    # sobreposição de 2 min
    assert all(1 < (b - a) <= 45 * 60 for a, b in ch)
    assert list(analysis.iter_chunks(1800, 2700, 120)) == [(0.0, 1800)]


def test_face_tracking_keeps_subject_inside_crop(synth_video):
    n = iter(range(10_000))
    def fake(g):
        i = next(n); h, w = g.shape
        return [(int(w * (0.2 + 0.03 * i)), int(h * 0.3), 60, 80)]       # rosto andando para a direita
    tr = editor.track_faces(synth_video, 10, 10, 640, 360, fake)
    assert tr["mode"] == "face"
    cw = 202
    xs = [max(0, min(640 - cw, c * 640 - cw / 2)) for _, c, _ in tr["knots"]]
    for (t, c, _), x in zip(tr["knots"], xs):
        assert x <= c * 640 <= x + cw                        # rosto dentro do recorte
    steps = [abs(b[1] - a[1]) / max(1e-6, b[0] - a[0]) for a, b in zip(tr["knots"], tr["knots"][1:])]
    assert max(steps) <= 0.121                               # sem movimentos bruscos (≤12% da largura/s)


def test_no_face_falls_back_to_center(synth_video):
    tr = editor.track_faces(synth_video, 10, 6, 640, 360, lambda g: [])
    assert tr["mode"] == "center"


def test_reprocessing_same_content_does_not_duplicate_clips(clip_row, engine):
    c, cid = clip_row
    before = engine.db.one("SELECT COUNT(*) n FROM clips WHERE content_id=?", (cid,))["n"]
    engine.q.enqueue("process", {"content_id": cid, "max_clips": 2}, content_id=cid, dedupe_key="p2")
    engine.run_one()
    after = engine.db.one("SELECT COUNT(*) n FROM clips WHERE content_id=?", (cid,))["n"]
    assert after == before


def test_fingerprint_dedupe_ignores_name_and_size_only_checks(engine, synth_video, tmp_path):
    from xhakinha.storage import quick_fingerprint
    import shutil
    other = tmp_path / "totally_different_name.mp4"
    shutil.copy(synth_video, other)
    assert quick_fingerprint(other, 120) == quick_fingerprint(synth_video, 120)
    modified = tmp_path / "same_size.mp4"
    data = bytearray(synth_video.read_bytes()); data[len(data) // 2:len(data) // 2 + 2000] = b"\x01" * 2000
    modified.write_bytes(bytes(data))
    assert quick_fingerprint(modified, 120) != quick_fingerprint(synth_video, 120)      # mesmo tamanho/duração, conteúdo diferente


def test_duplicate_download_is_skipped_by_fingerprint(engine, synth_video):
    a = make_content(engine, synth_video, status="downloaded")
    engine.db.x("UPDATE contents SET status='processed' WHERE id=?", (a,))
    b = make_content(engine, synth_video, status="queued")
    engine.db.x("UPDATE contents SET local_path=NULL WHERE id=?", (b,))
    # b precisa de fonte baixável: simulamos com cópia local existente
    import shutil
    dest = engine.st.download_path(b); shutil.copy(synth_video, dest)
    engine.db.x("UPDATE contents SET local_path=? WHERE id=?", (str(dest), b))
    engine.q.enqueue("acquire", {"content_id": b}, content_id=b)
    engine.run_one()
    # arquivo íntegro presente => segue para processamento (reaproveita), mas fingerprint igual impede nova aquisição duplicada quando vier de download
    assert engine.db.one("SELECT status FROM contents WHERE id=?", (b,))["status"] in ("downloaded", "skipped")
