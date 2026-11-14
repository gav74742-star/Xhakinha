"""URL avulsa de fonte autorizada (a autorização é atestada no cadastro)."""
from __future__ import annotations

from pathlib import Path

from . import ytdlp
from .base import Connector, ContentInfo


class UrlSource(Connector):
    platform = "url"
    can_monitor = False

    def test(self):
        try:
            ytdlp.validate_url(self.src["ref"])
            st = ytdlp.check_available()
            return {"ok": st["ok"], "detail": st.get("version") or st.get("error")}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def discover(self, known):
        return [ContentInfo(ext_id=self.src["ref"], title=self.src["name"], url=self.src["ref"], kind="vod")] \
            if self.src["ref"] not in known else []

    def download(self, content, dest: Path) -> Path:
        return ytdlp.download(content["url"], dest)
