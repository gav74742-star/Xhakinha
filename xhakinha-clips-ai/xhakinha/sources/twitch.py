"""Twitch (Helix oficial). Aquisição do VOD via yt-dlp apenas para o canal AUTORIZADO."""
from __future__ import annotations

import re
import time
from pathlib import Path

from .. import http
from ..retry import NotReadyYet, PermanentError
from . import ytdlp
from .base import Connector, ContentInfo
from .youtube import parse_ts

_TDUR = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def twitch_dur(s: str) -> float:
    m = _TDUR.fullmatch(s or "")
    h, mi, se = (int(x or 0) for x in (m.groups() if m else (0, 0, 0)))
    return h * 3600 + mi * 60 + se


class TwitchConnector(Connector):
    platform = "twitch"
    _tok: tuple[str, float] | None = None

    def _token(self) -> str:
        if not (self.s.twitch_client_id and self.s.twitch_client_secret):
            raise PermanentError("TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET não configurados")
        t = TwitchConnector._tok
        if t and t[1] > time.time() + 60:
            return t[0]
        r = http.need_ok(http.request("POST", self.s.twitch_oauth, data={
            "client_id": self.s.twitch_client_id, "client_secret": self.s.twitch_client_secret,
            "grant_type": "client_credentials"}), "Twitch token")
        j = r.json()
        TwitchConnector._tok = (j["access_token"], time.time() + j.get("expires_in", 3600))
        return j["access_token"]

    def _get(self, path: str, params: dict) -> dict:
        h = {"Client-Id": self.s.twitch_client_id, "Authorization": f"Bearer {self._token()}"}
        r = http.request("GET", f"{self.s.twitch_api}/{path}", params=params, headers=h)
        if r.status_code == 401:
            TwitchConnector._tok = None
        return http.need_ok(r, f"Twitch {path}").json()

    def _user(self) -> dict:
        if self.cfg.get("user_id"):
            return self.cfg
        d = self._get("users", {"login": self.src["ref"].lstrip("@").lower()}).get("data") or []
        if not d:
            raise PermanentError(f"Canal Twitch não encontrado: {self.src['ref']}")
        self.cfg["user_id"], self.cfg["login"] = d[0]["id"], d[0]["login"]
        self.save_cfg()
        return self.cfg

    def test(self) -> dict:
        try:
            u = self._user()
            return {"ok": True, "detail": f"Canal Twitch: {u['login']}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def discover(self, known):
        uid = self._user()["user_id"]
        out: list[ContentInfo] = []
        live = self._get("streams", {"user_id": uid}).get("data") or []
        live_ids = set()
        for st in live:
            live_ids.add(st["id"])
            out.append(ContentInfo(ext_id=f"stream:{st['id']}", title=st.get("title", ""), url=f"https://www.twitch.tv/{self.cfg['login']}",
                                   kind="live", live_state="live", published_at=parse_ts(st.get("started_at")),
                                   meta={"stream_id": st["id"]}))
        # transmissões que eu achava ao vivo e sumiram => encerradas
        for k, v in known.items():
            if k.startswith("stream:") and v.get("live_state") == "live" and k.split(":", 1)[1] not in live_ids:
                out.append(ContentInfo(ext_id=k, title=v.get("title", ""), url=v.get("url", ""), kind="live",
                                       live_state="ended", ended_at=time.time(), meta=v.get("meta", {})))
        for v in (self._get("videos", {"user_id": uid, "type": "archive", "first": 20}).get("data") or []):
            if v.get("stream_id") in live_ids:
                continue  # VOD ainda sendo gravado
            out.append(ContentInfo(ext_id=f"video:{v['id']}", title=v.get("title", ""), url=v["url"], kind="vod",
                                   duration=twitch_dur(v.get("duration", "")), published_at=parse_ts(v.get("created_at")),
                                   meta={"stream_id": v.get("stream_id")}))
        return out

    def list_archive(self, page=None, limit=50):
        uid = self._user()["user_id"]
        p = {"user_id": uid, "type": "archive", "first": min(limit, 100)}
        if page:
            p["after"] = page
        d = self._get("videos", p)
        infos = [ContentInfo(ext_id=f"video:{v['id']}", title=v.get("title", ""), url=v["url"], kind="archive",
                             duration=twitch_dur(v.get("duration", "")), published_at=parse_ts(v.get("created_at")))
                 for v in d.get("data") or []]
        return infos, (d.get("pagination") or {}).get("cursor")

    def download(self, content, dest: Path) -> Path:
        if content["ext_id"].startswith("stream:"):
            raise NotReadyYet("aguardando o VOD da transmissão", retry_after=600)
        return ytdlp.download(content["url"], dest)
