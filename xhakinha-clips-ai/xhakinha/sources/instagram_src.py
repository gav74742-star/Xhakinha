"""Instagram como FONTE: somente mídia da PRÓPRIA conta, via Graph API oficial (media_url)."""
from __future__ import annotations

from pathlib import Path

from .. import http
from ..retry import PermanentError, TransientError
from .base import Connector, ContentInfo
from .youtube import parse_ts


class InstagramSource(Connector):
    platform = "instagram"

    def _acct(self) -> dict:
        a = self.db.get_secret("account:instagram")
        if not a:
            raise PermanentError("Conta do Instagram não conectada (aba Contas)")
        return a

    def test(self) -> dict:
        try:
            a = self._acct()
            r = http.need_ok(http.request("GET", f"{self.s.graph_base}/{a['ig_user_id']}",
                                          params={"fields": "username", "access_token": a["access_token"]}), "Instagram")
            return {"ok": True, "detail": f"@{r.json().get('username')}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _media(self, page=None, limit=25):
        a = self._acct()
        p = {"fields": "id,media_type,media_url,permalink,timestamp,caption", "limit": limit, "access_token": a["access_token"]}
        if page:
            p["after"] = page
        d = http.need_ok(http.request("GET", f"{self.s.graph_base}/{a['ig_user_id']}/media", params=p), "Instagram media").json()
        infos = [ContentInfo(ext_id=m["id"], title=(m.get("caption") or "")[:120], url=m.get("permalink", ""), kind="vod",
                             published_at=parse_ts(m.get("timestamp")), meta={"media_url": m.get("media_url")})
                 for m in d.get("data", []) if m.get("media_type") in ("VIDEO", "REELS") and m.get("media_url")]
        return infos, ((d.get("paging") or {}).get("cursors") or {}).get("after") if (d.get("paging") or {}).get("next") else None

    def discover(self, known):
        infos, _ = self._media(limit=10)
        return [i for i in infos if i.ext_id not in known]

    def list_archive(self, page=None, limit=50):
        infos, nxt = self._media(page, 25)
        for i in infos:
            i.kind = "archive"
        return infos, nxt

    def download(self, content, dest: Path) -> Path:
        import json
        url = (json.loads(content.get("meta") or "{}")).get("media_url")
        if not url:
            raise PermanentError("media_url ausente")
        r = http.request("GET", url, stream=True, timeout=120)
        http.need_ok(r, "download Instagram")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for ch in r.iter_content(1 << 20):
                f.write(ch)
        return dest
