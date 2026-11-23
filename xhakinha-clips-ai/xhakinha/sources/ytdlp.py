"""Aquisição via yt-dlp — conteúdo próprio ou autorizado."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from ..retry import NotReadyYet, PermanentError, TransientError

ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "twitch.tv", "instagram.com",
    "tiktok.com", "kick.com", "facebook.com", "vimeo.com",
)

_PERMANENT = re.compile(
    r"(?i)private video|sign in|log in|login required|members[- ]only|"
    r"drm|age[- ]restricted|confirm your age|not available in your country|"
    r"geo restriction|has been removed|video unavailable|copyright|"
    r"unsupported url|subscriber|premium|requires authentication"
)
_NOTREADY = re.compile(
    r"(?i)live event will begin|premieres in|is live|currently live|"
    r"stream is offline|this live event|processing|not yet available|"
    r"will be available"
)
_TRANSIENT = re.compile(
    r"(?i)http error (429|5\d\d)|timed out|temporar|"
    r"connection (reset|refused|aborted)|unable to download|"
    r"remote end closed|ssl|network|try again|too many requests|"
    r"fragment not found|server returned"
)


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
        out = subprocess.run(
            cmd + ["--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {"ok": out.returncode == 0, "version": out.stdout.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise PermanentError("URL inválida.")
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise PermanentError("URL precisa ser https://")
    host = u.hostname.lower()
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise PermanentError(f"Host não permitido para aquisição: {host}")
    if any(ch in url for ch in " \n\r\t;|&`$<>"):
        raise PermanentError("URL contém caracteres inválidos.")
    return url


def _cookie_args() -> list[str]:
    configured = os.environ.get("YTDLP_COOKIEFILE", "").strip()
    if not configured:
        return []

    cookiefile = Path(configured).expanduser()
    if not cookiefile.is_file():
        raise PermanentError(
            "YTDLP_COOKIEFILE foi configurado, mas o arquivo de cookies não existe."
        )
    return ["--cookies", str(cookiefile)]


def classify(stderr: str) -> Exception:
    text = stderr.strip()
    msg = text.splitlines()[-1][:300] if text else "erro desconhecido"

    if _NOTREADY.search(text):
        return NotReadyYet(msg, retry_after=600)

    if _PERMANENT.search(text):
        return PermanentError(
            f"Acesso não autorizado ou conteúdo indisponível: {msg}"
        )

    if _TRANSIENT.search(text):
        return TransientError(msg)

    return TransientError(msg)


def download(
    url: str,
    dest: Path,
    *,
    max_height: int = 1080,
    max_filesize_gb: float = 8.0,
    timeout: int = 7200,
) -> Path:
    cmd = find_ytdlp()
    if not cmd:
        raise PermanentError(
            "yt-dlp não está instalado. Rode: pip install yt-dlp "
            "(ou use a imagem Docker oficial do projeto)."
        )

    validate_url(url)

    if max_height < 1 or max_filesize_gb <= 0 or timeout < 1:
        raise PermanentError("Parâmetros de download inválidos.")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem = dest.with_suffix("")

    args = (
        cmd
        + [
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout", "30",
            "--extractor-retries", "3",
            "--retries", "5",
            "--fragment-retries", "5",
            "--file-access-retries", "3",
            "--retry-sleep", "http:linear=1:5",
            "--retry-sleep", "fragment:linear=1:5",
            "--concurrent-fragments", "4",
            "-f",
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
            "--merge-output-format", "mp4",
            "--max-filesize", f"{int(max_filesize_gb * 1024)}M",
        ]
        + _cookie_args()
        + [
            "-o", f"{stem}.%(ext)s",
            "--",
            url,
        ]
    )

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise TransientError("yt-dlp excedeu o tempo limite.")
    except OSError as exc:
        raise TransientError(f"Não foi possível executar yt-dlp: {exc}")

    if result.returncode != 0:
        raise classify(result.stderr or result.stdout)

    candidates = (dest, *stem.parent.glob(stem.name + ".*"))
    for candidate in candidates:
        if (
            candidate.is_file()
            and candidate.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
            and candidate.stat().st_size > 0
        ):
            return candidate

    raise TransientError("yt-dlp terminou sem gerar um arquivo de vídeo válido.")
