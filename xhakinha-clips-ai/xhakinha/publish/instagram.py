"""Instagram Graph API — Reels. Requer URL pública temporária (assinada, expira) servida pelo próprio painel.
Container -> status_code=FINISHED -> media_publish -> permalink. Só vira 'published' após o media_id real."""
from __future__ import annotations

import time

from .. import http
from ..retry import PermanentError, TransientError
from .base import Publisher, StepResult, sign_media_token


class InstagramPublisher(Publisher):
    platform = "instagram"

    def account(self) -> dict:
        a = self.db.get_secret("account:instagram")
        if not a:
            raise PermanentError("Conta do Instagram não conectada (aba Contas)")
        return a

    def connected(self) -> bool:
        return bool(self.db.get_secret("account:instagram"))

    def _g(self, method: str, path: str, **params) -> dict:
        a = self.account()
        params["access_token"] = a["access_token"]
        kw = {"params": params} if method == "GET" else {"data": params}
        r = http.request(method, f"{self.s.graph_base}/{path}", **kw)
        try:
            j = r.json()
        except ValueError:
            raise TransientError(f"Graph {path}: resposta não-JSON (HTTP {r.status_code})")
        if r.status_code >= 400 or "error" in j:
            e = j.get("error", {})
            code, msg = e.get("code"), e.get("message", "")[:200]
            if code in (1, 2, 4, 17, 32, 613) or e.get("is_transient"):
                raise TransientError(f"Graph {path}: {code} {msg}")
            raise PermanentError(f"Graph {path}: HTTP {r.status_code} code={code} {msg}")
        return j

    def public_url(self, pub: dict, size: int) -> str:
        base = self.s.public_base_url
        if not (base.startswith("https://") or (self.s.env == "test" and base.startswith("http://"))):
            raise PermanentError("PUBLIC_BASE_URL (https) não configurada: o Instagram precisa baixar o vídeo de uma URL pública. "
                                 "Veja o tutorial (HTTPS gratuito com Caddy + DuckDNS ou Cloudflare Tunnel).")
        url = f"{base}/media/{sign_media_token(self.s.secret_key, pub['id'])}.mp4"
        try:
            r = http.request("GET", url, headers={"Range": "bytes=0-1"}, timeout=20)
        except TransientError:
            raise TransientError("URL pública do vídeo inacessível agora (rede)")
        if r.status_code not in (200, 206) or "video" not in r.headers.get("Content-Type", ""):
            raise PermanentError(f"URL pública não serve o vídeo (HTTP {r.status_code}). Verifique domínio/HTTPS/proxy.")
        return url

    def step(self, pub: dict, clip: dict) -> StepResult:
        st = self.state(pub)
        stage = st.get("stage", "container")
        ig = self.account()["ig_user_id"]
        if stage == "container":
            from pathlib import Path
            p = Path(pub["file_path"] or "")
            if not p.is_file():
                raise PermanentError("arquivo da publicação ausente (será regenerado do clipe)")
            url = self.public_url(pub, p.stat().st_size)
            j = self._g("POST", f"{ig}/media", media_type="REELS", video_url=url, caption=(st.get("caption") or "")[:2200],
                        share_to_feed="true")
            st.update(stage="poll", container_id=j["id"], video_url=url, created_ts=time.time())
            self.save(pub, st, remote_id=j["id"], status="uploading")
            return StepResult("wait", 20, "container criado")
        if stage == "poll":
            j = self._g("GET", st["container_id"], fields="status_code,status")
            sc = j.get("status_code")
            if sc == "IN_PROGRESS":
                self.save(pub, st, status="processing")
                return StepResult("wait", 30, sc)
            if sc == "FINISHED":
                st["stage"] = "publish"
                self.save(pub, st)
            elif sc == "PUBLISHED":
                st["stage"] = "confirm"
                self.save(pub, st)
            elif sc in ("ERROR", "EXPIRED"):
                st["errors"] = st.get("errors", 0) + 1
                if st["errors"] > 2:
                    raise PermanentError(f"Instagram container {sc}: {j.get('status', '')[:200]}")
                st.update(stage="container")
                self.save(pub, st)
                raise TransientError(f"Instagram container {sc}; recriando (nada foi publicado)")
            else:
                raise TransientError(f"status_code inesperado: {sc!r}")
            stage = st["stage"]
        if stage == "publish":
            if st.get("hold") and not st.get("confirmed"):
                self.save(pub, st, status="awaiting_confirmation")
                return StepResult("hold", 0, "aguardando confirmação do administrador (Instagram não tem modo privado)")
            # idempotência: se uma tentativa anterior já publicou, não publicar de novo
            if self._g("GET", st["container_id"], fields="status_code").get("status_code") == "PUBLISHED":
                st["stage"] = "confirm"
            else:
                try:
                    lim = self._g("GET", f"{ig}/content_publishing_limit", fields="quota_usage,config")
                    d0 = (lim.get("data") or [{}])[0]
                    if d0.get("quota_usage", 0) >= d0.get("config", {}).get("quota_total", 100):
                        raise TransientError("limite diário de publicações da API do Instagram atingido", retry_after=3600)
                except PermanentError:
                    pass
                j = self._g("POST", f"{ig}/media_publish", creation_id=st["container_id"])
                st.update(stage="confirm", media_id=j["id"])
            self.save(pub, st)
            stage = "confirm"
        if stage == "confirm":
            mid = st.get("media_id")
            if not mid:       # publicação aconteceu mas o id não foi gravado: busca nas mídias recentes
                ml = self._g("GET", f"{ig}/media", fields="id,permalink,timestamp", limit=5)
                from datetime import datetime
                cand = None
                for m in ml.get("data") or []:       # só aceita mídia criada DEPOIS do nosso container (evita ID errado)
                    try:
                        ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
                    except (KeyError, ValueError):
                        continue
                    if ts >= st.get("created_ts", 0) - 60:
                        cand = m
                        break
                if not cand:
                    raise TransientError("publicado, aguardando o ID aparecer")
                mid = cand["id"]
            j = self._g("GET", mid, fields="id,permalink,timestamp")
            self.save(pub, st, status="published", public_id=j["id"], url=j.get("permalink"), published_at=time.time(),
                      visibility="PUBLIC", error=None, needs_admin=0)
            return StepResult("done", 0, "publicado")
        raise PermanentError(f"estado desconhecido: {stage}")

    def fetch_metrics(self, pub: dict) -> dict | None:
        if not pub.get("public_id"):
            return None
        try:
            m = self._g("GET", pub["public_id"], fields="like_count,comments_count")
            out = {"likes": m.get("like_count"), "comments": m.get("comments_count"), "views": None, "shares": None,
                   "retention": None, "raw": dict(m)}
            try:
                ins = self._g("GET", f"{pub['public_id']}/insights", metric="views,shares,ig_reels_avg_watch_time")
                for it in ins.get("data", []):
                    v = (it.get("values") or [{}])[0].get("value")
                    if it["name"] == "views":
                        out["views"] = v
                    elif it["name"] == "shares":
                        out["shares"] = v
                    elif it["name"] == "ig_reels_avg_watch_time" and v is not None:
                        out["raw"]["avg_watch_ms"] = v
                        out["_avg_ms"] = v
            except (PermanentError, TransientError):
                pass            # algumas métricas podem não existir para a conta
            return out
        except Exception:
            return None
