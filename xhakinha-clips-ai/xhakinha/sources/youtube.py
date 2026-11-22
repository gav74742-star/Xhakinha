"""YouTube: descoberta por RSS (custo 0 de cota) + Data API só para detalhes (1 unidade/50 vídeos)."""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .. import http
from ..retry import PermanentError, QuotaExceeded, TransientError
from . import ytdlp
from .base import Connector, ContentInfo
from .quota import Quota

_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def parse_dur(s: str) -> float:
    m = _DUR.fullmatch(s or "")
    if not m:
        return 0.0
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def extract_video_id(ref: str) -> str | None:
    """Extrai um ID de vídeo de uma referência ou URL reconhecida do YouTube."""
    ref = (ref or "").strip()

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", ref):
        return ref

    try:
        parsed = urlparse(ref if "://" in ref else "https://" + ref)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in {"youtube.com", "m.youtube.com", "music.youtube.com",
                    "youtu.be", "youtube-nocookie.com"}:
        return None

    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [None])[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            video_id = parts[1]

    return video_id if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else None


class YouTubeConnector(Connector):
    platform = "youtube"
    COST_LIST = 1

    def __init__(self, *a):
        super().__init__(*a)
        self.quota = Quota(self.db, "youtube", self.s.youtube_daily_quota)
        self._etag = self.cfg.get("rss_etag")

    # --- chamadas à API com contabilidade de cota ---------------------------
    def _api(self, path: str, params: dict, cost: int = 1) -> dict:
        if not self.s.youtube_api_key:
            raise PermanentError("YOUTUBE_API_KEY não configurada")
        self.quota.charge(cost)
        r = http.request("GET", f"{self.s.youtube_base}/{path}", params={**params, "key": self.s.youtube_api_key})
        if r.status_code == 403 and "quota" in r.text.lower():
            self.quota.mark_exhausted()
            raise QuotaExceeded("YouTube respondeu quotaExceeded; pausando até o reset", retry_after=3600)
        http.need_ok(r, f"YouTube {path}")
        return r.json()

    def video_id(self) -> str | None:
        return extract_video_id(self.src.get("ref", ""))

    def channel_id(self) -> str:
        if self.video_id():
            raise PermanentError("Esta fonte é um vídeo individual, não um canal")
        if self.cfg.get("channel_id"):
            return self.cfg["channel_id"]
        ref = self.src["ref"].strip()
        if re.fullmatch(r"UC[\w-]{22}", ref):
            cid = ref
        else:
            handle = ref if ref.startswith("@") else "@" + ref
            d = self._api("channels", {"part": "id", "forHandle": handle})
            items = d.get("items") or []
            if not items:
                raise PermanentError(f"Canal não encontrado: {ref}")
            cid = items[0]["id"]
        self.cfg["channel_id"] = cid
        self.save_cfg()
        return cid

    def test(self) -> dict:
        try:
            video_id = self.video_id()
            if video_id:
                items = self._details([video_id])
                if not items:
                    raise PermanentError("Vídeo não encontrado ou indisponível")
                return {"ok": True, "detail": f"Vídeo: {items[0].title}",
                        "quota_used_today": self.quota.used()}

            cid = self.channel_id()
            d = self._api("channels", {"part": "snippet", "id": cid})
            n = (d.get("items") or [{}])[0].get("snippet", {}).get("title")
            return {"ok": bool(n), "detail": f"Canal: {n}" if n else "canal sem dados",
                    "quota_used_today": self.quota.used()}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # --- descoberta ----------------------------------------------------------
    def _rss_ids(self) -> list[str]:
        r = http.request("GET", self.s.youtube_rss, params={"channel_id": self.channel_id()},
                         headers={"If-None-Match": self._etag} if self._etag else {})
        if r.status_code == 304:
            return []
        http.need_ok(r, "YouTube RSS")
        if r.headers.get("ETag"):
            self.cfg["rss_etag"] = self._etag = r.headers["ETag"]
        root = ET.fromstring(r.content)
        return [e.findtext("yt:videoId", namespaces=NS) for e in root.findall("a:entry", NS) if e.findtext("yt:videoId", namespaces=NS)]

    def _details(self, ids: list[str]) -> list[ContentInfo]:
        out = []
        for i in range(0, len(ids), 50):
            d = self._api("videos", {"part": "snippet,contentDetails,liveStreamingDetails", "id": ",".join(ids[i:i + 50])})
            for it in d.get("items", []):
                sn, ls = it.get("snippet", {}), it.get("liveStreamingDetails", {})
                lbc = sn.get("liveBroadcastContent", "none")
                ended = parse_ts(ls.get("actualEndTime"))
                was_live = bool(ls)
                state = "live" if lbc == "live" else "upcoming" if lbc == "upcoming" else ("ended" if was_live else None)
                out.append(ContentInfo(
                    ext_id=it["id"], title=sn.get("title", ""), url=f"https://www.youtube.com/watch?v={it['id']}",
                    kind="live" if was_live else "vod", duration=parse_dur(it.get("contentDetails", {}).get("duration", "")),
                    live_state=state, ended_at=ended, published_at=parse_ts(sn.get("publishedAt")),
                    meta={"channel": sn.get("channelTitle")}))
        return out

    def discover(self, known: dict[str, dict]) -> list[ContentInfo]:
        video_id = self.video_id()
        if video_id:
            previous = known.get(video_id)
            if previous and previous.get("live_state") not in ("live", "upcoming"):
                return []
            return self._details([video_id])

        ids = self._rss_ids()
        self.save_cfg()
        want = [i for i in ids if i not in known or known[i].get("live_state") in ("live", "upcoming")]
        # lives já conhecidas em andamento que sumiram do RSS também precisam ser reconsultadas
        want += [k for k, v in known.items() if v.get("live_state") in ("live", "upcoming") and k not in want]
        return self._details(want[:150]) if want else []

    def list_archive(self, page=None, limit=50):
        if self.video_id():
            return [], None
        cid = self.channel_id()
        up = "UU" + cid[2:]
        p = {"part": "contentDetails", "playlistId": up, "maxResults": min(limit, 50)}
        if page:
            p["pageToken"] = page
        d = self._api("playlistItems", p)
        ids = [i["contentDetails"]["videoId"] for i in d.get("items", [])]
        infos = [c for c in self._details(ids) if c.live_state not in ("live", "upcoming")]
        for c in infos:
            c.kind = "archive"
        return infos, d.get("nextPageToken")

    def download(self, content: dict, dest: Path) -> Path:
        if content.get("live_state") in ("live", "upcoming"):
            from ..retry import NotReadyYet
            raise NotReadyYet("transmissão ainda em andamento", retry_after=600)
        return ytdlp.download(content["url"], dest)
