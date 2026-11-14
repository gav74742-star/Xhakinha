"""Aquisição via yt-dlp — SOMENTE conteúdo próprio/autorizado. Nunca contorna login, DRM ou bloqueios."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from ..retry import NotReadyYet, PermanentError, TransientError

ALLOWED_HOSTS = ("youtube.com", "youtu.be", "twitch.tv", "instagram.com", "tiktok.com", "kick.com", "facebook.com", "vimeo.com")
_PERMANENT = re.compile(r"(?i)private video|sign in|log in|login required|members[- ]only|drm|age[- ]restricted|"
                        r"confirm your age|not available in your country|geo|has been removed|video unavailable|"
                        r"copyright|unsupported url|subscriber|premium|requires authentication|cookies")
_NOTREADY = re.compile(r"(?i)live event will begin|premieres in|is live|currently live|stream is offline|"
                       r"this live event|processing|not yet available|will be available")
_TRANSIENT = re.compile(r"(?i)http error (429|5\d\d)|timed out|temporar|connection (reset|refused|aborted)|"
                        r"unable to download|remote end closed|ssl|network|try again|too many requests")


def find_ytdlp() -> list[str] | None:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


def check_available() -> dict:
    cmd = find_ytdlp()
    if not cmd:
        return {"ok": False, "error": "yt-dlp NÃO instalado (pip install yt-dlp)"}
    try:
        out = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=30)
        return {"ok": out.returncode == 0, "version": out.stdout.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def validate_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise PermanentError("URL precisa ser https://")
    host = u.hostname.lower()
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise PermanentError(f"Host não permitido para aquisição: {host}")
    if any(ch in url for ch in " \n\r\t;|&`$<>"):
        raise PermanentError("URL contém caracteres inválidos")
    return url


def classify(stderr: str) -> Exception:
    msg = stderr.strip().splitlines()[-1][:300] if stderr.strip() else "erro desconhecido"
    if _NOTREADY.search(stderr):
        return NotReadyYet(msg, retry_after=600)
    if _PERMANENT.search(stderr):
        return PermanentError(f"Acesso não autorizado/indisponível (não vamos contornar): {msg}")
    if _TRANSIENT.search(stderr):
        return TransientError(msg)
    return TransientError(msg)


def download(url: str, dest: Path, *, max_height: int = 1080, max_filesize_gb: float = 8.0, timeout: int = 7200) -> Path:
    cmd = find_ytdlp()
    if not cmd:  # falha UMA vez, sem gastar retentativas
        raise PermanentError("yt-dlp não está instalado. Rode: pip install yt-dlp (ou use a imagem Docker oficial do projeto).")
    validate_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem = dest.with_suffix("")
    args = cmd + ["--no-playlist", "--no-warnings", "--no-cookies-from-browser", "-f",
                  f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b", "--merge-output-format", "mp4",
                  "--max-filesize", f"{int(max_filesize_gb * 1024)}M", "--retries", "3", "--fragment-retries", "3",
                  "-o", f"{stem}.%(ext)s", "--", url]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TransientError("yt-dlp excedeu o tempo limite")
    if r.returncode != 0:
        raise classify(r.stderr or r.stdout)
    for cand in (dest, *stem.parent.glob(stem.name + ".*")):
        if cand.is_file() and cand.suffix in (".mp4", ".mkv", ".webm", ".mov"):
            return cand
    raise TransientError("yt-dlp terminou sem gerar arquivo")
