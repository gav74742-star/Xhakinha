"""Healthcheck honesto: o que está de fato disponível NESTE executor (sem inventar)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile

from .. import analysis


def gpu_info() -> dict:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return {"gpu": True, "name": r.stdout.strip().splitlines()[0]}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return {"gpu": False}


def local_report(drive=None) -> dict:
    du = shutil.disk_usage(tempfile.gettempdir())
    rep = {"python": sys.version.split()[0], "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
           "whisper": analysis.whisper_available(), "faces": analysis.face_detector_ok(), "disk_free_gb": round(du.free / 1e9, 1),
           **gpu_info()}
    if drive is not None:
        try:
            q = drive.quota()
            rep["drive"] = {"ok": True, "limit": q.get("limit"), "usage": q.get("usage")}
        except Exception as e:
            rep["drive"] = {"ok": False, "error": str(e)[:150]}
    rep["ok"] = rep["ffmpeg"] and rep["ffprobe"] and rep["disk_free_gb"] > 1 and rep.get("drive", {"ok": True})["ok"]
    return rep


if __name__ == "__main__":
    import json
    print(json.dumps(local_report(), indent=2))
