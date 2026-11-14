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
        day = self._day(now)
        with self.db.tx() as c:
            row = c.execute("SELECT used, exhausted_until FROM quota WHERE day=? AND api=?", (day, self.api)).fetchone()
            used = row["used"] if row else 0
            ex = row["exhausted_until"] if row else None
            if ex and (now or time.time()) < ex:
                raise QuotaExceeded(f"cota {self.api} esgotada até o reset", retry_after=max(60, ex - (now or time.time())))
            if used + units > self.limit:
                raise QuotaExceeded(f"cota própria {self.api} atingida ({used}/{self.limit})",
                                    retry_after=max(60, self._next_reset(now) - (now or time.time())))
            c.execute("INSERT INTO quota(day,api,used) VALUES(?,?,?) ON CONFLICT(day,api) DO UPDATE SET used=used+?",
                      (day, self.api, units, units))

    def mark_exhausted(self, now: float | None = None) -> None:
        """Chamado quando a API responde 403 quotaExceeded: bloqueia até o reset real."""
        self.db.x("INSERT INTO quota(day,api,used,exhausted_until) VALUES(?,?,?,?) ON CONFLICT(day,api) DO UPDATE SET exhausted_until=excluded.exhausted_until",
                  (self._day(now), self.api, self.limit, self._next_reset(now)))
