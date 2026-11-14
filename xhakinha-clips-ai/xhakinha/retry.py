"""Erros classificados + backoff exponencial com jitter."""
from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class XhError(Exception):
    pass


class TransientError(XhError):
    """Falha temporária: vale tentar de novo (rede, 5xx, 429, cota que reseta)."""
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class PermanentError(XhError):
    """Exige intervenção (credencial ausente, vídeo privado, ferramenta não instalada).
    NÃO consome retentativas: vai para a fila de falhas e espera o administrador."""


class NotReadyYet(TransientError):
    """Conteúdo ainda não disponível (ex.: live acabou, VOD ainda processando).
    Reagendado sem consumir tentativas (limitado por prazo)."""


class QuotaExceeded(TransientError):
    pass


def backoff_delay(attempt: int, base: float = 20.0, cap: float = 3600.0, jitter: float = 0.3,
                  rnd: Callable[[], float] = random.random) -> float:
    """attempt começa em 1. Exponencial (base*2^(n-1)), teto `cap`, +-jitter."""
    d = min(cap, base * (2 ** max(0, attempt - 1)))
    return max(0.0, d * (1 + jitter * (2 * rnd() - 1)))


def classify_http(status: int) -> type[XhError] | None:
    """None = sucesso."""
    if status < 400:
        return None
    if status in (408, 409, 425, 429) or status >= 500:
        return TransientError
    return PermanentError


def call_with_retry(fn: Callable[[], T], *, attempts: int = 4, base: float = 1.0, cap: float = 30.0,
                    sleep: Callable[[float], None] = time.sleep, on_retry: Callable | None = None) -> T:
    """Retentativa síncrona curta (dentro de um job). Só repete TransientError."""
    last: Exception | None = None
    for n in range(1, attempts + 1):
        try:
            return fn()
        except TransientError as e:
            last = e
            if n == attempts:
                break
            delay = e.retry_after if e.retry_after else backoff_delay(n, base, cap)
            if on_retry:
                on_retry(n, e, delay)
            sleep(delay)
    assert last is not None
    raise last
