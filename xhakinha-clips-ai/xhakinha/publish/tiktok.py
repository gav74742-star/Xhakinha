"""TikTok Content Posting API (Direct Post, FILE_UPLOAD). Fluxo e restrições conferidos na doc oficial.

Não marca 'published' sem status PUBLISH_COMPLETE. Salva o ID público SOMENTE se a API devolver
`publicaly_available_post_id` (grafia oficial). Apps não auditados: conteúdo restrito a privado (SELF_ONLY)."""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlencode

from .. import http
from ..retry import PermanentError, TransientError
from .base import Publisher, StepResult

PERM_CODES = {"access_token_invalid", "scope_not_authorized", "scope_permission_missed", "token_not_authorized_for_specified_scopes",
              "spam_risk_too_many_posts", "spam_risk_user_banned_from_posting", "reached_active_user_cap",
              "unaudited_client_can_only_post_to_private_accounts", "url_ownership_unverified", "privacy_level_option_mismatch",
              "user_banned_from_posting", "invalid_param"}
TRANS_CODES = {"rate_limit_exceeded", "internal_error", "internal", "service_unavailable"}


class TikTokPublisher(Publisher):
    platform = "tiktok"

    # --- conta/token -------------------------------------------------------
    def account(self) -> dict:
        a = self.db.get_secret("account:tiktok")
        if not a:
            raise PermanentError("Conta do TikTok não conectada (aba Contas → Conectar TikTok)")
        return a

    def connected(self) -> bool:
        return bool(self.db.get_secret("account:tiktok"))

    def _token(self) -> str:
        a = self.account()
        if a.get("expires_at", 0) > time.time() + 120:
            return a["access_token"]
        if not a.get("refresh_token"):
            raise PermanentError("Token TikTok expirou e não há refresh_token; reconecte a conta")
        r = http.request("POST", f"{self.s.tiktok_api}/v2/oauth/token/", data={
            "client_key": self.s.tiktok_client_key, "client_secret": self.s.tiktok_client_secret,
            "grant_type": "refresh_token", "refresh_token": a["refresh_token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        j = r.json() if r.content else {}
        if r.status_code != 200 or "access_token" not in j:
            raise PermanentError(f"Falha ao renovar token TikTok: {str(j)[:200]}")
        a.update(access_token=j["access_token"], refresh_token=j.get("refresh_token", a["refresh_token"]),
                 expires_at=time.time() + int(j.get("expires_in", 86400)))
        self.db.put_secret("account:tiktok", a)
        return a["access_token"]

    def _call(self, path: str, body: dict) -> dict:
        r = http.request("POST", f"{self.s.tiktok_api}{path}", json=body,
                         headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json; charset=UTF-8"})
        try:
            j = r.json()
        except ValueError:
            raise TransientError(f"TikTok {path}: resposta não-JSON (HTTP {r.status_code})")
        err = j.get("error") or {}
        code = err.get("code", "ok")
        if code != "ok":
            msg = f"TikTok {path}: {code}: {err.get('message', '')[:200]}"
            if code in TRANS_CODES:
                raise TransientError(msg)
            raise PermanentError(msg + (" — dica: apps NÃO auditados só postam em contas privadas; deixe a conta privada ou conclua a auditoria"
                                        if code == "unaudited_client_can_only_post_to_private_accounts" else ""))
        return j.get("data") or {}

    # --- máquina de estados idempotente ---------------------------------------
    def step(self, pub: dict, clip: dict) -> StepResult:
        st = self.state(pub)
        stage = st.get("stage", "init")
        path = Path(pub["file_path"] or "")
        if stage in ("init", "upload") and not path.is_file():
            raise PermanentError("arquivo da publicação ausente (será regenerado do clipe)")
        if stage == "init":
            ci = self._call("/v2/post/publish/creator_info/query/", {})
            opts = ci.get("privacy_level_options") or ["SELF_ONLY"]
            want = "SELF_ONLY" if not self.db.get("tiktok_audited") else self.db.get("tiktok_privacy", "SELF_ONLY")
            privacy = want if want in opts else ("SELF_ONLY" if "SELF_ONLY" in opts else opts[0])
            size = path.stat().st_size
            if size > 64 * 1024 * 1024:
                raise PermanentError("vídeo > 64MB para upload em 1 bloco")
            info = {"title": (st.get("caption") or "")[:2200], "privacy_level": privacy, "disable_duet": False,
                    "disable_comment": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000}
            d = self._call("/v2/post/publish/video/init/", {"post_info": info, "source_info": {
                "source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}})
            if not d.get("publish_id") or not d.get("upload_url"):
                raise TransientError("init sem publish_id/upload_url")
            st.update(stage="upload", publish_id=d["publish_id"], upload_url=d["upload_url"], privacy=privacy)
            self.save(pub, st, remote_id=d["publish_id"], status="uploading", visibility=privacy)
            stage = "upload"
        if stage == "upload":
            size = path.stat().st_size
            with open(path, "rb") as f:
                r = http.request("PUT", st["upload_url"], data=f, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(size), "Content-Range": f"bytes 0-{size - 1}/{size}"})
            if r.status_code not in (200, 201, 206):
                if r.status_code in (403, 404, 410):      # upload_url expirou: nada foi publicado ainda -> reinicia
                    st = {"stage": "init", "caption": st.get("caption"), "reinit": st.get("reinit", 0) + 1}
                    if st["reinit"] > 2:
                        raise PermanentError(f"upload recusado repetidamente (HTTP {r.status_code})")
                    self.save(pub, st)
                    raise TransientError(f"upload_url expirada (HTTP {r.status_code}); reiniciando init")
                raise TransientError(f"upload HTTP {r.status_code}")
            st["stage"] = "poll"
            self.save(pub, st, status="processing")
        # poll
        d = self._call("/v2/post/publish/status/fetch/", {"publish_id": st["publish_id"]})
        status = d.get("status", "")
        if status in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"):
            return StepResult("wait", 45, status)
        if status == "SEND_TO_USER_INBOX":
            self.save(pub, st, status="awaiting_confirmation", needs_admin=1, error="Enviado como rascunho para a caixa do TikTok; publique no app.")
            return StepResult("hold", 0, "rascunho na inbox")
        if status == "PUBLISH_COMPLETE":
            ids = d.get("publicaly_available_post_id") or []
            pid = str(ids[0]) if ids else None
            url = None
            uname = self.account().get("username")
            if pid and uname:
                url = f"https://www.tiktok.com/@{uname}/video/{pid}"
            self.save(pub, st, status="published", public_id=pid, url=url, published_at=time.time(),
                      visibility=st.get("privacy"), error=None, needs_admin=0)
            return StepResult("done", 0, "PUBLISH_COMPLETE" + ("" if pid else " (sem ID público: post privado/não auditado)"))
        if status == "FAILED":
            reason = d.get("fail_reason", "")
            if reason == "internal":
                st.update(stage="init")
                self.save(pub, st)
                raise TransientError("TikTok FAILED: internal (retentável) — nada foi publicado")
            raise PermanentError(f"TikTok FAILED: {reason}")
        raise TransientError(f"status TikTok inesperado: {status!r}")

    def fetch_metrics(self, pub: dict) -> dict | None:
        if not pub.get("public_id"):
            return None            # sem ID público (ex.: SELF_ONLY) a API de exibição não retorna métricas
        try:
            d = self._call("/v2/video/query/?fields=id,view_count,like_count,comment_count,share_count",
                           {"filters": {"video_ids": [pub["public_id"]]}})
            v = (d.get("videos") or [None])[0]
            return {"views": v.get("view_count"), "likes": v.get("like_count"), "comments": v.get("comment_count"),
                    "shares": v.get("share_count"), "retention": None, "raw": v} if v else None
        except Exception:
            return None


def authorize_url(client_key: str, redirect_uri: str, state: str, base: str) -> str:
    q = urlencode({"client_key": client_key, "scope": "user.info.basic,video.publish,video.upload,video.list",
                   "response_type": "code", "redirect_uri": redirect_uri, "state": state})
    return f"{base}?{q}"
