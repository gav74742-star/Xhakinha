"""Backblaze B2 via API S3 compatível."""
from __future__ import annotations

import os
import time
from pathlib import Path, PurePosixPath

from .drive_storage import DriveBackend, IntegrityError, sha256_file
from ..retry import PermanentError, TransientError


class B2Backend(DriveBackend):
    min_free_bytes = 512 * 1024 * 1024

    def __init__(
        self, endpoint_url: str, region: str, key_id: str,
        application_key: str, bucket: str, *,
        max_gb: float = 8.0, client=None
    ):
        missing = [
            name for name, value in {
                "B2_ENDPOINT_URL": endpoint_url,
                "B2_REGION": region,
                "B2_KEY_ID": key_id,
                "B2_APPLICATION_KEY": application_key,
                "B2_BUCKET": bucket,
            }.items() if not str(value or "").strip()
        ]
        if missing:
            raise ValueError("Configuração B2 ausente: " + ", ".join(missing))
        if not endpoint_url.startswith("https://"):
            raise ValueError("B2_ENDPOINT_URL deve usar HTTPS")
        if not (0.1 <= float(max_gb) <= 9.0):
            raise ValueError("XH_B2_MAX_GB deve ficar entre 0.1 e 9.0 GB")

        self.bucket = bucket.strip()
        self.capacity = int(float(max_gb) * 1_000_000_000)

        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise RuntimeError("boto3 não instalado; atualize requirements") from exc

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url.rstrip("/"),
                region_name=region,
                aws_access_key_id=key_id,
                aws_secret_access_key=application_key,
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 4, "mode": "standard"},
                ),
            )

        self.client = client
        self._quota_cache = None
        self._quota_cached_at = 0.0

    @staticmethod
    def _key(path: str) -> str:
        raw = str(path)
        parts = raw.split("/")
        if (
            not raw
            or "\\" in raw
            or "\x00" in raw
            or raw.startswith("/")
            or any(part in (".", "..") for part in parts)
            or any(part == "" for part in parts[:-1])
        ):
            raise PermissionError(f"chave B2 inválida: {raw}")

        p = PurePosixPath(raw)
        if p.is_absolute() or not p.parts:
            raise PermissionError(f"chave B2 inválida: {raw}")
        return p.as_posix()

    @staticmethod
    def _error_details(exc: Exception) -> tuple[str, int]:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return "", 0
        error = response.get("Error") or {}
        metadata = response.get("ResponseMetadata") or {}
        code = str(error.get("Code", ""))
        try:
            status = int(metadata.get("HTTPStatusCode", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        return code, status

    @staticmethod
    def _raise(exc: Exception, operation: str):
        code, status = B2Backend._error_details(exc)
        if code or status:
            detail = f"B2 {operation}: {code or status}"
            if status >= 500 or status == 429 or code in ("SlowDown", "RequestTimeout"):
                raise TransientError(detail) from exc
            raise PermanentError(detail) from exc
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            raise TransientError(f"B2 {operation}: {exc}") from exc
        raise PermanentError(f"B2 {operation}: {exc}") from exc

    def ensure_layout(self) -> None:
        return None

    def stat(self, path: str) -> dict | None:
        key = self._key(path)
        try:
            h = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            code, status = self._error_details(exc)
            if code in ("404", "NoSuchKey", "NotFound") or status == 404:
                return None
            self._raise(exc, "HEAD")

        meta = h.get("Metadata") or {}
        lm = h.get("LastModified")
        return {
            "path": key,
            "id": key,
            "size": int(h.get("ContentLength", 0)),
            "md5": meta.get("md5") or str(h.get("ETag", "")).strip('"'),
            "sha256": meta.get("sha256", ""),
            "mtime": lm.timestamp() if hasattr(lm, "timestamp") else 0,
        }

    def _put(self, local: Path, path: str, md5: str) -> dict:
        key = self._key(path)
        local = Path(local)
        size = local.stat().st_size
        sha256 = sha256_file(local)
        try:
            with local.open("rb") as body:
                self.client.put_object(
                    Bucket=self.bucket, Key=key, Body=body,
                    ContentLength=size,
                    Metadata={"md5": md5, "sha256": sha256},
                )
        except Exception as exc:
            self._raise(exc, "PUT")

        result = self.stat(key)
        if (
            not result
            or result.get("md5") != md5
            or result.get("sha256") != sha256
            or int(result.get("size", -1)) != size
        ):
            raise TransientError(f"verificação MD5/SHA256/tamanho do B2 falhou: {key}")

        self._quota_cache = None
        return result

    def upload(self, local: Path, path: str, *, expect_sha256: str | None = None) -> dict:
        result = super().upload(local, path, expect_sha256=expect_sha256)
        remote = self.stat(path)
        expected = expect_sha256 or sha256_file(Path(local))
        if not remote or remote.get("sha256") != expected:
            raise IntegrityError(f"{path}: SHA256 remoto ausente ou divergente no B2")
        return result

    def download_raw(self, path: str, dest: Path) -> dict:
        key = self._key(path)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            body = obj["Body"]
            with dest.open("wb") as out:
                if hasattr(body, "iter_chunks"):
                    for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
                else:
                    while True:
                        chunk = body.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                out.flush()
            info = self.stat(key)
            if not info:
                raise TransientError(f"objeto desapareceu durante download: {key}")
            return info
        except Exception as exc:
            if isinstance(exc, (TransientError, PermanentError)):
                raise
            self._raise(exc, "GET")

    def listdir(self, prefix: str) -> list[dict]:
        pref = self._key(prefix).rstrip("/") + "/" if prefix else ""
        out = []
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=pref):
                for item in page.get("Contents", []):
                    lm = item.get("LastModified")
                    out.append({
                        "path": item.get("Key", ""),
                        "size": int(item.get("Size", 0)),
                        "mtime": lm.timestamp() if hasattr(lm, "timestamp") else 0,
                    })
        except Exception as exc:
            self._raise(exc, "LIST")
        return out

    def quota(self) -> dict:
        now = time.time()
        if self._quota_cache is not None and now - self._quota_cached_at < 30:
            return dict(self._quota_cache)
        usage = sum(int(x.get("size", 0)) for x in self.listdir(""))
        self._quota_cache = {"limit": self.capacity, "usage": usage}
        self._quota_cached_at = now
        return dict(self._quota_cache)

    def _delete(self, path: str) -> None:
        key = self._key(path)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            self._quota_cache = None
        except Exception as exc:
            self._raise(exc, "DELETE")

    def delete_published_clip(self, path: str) -> None:
        """Exclui somente a cópia MP4 publicada e seu sidecar em clips/pending."""
        key = self._key(path)
        p = PurePosixPath(key)
        if (
            len(p.parts) != 3
            or p.parts[:2] != ("clips", "pending")
            or p.suffix.lower() != ".mp4"
        ):
            raise PermissionError(
                f"recusado apagar caminho que não seja MP4 em clips/pending: {path}"
            )

        self._delete(key)
        sidecar = p.with_suffix(".json").as_posix()
        if self.stat(sidecar):
            self._delete(sidecar)

    def delete_original(self, path: str) -> None:
        raise PermissionError(
            "exclusão de originais desativada: arquivos-fonte devem ser preservados no B2"
        )
