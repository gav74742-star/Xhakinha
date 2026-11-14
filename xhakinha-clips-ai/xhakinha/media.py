"""Utilitários de mídia (ffprobe/ffmpeg) — sempre sem shell, com argumentos em lista."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .retry import PermanentError, TransientError


def run(args: list[str], timeout: int = 7200) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise PermanentError(f"Executável ausente: {args[0]} (instale ffmpeg)")
    except subprocess.TimeoutExpired:
        raise TransientError(f"{args[0]} excedeu o tempo limite")


def probe(path: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 120)
    if r.returncode != 0:
        raise PermanentError(f"Arquivo de vídeo inválido/corrompido: {r.stderr[:200]}")
    j = json.loads(r.stdout or "{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise PermanentError("Arquivo não contém vídeo")
    fr = v.get("avg_frame_rate", "0/1")
    try:
        n, d = fr.split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except ValueError:
        fps = 0.0
    dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
    return {"duration": dur, "width": int(v["width"]), "height": int(v["height"]), "fps": fps or 30.0,
            "vcodec": v.get("codec_name"), "has_audio": a is not None, "acodec": a.get("codec_name") if a else None,
            "subtitle_streams": sum(1 for s in j.get("streams", []) if s.get("codec_type") == "subtitle")}
