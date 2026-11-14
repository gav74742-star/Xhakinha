"""Análise de áudio/vídeo/semântica e pontuação 0-100 de momentos.

HONESTIDADE: laughter/scream/surpresa são ESTIMADORES por processamento de sinal (energia, modulação
3-7 Hz, brilho espectral). A parte semântica só roda se faster-whisper estiver instalado (transcrição
100% interna, nunca vai para o vídeo). Sem ele, o sistema usa apenas sinal de áudio + visão."""
from __future__ import annotations

import os

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .media import probe, run

SR = 16000
HOP = 0.05            # s
FR = int(SR * HOP)    # amostras por hop
CRITERIA = ["entretenimento", "clareza", "intensidade", "contexto", "originalidade", "retencao", "qualidade_av", "independencia"]
BASE_WEIGHTS = {"entretenimento": .26, "clareza": .10, "intensidade": .12, "contexto": .12,
                "originalidade": .08, "retencao": .14, "qualidade_av": .08, "independencia": .10}


def clip01(x):
    return float(np.clip(x, 0.0, 1.0))


# ------------------------------------------------------------------ áudio
def audio_hops(path: Path, start: float, dur: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Retorna (db, zcr, peak) por hop de 50 ms. Streaming: memória baixa."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path), "-vn", "-ac", "1",
           "-ar", str(SR), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    dbs, zs, pk = [], [], []
    buf = b""
    block = FR * 2 * 1200  # 60 s
    while True:
        chunk = p.stdout.read(block)
        if not chunk:
            break
        buf += chunk
        n = (len(buf) // (FR * 2)) * FR * 2
        if n:
            x = np.frombuffer(buf[:n], dtype=np.int16).astype(np.float32) / 32768.0
            buf = buf[n:]
            x = x.reshape(-1, FR)
            pw = (x ** 2).mean(axis=1) + 1e-10
            dbs.append(10 * np.log10(pw))
            zs.append((np.diff(np.signbit(x), axis=1) != 0).mean(axis=1))
            pk.append(np.abs(x).max(axis=1))
    p.stdout.close()
    p.wait()
    if not dbs:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    return np.concatenate(dbs), np.concatenate(zs), np.concatenate(pk)


def _moving(x: np.ndarray, w: int) -> np.ndarray:
    if len(x) == 0:
        return x
    w = max(1, min(w, len(x)))
    k = np.ones(w) / w
    pad = np.pad(x, (w // 2, w - 1 - w // 2), mode="edge")
    return np.convolve(pad, k, mode="valid")


@dataclass
class AudioSignal:
    t0: float                  # início absoluto (s)
    db: np.ndarray             # por hop
    zcr: np.ndarray
    peak: np.ndarray
    base: np.ndarray           # baseline (dB) por hop
    laugh: np.ndarray          # 0..1 por hop
    scream: np.ndarray
    rise: np.ndarray           # dB acima do passado recente
    speech: np.ndarray         # bool por hop
    exc: np.ndarray            # excitação 0..1 suavizada (3 s)
    noise_db: float = -60.0

    def idx(self, t: float) -> int:
        return int(np.clip((t - self.t0) / HOP, 0, max(0, len(self.db) - 1)))


def build_signal(db: np.ndarray, zcr: np.ndarray, peak: np.ndarray, t0: float = 0.0) -> AudioSignal:
    n = len(db)
    if n == 0:
        z = np.zeros(0)
        return AudioSignal(t0, z, z, z, z, z, z, z, z.astype(bool), z)
    base = _moving(db, int(60 / HOP))
    noise = float(np.percentile(db, 15))
    speech = db > (noise + 8)
    # elevação contra passado recente (2-12 s atrás)
    past = np.roll(_moving(db, int(10 / HOP)), int(2 / HOP))
    past[: int(12 / HOP)] = base[: int(12 / HOP)]
    rise = db - past
    env = np.sqrt(10 ** (db / 10))
    laugh = np.zeros(n)
    win, step = int(3 / HOP), int(0.5 / HOP)
    for i in range(0, max(1, n - win), step):
        seg = env[i:i + win]
        if len(seg) < win or seg.mean() < 1e-4 or db[i:i + win].mean() < noise + 6:
            continue
        s = seg - seg.mean()
        den = (s ** 2).sum()
        if den <= 0:
            continue
        ac = np.correlate(s, s, "full")[win - 1:] / den
        score = ac[3:9].max() if ac[3:9].size else 0   # 0.15-0.45 s => 2-6.7 Hz
        mod = clip01((np.std(seg) / (seg.mean() + 1e-9)) * 1.5)        # profundidade de modulação
        v = clip01(max(0.0, score) * 1.2) * mod
        laugh[i:i + step] = v
    zthr = np.percentile(zcr, 70)
    loud = (db > base + 7) & (zcr > zthr)
    sc = _moving(loud.astype(float), int(1.5 / HOP))
    scream = np.clip((sc - 0.5) * 2, 0, 1)
    lv = np.clip((db - base) / 12.0, 0, 1)
    exc = 0.35 * lv + 0.25 * laugh + 0.2 * scream + 0.2 * np.clip(rise / 10, 0, 1)
    exc = _moving(exc, int(3 / HOP))
    exc[~_moving(speech.astype(float), int(2 / HOP)).astype(bool)] *= 0.5
    return AudioSignal(t0, db, zcr, peak, base, laugh, scream, rise, speech, exc, noise)


def pause_points(sig: AudioSignal, min_len: float = 0.4) -> np.ndarray:
    """Tempos absolutos (centro) de pausas >= min_len."""
    quiet = ~sig.speech
    pts, i, n = [], 0, len(quiet)
    m = int(min_len / HOP)
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            if j - i >= m:
                pts.append(sig.t0 + (i + j) / 2 * HOP)
            i = j
        else:
            i += 1
    return np.array(pts)


# ------------------------------------------------------------------ visão
_cascade = None


def _face_detector():
    global _cascade
    if _cascade is None:
        import cv2
        try:
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        except Exception:
            path = ""
        c = cv2.CascadeClassifier(path) if path else None
        _cascade = c if c is not None and not c.empty() else False
    return _cascade


def face_detector_ok() -> bool:
    return bool(_face_detector())


def haar_faces(gray) -> list[tuple[int, int, int, int]]:
    """Detecção de rostos (Haar do OpenCV). Se o OpenCV não trouxer o modelo, devolve [] (enquadramento central)."""
    det = _face_detector()
    if not det:
        return []
    f = det.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(24, 24))
    return [tuple(map(int, r)) for r in f]


def visual_features(path: Path, start: float, end: float, detector=haar_faces, fps_sample: float = 1.0) -> dict:
    import cv2
    cap = cv2.VideoCapture(str(path))
    out = {"motion": 0.0, "face_ratio": 0.0, "face_area": 0.0, "sharp": 0.0, "cuts": 0, "samples": 0}
    if not cap.isOpened():
        return out
    prev, motions, faces, areas, sharps, cuts = None, [], 0, [], [], 0
    t = start
    while t < end:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok:
            break
        h, w = fr.shape[:2]
        sc = 320.0 / w
        g = cv2.cvtColor(cv2.resize(fr, (320, max(2, int(h * sc)))), cv2.COLOR_BGR2GRAY)
        sharps.append(float(cv2.Laplacian(g, cv2.CV_64F).var()))
        fs = detector(g)
        if fs:
            faces += 1
            areas.append(max(a[2] * a[3] for a in fs) / (g.shape[0] * g.shape[1]))
        if prev is not None:
            d = float(np.abs(g.astype(np.float32) - prev.astype(np.float32)).mean() / 255)
            motions.append(d)
            if d > 0.25:
                cuts += 1
        prev = g
        t += 1.0 / fps_sample
    cap.release()
    n = max(1, len(sharps))
    out.update(motion=float(np.mean(motions)) if motions else 0.0, face_ratio=faces / n,
               face_area=float(np.mean(areas)) if areas else 0.0, sharp=float(np.mean(sharps)) if sharps else 0.0,
               cuts=cuts, samples=len(sharps))
    return out


# ------------------------------------------------------------------ transcrição (opcional, interna)
_whisper = None
LEXICON = re.compile(r"(?i)\b(kkk+|haha+|rs+|caramba|caraca|meu deus|n[aã]o acredito|socorro|nossa|olha (isso|s[oó])|"
                     r"s[eé]rio|que isso|para|cala a boca|gente|pelo amor|ai ai|aff+|eita|uau|incr[ií]vel|loucura)\b")


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def transcribe_window(path: Path, start: float, end: float, model: str = "small") -> list[dict] | None:
    """Transcrição LOCAL de um trecho. None se faster-whisper não existir."""
    global _whisper
    if not whisper_available():
        return None
    if _whisper is None:
        from faster_whisper import WhisperModel
        dev = os.environ.get("XH_WHISPER_DEVICE", "cpu")          # "cuda" em executor com GPU (Kaggle)
        _whisper = WhisperModel(model, device=dev, compute_type="float16" if dev == "cuda" else "int8")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "w.wav"
        r = run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(path),
                 "-vn", "-ac", "1", "-ar", "16000", str(wav)], 300)
        if r.returncode != 0:
            return None
        segs, _ = _whisper.transcribe(str(wav), language="pt", vad_filter=True, beam_size=1)
        return [{"start": start + s.start, "end": start + s.end, "text": s.text.strip(),
                 "logprob": getattr(s, "avg_logprob", -0.5)} for s in segs]


def semantic_score(segs: list[dict] | None) -> tuple[float, str]:
    if not segs:
        return 0.0, ""
    text = " ".join(s["text"] for s in segs)
    words = max(1, len(text.split()))
    dur = max(1.0, segs[-1]["end"] - segs[0]["start"])
    hits = len(LEXICON.findall(text))
    ex = text.count("!") + 0.5 * text.count("?")
    dens = words / dur
    sc = clip01(0.15 * hits + 0.08 * ex + 0.25 * clip01((dens - 1.0) / 2.5) + 0.15 * clip01(words / 40))
    return sc, text


# ------------------------------------------------------------------ candidatos + pontuação
@dataclass
class Candidate:
    start: float
    end: float
    peak_t: float
    scores: dict = field(default_factory=dict)
    score: float = 0.0
    category: str = "momento"
    transcript: str = ""
    feats: dict = field(default_factory=dict)


def iter_chunks(duration: float, chunk_s: float, overlap_s: float):
    """Blocos de 30-60 min com sobreposição para não perder momentos nas emendas."""
    if duration <= chunk_s * 1.15:
        yield 0.0, duration
        return
    t = 0.0
    while t < duration:
        end = min(duration, t + chunk_s)
        yield t, end
        if end >= duration:
            break
        t = end - overlap_s


def find_peaks(x: np.ndarray, min_dist: int, thr: float, k: int) -> list[int]:
    order = np.argsort(-x)
    taken: list[int] = []
    for i in order:
        if x[i] < thr or len(taken) >= k:
            break
        if all(abs(i - j) >= min_dist for j in taken):
            taken.append(int(i))
    return sorted(taken)


def propose_windows(sig: AudioSignal, cfg: dict, k: int) -> list[tuple[float, float, float]]:
    if len(sig.exc) == 0:
        return []
    thr = max(0.30, float(np.percentile(sig.exc, 88)))
    peaks = find_peaks(sig.exc, int(10 / HOP), thr, k)
    pauses = pause_points(sig)
    cmin, cpref, chard = cfg["clip_min_s"], cfg["clip_pref_max_s"], cfg["clip_hard_max_s"]
    wins: list[list[float]] = []
    for p in peaks:
        pt = sig.t0 + p * HOP
        s, e = pt - 11.0, pt + 14.0
        if wins and s <= wins[-1][1] + 3 and (e - wins[-1][0]) <= chard:   # funde picos vizinhos
            wins[-1][1] = e
            wins[-1][2] = max(wins[-1][2], pt)
        else:
            wins.append([s, e, pt])
    out = []
    for s, e, pt in wins:
        if len(pauses):
            near_s = pauses[(pauses > s - 4) & (pauses < s + 4)]
            near_e = pauses[(pauses > e - 3) & (pauses < e + 6)]
            if len(near_s):
                s = float(near_s[np.argmin(np.abs(near_s - s))])
            if len(near_e):
                e = float(near_e[np.argmin(np.abs(near_e - e))])
        s = max(sig.t0, s)
        e = min(sig.t0 + len(sig.db) * HOP, e)
        if e - s < cmin:
            e = min(sig.t0 + len(sig.db) * HOP, s + cmin)
        if e - s > chard:
            e = s + chard
        if e - s >= cmin * 0.9:
            out.append((s, e, pt))
    return out


def score_candidate(sig: AudioSignal, s: float, e: float, pt: float, vis: dict, seg_sem: tuple[float, str],
                    pauses: np.ndarray, meta: dict, cfg: dict, used: list[tuple[float, float]], weights: dict | None = None) -> Candidate:
    a, b = sig.idx(s), max(sig.idx(s) + 1, sig.idx(e))
    sl = slice(a, b)
    exc, laugh, scr, rise = sig.exc[sl], sig.laugh[sl], sig.scream[sl], sig.rise[sl]
    db, base = sig.db[sl], sig.base[sl]
    sem, text = seg_sem
    dur = e - s
    # critérios 0..1
    ent = clip01(0.40 * (laugh.max() if len(laugh) else 0) + 0.2 * (scr.max() if len(scr) else 0) + 0.2 * sem
                 + 0.2 * clip01((rise.max() if len(rise) else 0) / 12) + 0.15 * vis.get("motion", 0) * 4 * 0.0)
    ent = clip01(ent + 0.1 * clip01(vis.get("motion", 0) * 6))
    sp_ratio = float(sig.speech[sl].mean()) if b > a else 0
    clipping = float((sig.peak[sl] > 0.98).mean()) if b > a else 0
    snr = clip01((float(db[sig.speech[sl]].mean()) - sig.noise_db) / 30) if sig.speech[sl].any() else 0
    clar = clip01(0.5 * clip01(sp_ratio * 1.5) + 0.3 * (1 - clipping * 5) + 0.2 * snr)
    inten = clip01((float(db.max()) - float(base.mean())) / 15) if len(db) else 0
    near = lambda t: bool(len(pauses)) and float(np.min(np.abs(pauses - t))) < 1.2
    bq = (0.5 if near(s) else 0) + (0.5 if near(e) else 0)
    dfit = 1.0 if cfg["clip_min_s"] <= dur <= cfg["clip_pref_max_s"] else (0.7 if dur <= cfg["clip_hard_max_s"] else 0.0)
    ctx = clip01(0.5 * (0.2 + 0.8 * bq) + 0.3 * dfit + 0.2 * clip01((pt - s) / 6))
    ov = max((max(0, min(e, ue) - max(s, us)) / max(1e-6, dur) for us, ue in used), default=0.0)
    orig = clip01(1 - ov * 1.5)
    e3 = sig.exc[a:a + int(3 / HOP)]
    hook = clip01((float(e3.mean()) if len(e3) else 0) / max(0.05, float(exc.max()) if len(exc) else 1)) * 0.7 + 0.3 * clip01(vis.get("motion", 0) * 8)
    ret = clip01(0.5 * hook + 0.3 * clip01(sp_ratio * 1.4) + 0.2 * (1 if len(exc) and exc[-int(3 / HOP):].mean() > 0.3 * exc.max() else 0.5))
    res = clip01(min(meta.get("height", 720), meta.get("width", 1280)) / 720)
    q_av = clip01(0.35 * res + 0.25 * clip01(vis.get("sharp", 0) / 200) + 0.2 * vis.get("face_ratio", 0) + 0.2 * (1 - clipping * 5))
    indep = clip01(0.5 * bq + 0.3 * (1 - clip01(vis.get("cuts", 0) / 4)) + 0.2 * clip01(sp_ratio * 1.5))
    sc = {"entretenimento": ent, "clareza": clar, "intensidade": inten, "contexto": ctx, "originalidade": orig,
          "retencao": ret, "qualidade_av": q_av, "independencia": indep}
    w = weights or BASE_WEIGHTS
    tot = sum(w.get(k, 0) * v for k, v in sc.items()) / max(1e-9, sum(w.get(k, 0) for k in sc))
    lm, sm, rm = (laugh.max() if len(laugh) else 0), (scr.max() if len(scr) else 0), (rise.max() if len(rise) else 0)
    cat = ("risada/piada" if lm > 0.45 else "grito/susto" if sm > 0.5 else "reação/surpresa" if rm > 9 else
           "fala marcante" if sem > 0.4 else "ação/movimento" if vis.get("motion", 0) > 0.08 else "momento")
    return Candidate(start=s, end=e, peak_t=pt, scores={k: round(v * 100, 1) for k, v in sc.items()},
                     score=round(tot * 100, 1), category=cat, transcript=text,
                     feats={"laugh": float(lm), "scream": float(sm), "rise": float(rm), "speech": sp_ratio,
                            "motion": vis.get("motion", 0), "faces": vis.get("face_ratio", 0)})


def analyze_range(path: Path, t0: float, t1: float, meta: dict, cfg: dict, used: list[tuple[float, float]],
                  weights: dict | None = None, use_whisper: bool = True, whisper_model: str = "small",
                  detector=haar_faces) -> list[Candidate]:
    """Analisa [t0,t1] e devolve candidatos pontuados (ordem decrescente)."""
    db, zcr, pk = audio_hops(path, t0, t1 - t0)
    sig = build_signal(db, zcr, pk, t0)
    hours = max(0.25, (t1 - t0) / 3600)
    k = int(max(8, hours * cfg["clips_per_hour_of_video"] * 3))      # tamanho do "pool" de busca, não limite de produção
    pauses = pause_points(sig)
    cands = []
    for s, e, pt in propose_windows(sig, cfg, k):
        vis = visual_features(path, s, e, detector)
        segs = transcribe_window(path, s, e, whisper_model) if use_whisper else None
        if segs:                                  # alinha aos limites de fala (não corta frase)
            s2, e2 = max(t0, min(s, segs[0]["start"] - 0.3)), min(t1, max(e, segs[-1]["end"] + 0.4))
            if e2 - s2 <= cfg["clip_hard_max_s"]:
                s, e = s2, e2
        cands.append(score_candidate(sig, s, e, pt, vis, semantic_score(segs), pauses, meta, cfg, used, weights))
    cands.sort(key=lambda c: -c.score)
    return cands


def select_best(cands: list[Candidate], min_score: float) -> list[Candidate]:
    """Qualidade antes de quantidade: só ≥ min_score, sem sobreposição. Sem cota artificial."""
    chosen: list[Candidate] = []
    for c in cands:
        if c.score < min_score:
            continue
        if any(min(c.end, o.end) - max(c.start, o.start) > 0.2 * (c.end - c.start) for o in chosen):
            continue
        chosen.append(c)
    return sorted(chosen, key=lambda c: c.start)
