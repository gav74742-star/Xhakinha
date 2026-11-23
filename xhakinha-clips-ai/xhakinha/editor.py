"""Edição: 9:16, enquadramento com rastreamento de rosto, loudnorm, marca d'água por plataforma.
Vídeos finais são LIMPOS: sem legendas/títulos/textos. Única exceção: o @ discreto (versão por plataforma)."""
from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .analysis import haar_faces
from .media import probe, run
from .retry import PermanentError, TransientError

PRESET = os.environ.get("XH_FFMPEG_PRESET", "veryfast")
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]


def ev(x: float) -> int:
    return max(2, int(x) // 2 * 2)


def out_dims(ref_h: int, max_w: int = 1080) -> tuple[int, int]:
    """Dimensões 9:16 exatas, limitadas pela qualidade da fonte (nunca ampliamos além de ~1.8x)."""
    for w, h, need in ((1080, 1920, 1000), (720, 1280, 640), (540, 960, 0)):
        if ref_h >= need and w <= max_w:
            return w, h
    return 540, 960


# ---------------------------------------------------------------- rastreamento
def track_faces(path: Path, start: float, dur: float, w: int, h: int, detector=haar_faces, step: float = 0.5) -> dict:
    """Retorna {'knots': [(t, cx, cy)], 'face_h': média relativa, 'mode': 'face'|'center'}. cx,cy normalizados."""
    import cv2
    cap = cv2.VideoCapture(str(path))
    pts: list[tuple[float, float, float, float] | None] = []
    t = 0.0
    prev = None
    while t < dur and cap.isOpened():
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000)
        ok, fr = cap.read()
        if not ok:
            break
        sc = 480.0 / fr.shape[1]
        g = cv2.cvtColor(cv2.resize(fr, (480, max(2, int(fr.shape[0] * sc)))), cv2.COLOR_BGR2GRAY)
        fs = detector(g)
        gh, gw = g.shape
        sel = None
        if fs:
            big = max(f[2] * f[3] for f in fs)
            cands = [f for f in fs if f[2] * f[3] >= 0.5 * big]
            if prev is not None:
                f = min(cands, key=lambda f: abs((f[0] + f[2] / 2) / gw - prev))
            else:
                f = max(cands, key=lambda f: f[2] * f[3])
            sel = ((f[0] + f[2] / 2) / gw, (f[1] + f[3] / 2) / gh, f[3] / gh)
            prev = sel[0]
        pts.append((t, *sel) if sel else None)
        t += step
    cap.release()
    good = [p for p in pts if p]
    if len(good) < max(2, 0.3 * max(1, len(pts))):
        return {"knots": [(0.0, 0.5, 0.45), (dur, 0.5, 0.45)], "face_h": 0.0, "mode": "center"}
    ts = np.array([p[0] for p in good])
    cx = np.array([p[1] for p in good])
    cy = np.array([p[2] for p in good])
    fh = float(np.mean([p[3] for p in good]))
    # mediana de 5 + EMA e limite de velocidade (sem movimentos bruscos)
    def med(a):
        pad = np.pad(a, 2, mode="edge")
        return np.array([np.median(pad[i:i + 5]) for i in range(len(a))])
    cx, cy = med(cx), med(cy)
    sm = [cx[0]]
    for i in range(1, len(cx)):
        tgt = sm[-1] + 0.35 * (cx[i] - sm[-1])
        maxstep = 0.12 * (ts[i] - ts[i - 1])           # ≤ 12% da largura por segundo
        sm.append(sm[-1] + float(np.clip(tgt - sm[-1], -maxstep, maxstep)))
    knots = [(float(ts[i]), float(sm[i]), float(cy[i])) for i in range(len(ts))]
    knots = knots[::2] if len(knots) > 60 else knots                       # ≤ ~60 nós
    if knots[0][0] > 0:
        knots.insert(0, (0.0, knots[0][1], knots[0][2]))
    if knots[-1][0] < dur:
        knots.append((dur, knots[-1][1], knots[-1][2]))
    return {"knots": knots, "face_h": fh, "mode": "face"}


def x_expr(knots: list[tuple[float, float]], iw: int, cw: int) -> str:
    """Expressão linear por partes de x(t) para o filtro crop."""
    def px(c):  # centro normalizado -> x do canto, clampado
        return max(0.0, min(iw - cw, c * iw - cw / 2))
    pts = [(t, px(c)) for t, c in knots]
    expr = f"{pts[-1][1]:.1f}"
    for i in range(len(pts) - 1, 0, -1):
        (t0, x0), (t1, x1) = pts[i - 1], pts[i]
        dt = max(1e-3, t1 - t0)
        expr = f"if(lt(t,{t1:.3f}),{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{dt:.3f},{expr})"
    return expr


