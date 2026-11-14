"""Cliente HTTP com classificação de erros (transitório x permanente)."""
from __future__ import annotations

import requests

from .retry import PermanentError, QuotaExceeded, TransientError

_session = requests.Session()


def request(method: str, url: str, *, timeout: float = 30, **kw) -> requests.Response:
    try:
        r = _session.request(method, url, timeout=timeout, **kw)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise TransientError(f"rede: {type(e).__name__}") from e
    if r.status_code == 429:
        ra = r.headers.get("Retry-After")
        raise TransientError("HTTP 429 (limite de requisições)", retry_after=float(ra) if ra and ra.isdigit() else 120)
    if r.status_code >= 500 or r.status_code in (408, 425):
        raise TransientError(f"HTTP {r.status_code}")
    return r


def need_ok(r: requests.Response, what: str) -> requests.Response:
    if r.status_code in (401, 403):
        raise PermanentError(f"{what}: HTTP {r.status_code} (credencial inválida/sem permissão): {r.text[:200]}")
    if r.status_code >= 400:
        raise PermanentError(f"{what}: HTTP {r.status_code}: {r.text[:200]}")
    return r
