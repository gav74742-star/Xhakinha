from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass


@dataclass
class StepResult:
    status: str               # done | wait | hold
    wait_s: float = 0.0
    note: str = ""


def sign_media_token(secret: str, pub_id: int, ttl_s: int = 6 * 3600, now: float | None = None) -> str:
    exp = int((now or time.time()) + ttl_s)
    body = f"{pub_id}.{exp}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_media_token(secret: str, token: str, now: float | None = None) -> int | None:
    try:
        pid, exp, sig = token.split(".")
        good = hmac.new(secret.encode(), f"{pid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(sig, good) and int(exp) >= (now or time.time()):
            return int(pid)
    except (ValueError, AttributeError):
        pass
    return None


class Publisher:
    platform = "?"

    def __init__(self, s, db):
        self.s, self.db = s, db

    def state(self, pub: dict) -> dict:
        return json.loads(pub.get("state") or "{}")

    def save(self, pub: dict, st: dict, **cols) -> None:
        pub["state"] = json.dumps(st)
        sets = ["state=?", "updated=?"] + [f"{k}=?" for k in cols]
        self.db.x(f"UPDATE publications SET {', '.join(sets)} WHERE id=?", [pub["state"], time.time(), *cols.values(), pub["id"]])
        pub.update(cols)

    def connected(self) -> bool:
        return False

    def step(self, pub: dict, clip: dict) -> StepResult:
        raise NotImplementedError

    def fetch_metrics(self, pub: dict) -> dict | None:
        return None
