"""Controle de cota diária por API (YouTube reseta à meia-noite do Pacífico)."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..db import Store
from ..retry import QuotaExceeded

PT = ZoneInfo("America/Los_Angeles")


class Quota:
    def __init__(self, db: Store, api: str, daily_limit: int, tz: ZoneInfo = PT):
        self.db, self.api, self.limit, self.tz = db, api, daily_limit, tz

    def _day(self, now: float | None = None) -> str:
        return datetime.fromtimestamp(now or time.time(), self.tz).strftime("%Y-%m-%d")

    def _next_reset(self, now: float | None = None) -> float:
        d = datetime.fromtimestamp(now or time.time(), self.tz)
        nxt = (d.replace(hour=0, minute=0, second=0, microsecond=0)).timestamp() + 86400
        return nxt + 60

    def used(self, now: float | None = None) -> int:
        r = self.db.one("SELECT used FROM quota WHERE day=? AND api=?", (self._day(now), self.api))
        return r["used"] if r else 0

    def charge(self, units: int, now: float | None = None) -> None:
        day, t = self._day(now), (now or time.time())
        if units <= self.limit:
            # débito atômico em UM comando (sem transação interativa): só soma se couber no limite e não estiver esgotada
            n = self.db.conn.execute(
                "INSERT INTO quota(day,api,used) VALUES(?,?,?) ON CONFLICT(day,api) DO UPDATE SET used=used+? "
                "WHERE used+?<=? AND (exhausted_until IS NULL OR exhausted_until<=?)",
                (day, self.api, units, units, units, self.limit, t)).rowcount
            if n:
                return
        row = self.db.one("SELECT used, exhausted_until FROM quota WHERE day=? AND api=?", (day, self.api))
        used = row["used"] if row else 0
        ex = row["exhausted_until"] if row else None
        if ex and t < ex:
            raise QuotaExceeded(f"cota {self.api} esgotada até o reset", retry_after=max(60, ex - t))
        raise QuotaExceeded(f"cota própria {self.api} atingida ({used}/{self.limit})",
                            retry_after=max(60, self._next_reset(now) - t))

    def mark_exhausted(self, now: float | None = None) -> None:
        """Chamado quando a API responde 403 quotaExceeded: bloqueia até o reset real."""
        self.db.x("INSERT INTO quota(day,api,used,exhausted_until) VALUES(?,?,?,?) ON CONFLICT(day,api) DO UPDATE SET exhausted_until=excluded.exhausted_until",
                  (self._day(now), self.api, self.limit, self._next_reset(now)))
