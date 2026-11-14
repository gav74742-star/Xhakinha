from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..db import Store


@dataclass
class ContentInfo:
    ext_id: str
    title: str
    url: str
    kind: str                       # live | vod | archive
    duration: float | None = None
    live_state: str | None = None   # live | upcoming | ended | None
    ended_at: float | None = None
    published_at: float | None = None
    meta: dict = field(default_factory=dict)


class Connector:
    platform = "?"
    can_monitor = True

    def __init__(self, source: dict, s: Settings, db: Store):
        self.src, self.s, self.db = source, s, db
        self.cfg = json.loads(source.get("config") or "{}")

    def test(self) -> dict:                      # -> {"ok": bool, "detail": str}
        raise NotImplementedError

    def discover(self, known: dict[str, dict]) -> list[ContentInfo]:
        raise NotImplementedError

    def list_archive(self, page: str | None = None, limit: int = 50) -> tuple[list[ContentInfo], str | None]:
        return [], None

    def download(self, content: dict, dest: Path) -> Path:
        raise NotImplementedError

    def save_cfg(self) -> None:
        self.db.x("UPDATE sources SET config=? WHERE id=?", (json.dumps(self.cfg), self.src["id"]))


def make_connector(source: dict, s: Settings, db: Store) -> Connector:
    p = source["platform"]
    if p == "youtube":
        from .youtube import YouTubeConnector as C
    elif p == "twitch":
        from .twitch import TwitchConnector as C
    elif p == "instagram":
        from .instagram_src import InstagramSource as C
    elif p == "url":
        from .urlsrc import UrlSource as C
    else:
        raise ValueError(f"plataforma de fonte desconhecida: {p}")
    return C(source, s, db)
