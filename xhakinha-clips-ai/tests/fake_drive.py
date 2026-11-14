"""Servidor HTTP que IMITA a API do Google Drive v3 (SIMULAÇÃO — não é o Google). Cobre: OAuth refresh, list (q),
create, upload resumível (308/200, consulta de progresso), download, trash, about.storageQuota e injeção de falhas."""
import hashlib
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeDrive:
    def __init__(self, limit=None):
        self.files = {}
        self.sessions = {}
        self.limit = limit
        self.fail_put_once = 0
        self.fail_token = False
        self.revoked = False
        self.reject_quota = False
        self.requests = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()

    def used(self):
        return sum(len(f["data"]) for f in self.files.values() if not f["trashed"])

    def _meta(self, fid):
        f = self.files[fid]
        out = {"id": fid, "name": f["name"], "mimeType": f["mime"], "modifiedTime": f"2026-01-01T00:00:{f['n']:02d}Z"}
        if f["mime"] != "application/vnd.google-apps.folder":
            out["size"] = str(len(f["data"]))
            out["md5Checksum"] = hashlib.md5(f["data"]).hexdigest()
        return out

    def _query(self, q):
        res = []
        for fid, f in self.files.items():
            if f["trashed"] and "trashed=false" in q.replace(" ", ""):
                continue
            ok = True
            m = re.search(r"name='([^']*)'", q)
            if m and f["name"] != m[1]: ok = False
            m = re.search(r"'([^']+)' in parents", q)
            if m and m[1] not in f["parents"]: ok = False
            if "mimeType='application/vnd.google-apps.folder'" in q and f["mime"] != "application/vnd.google-apps.folder": ok = False
            if "mimeType!='application/vnd.google-apps.folder'" in q and f["mime"] == "application/vnd.google-apps.folder": ok = False
            if ok:
                res.append(self._meta(fid))
        return res

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, code, obj=None, headers=None, raw=None):
                body = raw if raw is not None else json.dumps(obj or {}).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _auth(self):
                if outer.revoked:
                    self._send(401, {"error": "expired"}); return False
                return self.headers.get("Authorization", "").startswith("Bearer ")

            def do_POST(self):
                u = urlparse(self.path); qs = parse_qs(u.query); b = self._body()
                outer.requests.append(("POST", u.path))
                if u.path == "/token":
                    if outer.fail_token:
                        return self._send(400, {"error": "invalid_grant"})
                    return self._send(200, {"access_token": "AT-" + uuid.uuid4().hex, "expires_in": 3600})
                if not self._auth():
                    return self._send(401) if not outer.revoked else None
                if u.path == "/drive/v3/files":
                    m = json.loads(b or b"{}")
                    fid = uuid.uuid4().hex
                    outer.files[fid] = {"name": m["name"], "parents": m.get("parents", []), "mime": m.get("mimeType", "application/octet-stream"),
                                        "data": b"", "trashed": False, "n": len(outer.files) % 60}
                    return self._send(200, outer._meta(fid))
                if u.path == "/upload/drive/v3/files" and qs.get("uploadType") == ["resumable"]:
                    if outer.reject_quota:
                        return self._send(403, {"error": {"errors": [{"reason": "storageQuotaExceeded"}]}})
                    m = json.loads(b)
                    sid = uuid.uuid4().hex
                    outer.sessions[sid] = {"meta": m, "data": b"", "total": int(self.headers["X-Upload-Content-Length"])}
                    return self._send(200, {}, {"Location": f"{outer.base}/upload/session/{sid}"})
                self._send(404)

            def do_PUT(self):
                u = urlparse(self.path); b = self._body()
                outer.requests.append(("PUT", u.path))
                if not self._auth():
                    return
                sid = u.path.rsplit("/", 1)[1]
                s = outer.sessions.get(sid)
                if not s:
                    return self._send(404)
                if outer.fail_put_once > 0:
                    outer.fail_put_once -= 1
                    return self._send(503, {"error": "backend"})
                cr = self.headers["Content-Range"]
                if cr.startswith("bytes */"):
                    h = {"Range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
                    return self._send(308, {}, h)
                m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
                start = int(m[1])
                if start != len(s["data"]):
                    return self._send(308, {}, {"Range": f"bytes=0-{len(s['data']) - 1}"})
                s["data"] += b
                if len(s["data"]) >= s["total"]:
                    if outer.limit is not None and outer.used() + len(s["data"]) > outer.limit:
                        return self._send(403, {"error": {"errors": [{"reason": "storageQuotaExceeded"}]}})
                    fid = uuid.uuid4().hex
                    meta = s["meta"]
                    outer.files[fid] = {"name": meta["name"], "parents": meta.get("parents", []), "mime": "application/octet-stream",
                                        "data": s["data"], "trashed": False, "n": len(outer.files) % 60}
                    return self._send(200, outer._meta(fid))
                self._send(308, {}, {"Range": f"bytes=0-{len(s['data']) - 1}"})

            def do_GET(self):
                u = urlparse(self.path); qs = parse_qs(u.query)
                outer.requests.append(("GET", u.path))
                if not self._auth():
                    return
                if u.path == "/drive/v3/files":
                    return self._send(200, {"files": outer._query(qs.get("q", [""])[0])})
                if u.path == "/drive/v3/about":
                    q = {"usage": str(outer.used())}
                    if outer.limit:
                        q["limit"] = str(outer.limit)
                    return self._send(200, {"storageQuota": q})
                m = re.match(r"/drive/v3/files/(\w+)", u.path)
                if m and m[1] in outer.files:
                    return self._send(200, raw=outer.files[m[1]]["data"])
                self._send(404)

            def do_PATCH(self):
                u = urlparse(self.path); b = self._body()
                if not self._auth():
                    return
                m = re.match(r"/drive/v3/files/(\w+)", u.path)
                if m and m[1] in outer.files:
                    outer.files[m[1]]["trashed"] = json.loads(b).get("trashed", False)
                    return self._send(200, {})
                self._send(404)
        return H
