"""Armazenamento no Google Drive (e backend de diretório local para testes/offline).

Regras anti-perda (todas aplicadas aqui, não "por convenção"):
 - upload resumível + verificação de MD5 devolvido pelo Drive; só é "ok" se conferir;
 - download grava em .part, confere hash e só então renomeia;
 - NUNCA apaga originais/clipes: `delete` só aceita prefixos descartáveis (snapshots/, checkpoints/, tmp/);
 - arquivos nunca são sobrescritos (versão nova = nome novo); o ponteiro de snapshot é o manifesto escrito POR ÚLTIMO;
 - antes de enviar, confere espaço livre (DriveFull sem tocar no que existe)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

import requests

from ..retry import PermanentError, QuotaExceeded, TransientError, call_with_retry
from .quota import RateLimiter

ROOT = "XHAKINHA_CLIPS_AI"
LAYOUT = ["originals", "clips/pending", "clips/published", "clips/failed", "db", "db/snapshots", "checkpoints",
          "logs", "exports", "tmp"]
DELETABLE = ("db/snapshots/", "checkpoints/", "tmp/", "logs/")
CHUNK = 8 * 1024 * 1024            # múltiplo de 256 KiB, exigido pelo Drive


class DriveFull(QuotaExceeded):
    pass


class IntegrityError(PermanentError):
    pass


def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


class DriveBackend:
    """Interface. `path` é sempre relativo à pasta raiz (ex.: 'clips/pending/clip_1.mp4')."""
    min_free_bytes = 512 * 1024 * 1024

    def ensure_layout(self) -> None: raise NotImplementedError
    def stat(self, path: str) -> dict | None: raise NotImplementedError
    def _put(self, local: Path, path: str, md5: str) -> dict: raise NotImplementedError
    def download_raw(self, path: str, dest: Path) -> dict: raise NotImplementedError
    def listdir(self, prefix: str) -> list[dict]: raise NotImplementedError
    def _delete(self, path: str) -> None: raise NotImplementedError
    def quota(self) -> dict: raise NotImplementedError          # {'limit':int|None,'usage':int}

    # ---- comum --------------------------------------------------------------
    def free_bytes(self) -> int | None:
        q = self.quota()
        return None if not q.get("limit") else q["limit"] - q["usage"]

    def has_space(self, need: int) -> bool:
        f = self.free_bytes()
        return f is None or f - need >= self.min_free_bytes

    def upload(self, local: Path, path: str, *, expect_sha256: str | None = None) -> dict:
        """Idempotente: se já existe com o mesmo MD5, não reenvia. Verifica MD5 do que o Drive guardou."""
        local = Path(local)
        size = local.stat().st_size
        md5 = md5_file(local)
        if expect_sha256 and sha256_file(local) != expect_sha256:
            raise IntegrityError(f"{local.name}: sha256 local difere do esperado (arquivo corrompido antes do envio)")
        cur = self.stat(path)
        if cur and cur.get("md5") == md5 and cur.get("size") == size:
            return cur | {"reused": True}
        if cur:
            raise IntegrityError(f"{path} já existe com conteúdo diferente (sem sobrescrita; use outro nome/versão)")
        if not self.has_space(size):
            raise DriveFull(f"Drive sem espaço para {size / 1e6:.0f} MB (livre {self.free_bytes()})", retry_after=6 * 3600)
        info = self._put(local, path, md5)
        if info.get("md5") != md5 or int(info.get("size", -1)) != size:
            raise TransientError(f"verificação pós-upload falhou para {path} (md5/tamanho divergem)")
        return info | {"reused": False}

    def download(self, path: str, dest: Path, *, md5: str | None = None, sha256: str | None = None) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        info = self.download_raw(path, part)
        want_md5 = md5 or info.get("md5")
        if want_md5 and md5_file(part) != want_md5:
            part.unlink(missing_ok=True)
            raise TransientError(f"download de {path} corrompido (md5)")
        if sha256 and sha256_file(part) != sha256:
            part.unlink(missing_ok=True)
            raise IntegrityError(f"download de {path}: sha256 diferente do registrado")
        os.replace(part, dest)
        return dest

    def read_json(self, path: str) -> dict | None:
        if not self.stat(path):
            return None
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"xh-rj-{os.getpid()}-{threading.get_ident()}.json"
        try:
            self.download(path, tmp)
            return json.loads(tmp.read_text())
        finally:
            tmp.unlink(missing_ok=True)

    def write_json(self, path: str, data: dict) -> dict:
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"xh-wj-{os.getpid()}-{threading.get_ident()}.json"
        tmp.write_text(json.dumps(data, sort_keys=True))
        try:
            return self.upload(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def delete(self, path: str) -> None:
        if not path.startswith(DELETABLE):
            raise PermissionError(f"recusado: {path} não está em prefixo descartável {DELETABLE}")
        self._delete(path)


# =============================================================== diretório local (SIMULAÇÃO de Drive)
class LocalDirBackend(DriveBackend):
    """Usa uma pasta como se fosse o Drive. Serve a testes e a modo offline; NÃO é o Drive real.
    Permite injetar falhas (fail_next_uploads) e limite de espaço (capacity)."""

    def __init__(self, root: Path, capacity: int | None = None):
        self.root = Path(root)
        self.capacity = capacity
        self.fail_next_uploads = 0
        self.corrupt_next_upload = False
        self.lock = threading.Lock()

    def _p(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise PermissionError("caminho fora da raiz")
        return p

    def ensure_layout(self) -> None:
        for d in LAYOUT:
            self._p(d).mkdir(parents=True, exist_ok=True)

    def stat(self, path):
        p = self._p(path)
        if not p.is_file():
            return None
        return {"path": path, "id": path, "size": p.stat().st_size, "md5": md5_file(p), "mtime": p.stat().st_mtime}

    def _put(self, local, path, md5):
        with self.lock:
            if self.fail_next_uploads > 0:
                self.fail_next_uploads -= 1
                raise TransientError("falha simulada de upload")
            p = self._p(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            part = p.with_suffix(p.suffix + ".uploading")
            shutil.copyfile(local, part)
            if self.corrupt_next_upload:
                self.corrupt_next_upload = False
                with open(part, "ab") as f:
                    f.write(b"\0")
            os.replace(part, p)
            return self.stat(path)

    def download_raw(self, path, dest):
        s = self.stat(path)
        if not s:
            raise PermanentError(f"{path} não existe no Drive")
        shutil.copyfile(self._p(path), dest)
        return s

    def listdir(self, prefix):
        base = self._p(prefix) if prefix else self.root
        out = []
        if base.is_dir():
            for f in sorted(base.rglob("*")):
                if f.is_file() and not f.name.endswith((".uploading", ".part")):
                    out.append({"path": str(f.relative_to(self.root)), "size": f.stat().st_size, "mtime": f.stat().st_mtime})
        return out

    def _delete(self, path):
        self._p(path).unlink(missing_ok=True)

    def quota(self):
        used = sum(f["size"] for f in self.listdir(""))
        return {"limit": self.capacity, "usage": used}


# =============================================================== Google Drive real (API v3)
class GoogleDriveBackend(DriveBackend):
    """Cliente do Drive v3 por OAuth de usuário (refresh token, escopo drive.file).
    Conta de serviço NÃO serve: não tem cota de armazenamento própria em Meu Drive."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, root_name: str = ROOT,
                 api: str = "https://www.googleapis.com", token_url: str = "https://oauth2.googleapis.com/token",
                 chunk: int = CHUNK, limiter: RateLimiter | None = None):
        self.cid, self.cs, self.rt = client_id, client_secret, refresh_token
        self.api, self.token_url, self.chunk = api.rstrip("/"), token_url, chunk
        self.root_name = root_name
        self.limiter = limiter or RateLimiter(8.0, 16)          # ~8 req/s, bem abaixo das cotas por minuto
        self._tok, self._exp = "", 0.0
        self._ids: dict[str, str] = {}
        self.s = requests.Session()

    # auth ------------------------------------------------------------------
    def _token(self) -> str:
        if self._tok and time.time() < self._exp - 60:
            return self._tok
        r = self.s.post(self.token_url, data={"client_id": self.cid, "client_secret": self.cs, "refresh_token": self.rt,
                                              "grant_type": "refresh_token"}, timeout=30)
        if r.status_code in (400, 401):
            raise PermanentError("refresh token do Google inválido/expirado (app OAuth em modo Teste? refaça a autorização): "
                                 + r.text[:120])
        if r.status_code >= 500 or r.status_code == 429:
            raise TransientError(f"oauth {r.status_code}")
        self._tok = r.json()["access_token"]
        self._exp = time.time() + int(r.json().get("expires_in", 3600))
        return self._tok

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        def go():
            self.limiter.wait()
            h = dict(kw.get("headers") or {})
            h["Authorization"] = "Bearer " + self._token()
            k = {**kw, "headers": h, "timeout": kw.get("timeout", 120)}
            try:
                r = self.s.request(method, url, **k)
            except requests.RequestException as e:
                raise TransientError(f"rede: {e}")
            if r.status_code == 401:
                self._tok = ""
                raise TransientError("token expirado")
            if r.status_code == 403:
                body = r.text
                if "storageQuotaExceeded" in body:
                    raise DriveFull("Drive cheio (storageQuotaExceeded)", retry_after=6 * 3600)
                if "rateLimitExceeded" in body or "userRateLimitExceeded" in body:
                    raise TransientError("Drive rate limit", retry_after=30)
                if "dailyLimitExceeded" in body or "quotaExceeded" in body:
                    raise QuotaExceeded("cota diária do Drive", retry_after=3 * 3600)
                raise PermanentError("Drive 403: " + body[:150])
            if r.status_code == 429 or r.status_code >= 500:
                raise TransientError(f"Drive {r.status_code}")
            return r
        return call_with_retry(go, attempts=5, base=1.0, cap=20.0)

    # pastas -----------------------------------------------------------------
    def _q(self, q: str, fields="files(id,name,size,md5Checksum,modifiedTime)") -> list[dict]:
        out, tok = [], None
        while True:
            r = self._req("GET", self.api + "/drive/v3/files", params={"q": q, "fields": f"nextPageToken,{fields}", "pageSize": 200,
                                                                         "pageToken": tok or "", "spaces": "drive"})
            if r.status_code != 200:
                raise TransientError(f"list {r.status_code}")
            j = r.json()
            out += j.get("files", [])
            tok = j.get("nextPageToken")
            if not tok:
                return out

    def _folder(self, parts: list[str], create=True) -> str | None:
        parent = "root"
        key = ""
        for name in [self.root_name, *parts]:
            key += "/" + name
            if key in self._ids:
                parent = self._ids[key]
                continue
            q = f"name='{name}' and '{parent}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            found = self._q(q)
            if found:
                fid = sorted(found, key=lambda f: f.get("modifiedTime", ""))[0]["id"]       # duplicata concorrente: usa a mais antiga
            elif create:
                r = self._req("POST", self.api + "/drive/v3/files", params={"fields": "id"},
                              json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]})
                fid = r.json()["id"]
            else:
                return None
            self._ids[key] = fid
            parent = fid
        return parent

    def ensure_layout(self) -> None:
        for d in LAYOUT:
            self._folder(d.split("/"))

    def _split(self, path: str) -> tuple[list[str], str]:
        parts = [p for p in path.split("/") if p]
        return parts[:-1], parts[-1]

    def stat(self, path):
        d, name = self._split(path)
        fid = self._folder(d, create=False)
        if not fid:
            return None
        fs = self._q(f"name='{name}' and '{fid}' in parents and trashed=false")
        if not fs:
            return None
        f = sorted(fs, key=lambda f: f.get("modifiedTime", ""))[0]
        return {"path": path, "id": f["id"], "size": int(f.get("size", 0)), "md5": f.get("md5Checksum"), "mtime": f.get("modifiedTime")}

    def _put(self, local, path, md5):
        d, name = self._split(path)
        parent = self._folder(d)
        size = local.stat().st_size
        meta = {"name": name, "parents": [parent], "appProperties": {"md5": md5}}
        fields = "id,name,size,md5Checksum"
        if size == 0:
            r = self._req("POST", self.api + "/drive/v3/files", params={"fields": fields}, json=meta)
            j = r.json()
            return {"path": path, "id": j["id"], "size": 0, "md5": j.get("md5Checksum") or md5}
        r = self._req("POST", self.api + "/upload/drive/v3/files", params={"uploadType": "resumable", "fields": fields}, json=meta,
                      headers={"X-Upload-Content-Length": str(size)})
        if r.status_code != 200 or "Location" not in r.headers:
            raise TransientError(f"início do upload falhou {r.status_code}")
        sess = r.headers["Location"]
        offset, fails = 0, 0
        with open(local, "rb") as f:
            while True:
                f.seek(offset)
                data = f.read(self.chunk)
                end = offset + len(data) - 1
                try:
                    rr = self._req("PUT", sess, data=data, headers={"Content-Range": f"bytes {offset}-{end}/{size}"}, timeout=300)
                except TransientError:
                    fails += 1
                    if fails > 6:
                        raise
                    rr = self._req("PUT", sess, data=b"", headers={"Content-Range": f"bytes */{size}"})   # pergunta quanto chegou
                if rr.status_code in (200, 201):
                    j = rr.json()
                    return {"path": path, "id": j["id"], "size": int(j.get("size", size)), "md5": j.get("md5Checksum")}
                if rr.status_code == 308:
                    rng = rr.headers.get("Range")
                    offset = int(rng.split("-")[1]) + 1 if rng else 0
                    continue
                raise TransientError(f"upload chunk {rr.status_code}")

    def download_raw(self, path, dest):
        s = self.stat(path)
        if not s:
            raise PermanentError(f"{path} não existe no Drive")
        r = self._req("GET", f"{self.api}/drive/v3/files/{s['id']}", params={"alt": "media"}, stream=True, timeout=300)
        if r.status_code != 200:
            raise TransientError(f"download {r.status_code}")
        with open(dest, "wb") as f:
            for b in r.iter_content(1 << 20):
                f.write(b)
        return s

    def listdir(self, prefix):
        d = [p for p in prefix.split("/") if p]
        fid = self._folder(d, create=False)
        if not fid:
            return []
        return [{"path": f"{prefix.rstrip('/')}/{f['name']}".lstrip("/"), "id": f["id"], "size": int(f.get("size", 0)),
                 "mtime": f.get("modifiedTime")} for f in self._q(f"'{fid}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'")]

    def _delete(self, path):
        s = self.stat(path)
        if s:
            self._req("PATCH", f"{self.api}/drive/v3/files/{s['id']}", json={"trashed": True})     # lixeira, recuperável

    def quota(self):
        r = self._req("GET", self.api + "/drive/v3/about", params={"fields": "storageQuota"})
        q = r.json().get("storageQuota", {})
        return {"limit": int(q["limit"]) if q.get("limit") else None, "usage": int(q.get("usage", 0))}
