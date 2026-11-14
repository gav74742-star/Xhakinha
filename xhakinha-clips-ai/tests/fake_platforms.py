"""SERVIDOR FALSO (simulado) de YouTube/Twitch/TikTok/Instagram. Serve só para testar a LÓGICA HTTP do cliente.
Resultados dos testes que usam isto NÃO provam integração real com as plataformas."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Fake:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.flags = {"yt_quota": False, "tt_status_seq": ["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"], "tt_public_id": True,
                      "ig_container_seq": ["IN_PROGRESS", "FINISHED"], "tt_fail": None, "tt_error_code": None,
                      "video_file": None, "ig_publish_fail_once": False, "yt_live": False}
        self.n = {"tt_init": 0, "tt_status": 0, "ig_container": 0, "ig_publish": 0, "ig_status": 0, "tt_upload": 0, "yt_videos": 0}
        self.published_ig: dict | None = None
        self.srv: ThreadingHTTPServer | None = None

    def start(self) -> str:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _j(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self): self.route("GET")
            def do_POST(self): self.route("POST")
            def do_PUT(self): self.route("PUT")

            def route(self, m):
                u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake.calls.append((m, u.path))
                raw = self._body()
                P = u.path
                # ---------------- YouTube
                if P.startswith("/yt/channels"):
                    if fake.flags["yt_quota"]:
                        return self._j({"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}}, 403)
                    return self._j({"items": [{"id": "UC" + "a" * 22, "snippet": {"title": "Canal Teste"}}]})
                if P.startswith("/yt/playlistItems"):
                    return self._j({"items": [{"contentDetails": {"videoId": "vid1"}}]})
                if P.startswith("/yt/videos"):
                    fake.n["yt_videos"] += 1
                    if fake.flags["yt_quota"]:
                        return self._j({"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}}, 403)
                    return self._j({"items": [{"id": "vid1", "snippet": {"title": "Live X", "liveBroadcastContent": "none", "publishedAt": "2026-01-01T00:00:00Z", "channelTitle": "Canal Teste"},
                                               "contentDetails": {"duration": "PT1H2M3S"}, "liveStreamingDetails": {"actualEndTime": "2026-01-01T02:00:00Z"}}]})
                if P.startswith("/yt/rss"):
                    xml = '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>vid1</yt:videoId></entry></feed>'
                    b = xml.encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); return self.wfile.write(b)
                # ---------------- TikTok
                if P == "/v2/oauth/token/":
                    return self._j({"access_token": "newtok", "refresh_token": "newref", "expires_in": 86400})
                if P == "/v2/post/publish/creator_info/query/":
                    return self._j({"data": {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"]}, "error": {"code": "ok"}})
                if P == "/v2/post/publish/video/init/":
                    fake.n["tt_init"] += 1
                    body = json.loads(raw or b"{}"); fake.tt_init_body = body
                    if fake.flags["tt_error_code"]:
                        return self._j({"error": {"code": fake.flags["tt_error_code"], "message": "x"}})
                    return self._j({"data": {"publish_id": f"pid{fake.n['tt_init']}", "upload_url": f"{fake.base}/tt_upload/{fake.n['tt_init']}"}, "error": {"code": "ok"}})
                if P.startswith("/tt_upload/"):
                    fake.n["tt_upload"] += 1
                    return self._j({}, 201)
                if P == "/v2/post/publish/status/fetch/":
                    seq = fake.flags["tt_status_seq"]; i = min(fake.n["tt_status"], len(seq) - 1); fake.n["tt_status"] += 1
                    st = seq[i]
                    d = {"status": st}
                    if st == "FAILED": d["fail_reason"] = fake.flags["tt_fail"] or "internal"
                    if st == "PUBLISH_COMPLETE" and fake.flags["tt_public_id"]: d["publicaly_available_post_id"] = [7123456789]
                    return self._j({"data": d, "error": {"code": "ok"}})
                # ---------------- Instagram (Graph)
                if P.endswith("/media") and m == "POST":
                    fake.n["ig_container"] += 1; fake.ig_body = parse_qs(raw.decode())
                    return self._j({"id": f"cont{fake.n['ig_container']}"})
                if P.endswith("/media") and m == "GET":
                    import time as _t
                    ts = _t.strftime("%Y-%m-%dT%H:%M:%S+0000", _t.gmtime())
                    return self._j({"data": [{"id": "mediaX", "permalink": "https://www.instagram.com/reel/ABC/", "timestamp": ts}]})
                if P.endswith("/media_publish"):
                    fake.n["ig_publish"] += 1
                    if fake.flags["ig_publish_fail_once"] and fake.n["ig_publish"] == 1:
                        fake.published_ig = {"id": "mediaX"}          # a API publicou mas a resposta se perdeu
                        self.send_response(500); self.end_headers(); return
                    fake.published_ig = {"id": "mediaX"}
                    return self._j({"id": "mediaX"})
                if P.endswith("/content_publishing_limit"):
                    return self._j({"data": [{"quota_usage": 1, "config": {"quota_total": 100}}]})
                if P.startswith("/ig/cont"):
                    fake.n["ig_status"] += 1
                    seq = list(fake.flags["ig_container_seq"]); i = min(fake.n["ig_status"] - 1, len(seq) - 1)
                    sc = seq[i]
                    if fake.published_ig: sc = "PUBLISHED"
                    return self._j({"id": P.split("/")[-1], "status_code": sc})
                if P.startswith("/ig/mediaX"):
                    return self._j({"id": "mediaX", "permalink": "https://www.instagram.com/reel/ABC/", "timestamp": "2026-01-01T00:00:00+0000"})
                if P.startswith("/ig/") and P.count("/") == 2 and m == "GET":
                    return self._j({"id": "1789", "username": "conta_teste"})
                # ---------------- "host público" do vídeo (simula nosso painel)
                if P.startswith("/media/"):
                    data = open(fake.flags["video_file"], "rb").read() if fake.flags["video_file"] else b"\0" * 10
                    self.send_response(206 if self.headers.get("Range") else 200); self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(data[:2]) if self.headers.get("Range") else len(data))); self.end_headers()
                    return self.wfile.write(data[:2] if self.headers.get("Range") else data)
                self._j({"error": "not found"}, 404)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self.base

    def stop(self):
        if self.srv:
            self.srv.shutdown()