# ---------------------------------------------------------------- render
def render_clip(src: Path, dest: Path, start: float, dur: float, info: dict | None = None,
                detector=haar_faces, max_w: int = 1080) -> dict:
    """Gera o clipe MASTER (limpo). Escreve em .tmp e renomeia (atômico)."""
    info = info or probe(src)
    iw, ih = info["width"], info["height"]
    dur = min(dur, max(1.0, info["duration"] - start))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    script = dest.with_suffix(".filter.txt")
    mode = "face"
    if iw / ih < 9 / 16 * 1.02:      # fonte vertical/estreita: encaixa e preenche fundo desfocado
        ow, oh = out_dims(ih, max_w)
        fg = f"[0:v]split[a][b];[a]scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},boxblur=30:5[bg];" \
             f"[b]scale={ow}:{oh}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
        mode = "fit"
    else:
        tr = track_faces(src, start, dur, iw, ih, detector)
        mode = tr["mode"]
        zoom = 1.0
        if tr["face_h"] and tr["face_h"] < 0.14:
            zoom = min(1.35, 0.2 / max(0.05, tr["face_h"]))       # zoom suave e constante (sem pulsar)
        ch = ev(ih / zoom)
        cw = ev(ch * 9 / 16)
        if cw > iw:
            cw = ev(iw); ch = ev(cw * 16 / 9)
        cy = float(np.mean([k[2] for k in tr["knots"]]))
        y = int(max(0, min(ih - ch, cy * ih - ch * 0.42)))
        xe = x_expr([(t, c) for t, c, _ in tr["knots"]], iw, cw)
        ow, oh = out_dims(ch, max_w)
        fg = f"[0:v]crop=w={cw}:h={ch}:x='{xe}':y={y},scale={ow}:{oh}:flags=lanczos,setsar=1[v]"
    script.write_text(fg)
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
            "-filter_complex_script", str(script), "-map", "[v]"]
    if info["has_audio"]:
        args += ["-map", "0:a:0", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,alimiter=limit=0.95",
                 "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    else:
        args += ["-an"]
    args += ["-c:v", "libx264", "-preset", PRESET, "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high",
             "-movflags", "+faststart", "-sn", "-dn", str(tmp)]
    # Compatibilidade entre versões novas e antigas do FFmpeg.
    modern_args = args.copy()
    modern_args[modern_args.index("-filter_complex_script")] = "-/filter_complex"
    r = run(modern_args, 3600)
    err = (r.stderr or "").lower()
    if r.returncode != 0 and "unrecognized option" in err and "filter_complex" in err:
        r = run(args, 3600)
    script.unlink(missing_ok=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise TransientError(f"ffmpeg falhou: {r.stderr[-300:]}")
    out = probe(tmp)
    if out["vcodec"] != "h264" or out["duration"] < 1:
        tmp.unlink(missing_ok=True)
        raise TransientError("saída inválida do render")
    os.replace(tmp, dest)
    return {"width": out["width"], "height": out["height"], "duration": out["duration"], "mode": mode,
            "size": dest.stat().st_size}


# ---------------------------------------------------------------- marca d'água por plataforma
def watermark_png(text: str, out_w: int, out_h: int, opacity: float, scale: float, dest: Path) -> tuple[int, int]:
    size = max(14, int(out_h * scale))
    font = None
    for f in FONTS:
        if os.path.exists(f):
            font = ImageFont.truetype(f, size)
            break
    font = font or ImageFont.load_default(size)
    tmp = Image.new("RGBA", (10, 10))
    l, t, r, b = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
    pad = max(2, size // 6)
    img = Image.new("RGBA", (r - l + 2 * pad, b - t + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    a = int(255 * opacity)
    d.text((pad - l + 1, pad - t + 1), text, font=font, fill=(0, 0, 0, int(a * 0.6)))      # sombra p/ legibilidade
    d.text((pad - l, pad - t), text, font=font, fill=(255, 255, 255, a))
    img.save(dest)
    return img.size


def position_xy(pos: str, W: int, H: int, w: int, h: int) -> tuple[int, int]:
    mx, my_top, my_bot = int(W * 0.04), int(H * 0.10), int(H * 0.20)    # evita UI de Reels/TikTok
    return {"top_left": (mx, my_top), "top_right": (W - w - mx, my_top), "bottom_left": (mx, H - h - my_bot),
            "bottom_right": (W - w - mx, H - h - my_bot), "top_center": ((W - w) // 2, my_top)}.get(pos, (mx, my_top))


def apply_watermark(master: Path, dest: Path, handle: str, opacity: float = 0.65, position: str = "top_left",
                    scale: float = 0.035) -> Path:
    """Gera a versão da plataforma: master + @ discreto. Sem @ configurado => cópia exata (nada é inventado)."""
    handle = (handle or "").strip()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not handle:
        shutil.copyfile(master, dest)
        return dest
    if not handle.startswith("@"):
        handle = "@" + handle
    info = probe(master)
    png = dest.with_suffix(".wm.png")
    w, h = watermark_png(handle, info["width"], info["height"], opacity, scale, png)
    x, y = position_xy(position, info["width"], info["height"], w, h)
    tmp = dest.with_suffix(".tmp.mp4")
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(master), "-i", str(png),
            "-filter_complex", f"[0:v][1:v]overlay={x}:{y}:format=auto[v]", "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", PRESET, "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", "-sn", str(tmp)]
    r = run(args, 1800)
    png.unlink(missing_ok=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise TransientError(f"watermark falhou: {r.stderr[-300:]}")
    os.replace(tmp, dest)
    return dest
