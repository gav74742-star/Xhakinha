"""Gerenciador de cotas: limites configuráveis, margem de segurança, reserva para prioridade,
previsão, rate limiting e circuit breaker. Estimativas conservadoras quando a cota é desconhecida."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Valores PADRÃO e CONSERVADORES (ver docs/PESQUISA-MULTICLOUD.md; todos editáveis na tabela providers).
# known=False => a plataforma não expõe consulta de cota: tratamos como estimativa e sinalizamos incerteza.
PROVIDER_DEFAULTS: dict[str, dict] = {
    "kaggle-cpu": {"session_seconds": 11 * 3600, "week_seconds": 0, "day_seconds": 0, "safety": 0.9,
                   "reserve_frac": 0.1, "known": False, "kind": "executor", "gpu": False},
    "kaggle-gpu": {"session_seconds": 11 * 3600, "week_seconds": 27 * 3600, "day_seconds": 0, "safety": 0.9,
                   "reserve_frac": 0.15, "known": False, "kind": "executor", "gpu": True},
    "colab": {"session_seconds": 6 * 3600, "week_seconds": 0, "day_seconds": 0, "safety": 0.8,
              "reserve_frac": 0.0, "known": False, "kind": "executor", "gpu": False},
    "github": {"session_seconds": 20 * 60, "week_seconds": 0, "day_seconds": 2 * 3600, "safety": 0.8,
               "reserve_frac": 0.0, "known": False, "kind": "executor", "gpu": False},
    "local": {"session_seconds": 24 * 3600, "week_seconds": 0, "day_seconds": 0, "safety": 1.0,
              "reserve_frac": 0.0, "known": True, "kind": "executor", "gpu": False},
}
PRIORITY_RESERVED = 8          # tarefas com prioridade >= 8 podem usar a reserva
START_MARGIN_S = 120           # não iniciar tarefa que não caiba na sessão com esta folga


def period_keys(now: float) -> dict[str, str]:
    d = datetime.fromtimestamp(now, timezone.utc)
    iso = d.isocalendar()
    return {"day": d.strftime("%Y-%m-%d"), "week": f"{iso[0]}-W{iso[1]:02d}", "session": ""}


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    uncertain: bool = False
    remaining: dict = field(default_factory=dict)


def decide(cfg: dict, usage: dict[str, float], est_seconds: float, priority: int, gpu_task: bool,
           time_left: float | None) -> Decision:
    """Pura e testável. usage = {'day':s,'week':s}. Retorna se PODE iniciar (nunca estoura o limite estimado)."""
    safety = float(cfg.get("safety", 0.85))
    reserve = float(cfg.get("reserve_frac", 0.0)) if priority < PRIORITY_RESERVED else 0.0
    unc = not cfg.get("known", False)
    rem: dict[str, float] = {}
    sess = float(cfg.get("session_seconds") or 0)
    if time_left is not None:
        cap = time_left - START_MARGIN_S
        if sess:
            cap = min(cap, sess * safety)
        rem["session"] = cap
        if est_seconds > cap:
            return Decision(False, f"não cabe na sessão (resta {cap:.0f}s, estimado {est_seconds:.0f}s)", unc, rem)
    for per, key in (("day", "day_seconds"), ("week", "week_seconds")):
        lim = float(cfg.get(key) or 0)
        if not lim:
            continue
        if per == "week" and not gpu_task and cfg.get("gpu"):
            continue
        budget = lim * safety * (1 - reserve)
        left = budget - usage.get(per, 0.0)
        rem[per] = left
        if est_seconds > left:
            return Decision(False, f"cota {per} estimada esgotada (resta {left:.0f}s de {budget:.0f}s)", unc, rem)
    return Decision(True, "ok", unc, rem)


def forecast(cfg: dict, usage: dict[str, float], recent_rate_per_hour: float) -> dict:
    """Previsão simples: horas até esgotar o orçamento estimado ao ritmo atual."""
    out = {}
    for per, key in (("day", "day_seconds"), ("week", "week_seconds")):
        lim = float(cfg.get(key) or 0) * float(cfg.get("safety", 0.85))
        if lim:
            left = max(0.0, lim - usage.get(per, 0.0))
            out[per] = {"left_s": left, "hours_to_exhaust": (left / 3600 / recent_rate_per_hour) if recent_rate_per_hour > 0 else None}
    return out


def breaker_next(state: str, failures: int, opens: int, opened_at: float | None, now: float, *,
                 ok: bool | None = None, threshold: int = 3, base: float = 120.0, cap: float = 3600.0):
    """Máquina do circuit breaker. Retorna (state, failures, opens, opened_at, allow_now)."""
    cool = min(cap, base * (2 ** max(0, opens - 1))) if opens else 0.0
    if ok is True:
        return "closed", 0, 0, None, True
    if ok is False:
        failures += 1
        if state == "half_open" or failures >= threshold:
            return "open", failures, opens + 1, now, False
        return state, failures, opens, opened_at, True
    # consulta
    if state == "open":
        if opened_at is not None and now - opened_at >= cool:
            return "half_open", failures, opens, opened_at, True      # deixa passar UMA sonda
        return "open", failures, opens, opened_at, False
    return state, failures, opens, opened_at, True


class RateLimiter:
    """Token bucket (thread-safe). `acquire` devolve segundos de espera (0 = livre)."""

    def __init__(self, rate_per_s: float, burst: int, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate_per_s, float(burst), clock
        self.tokens, self.t = float(burst), clock()
        self.lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> float:
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens >= n:
                self.tokens -= n
                return 0.0
            return (n - self.tokens) / self.rate

    def wait(self, n: float = 1.0, sleep=time.sleep) -> None:
        w = self.acquire(n)
        while w > 0:
            sleep(min(w, 5.0))
            w = self.acquire(n)
