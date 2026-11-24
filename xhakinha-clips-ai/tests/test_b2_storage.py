from __future__ import annotations

from pathlib import Path

from xhakinha.cloud.b2_storage import B2Backend


class Body:
    def __init__(self, data): self.data = data
    def iter_chunks(self, chunk_size=1024 * 1024):
        for i in range(0, len(self.data), chunk_size): yield self.data[i:i + chunk_size]


class FakeB2:
    def __init__(self):
        self.objects = {}
        self.multipart_uploads = []

    def put_object(self, Bucket, Key, Body, ContentLength, Metadata):
        self.objects[Key] = {"body": Body.read(), "meta": Metadata}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, Config=None):
        with open(Filename, "rb") as source:
            data = source.read()
        metadata = (ExtraArgs or {}).get("Metadata", {})
        self.objects[Key] = {"body": data, "meta": metadata}
        self.multipart_uploads.append(Key)
    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            error = RuntimeError("objeto não encontrado")
            error.response = {
                "Error": {"Code": "404"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }
            raise error
        obj = self.objects[Key]
        return {"ContentLength": len(obj["body"]), "Metadata": obj["meta"]}
    def get_object(self, Bucket, Key): return {"Body": Body(self.objects[Key]["body"])}
    def delete_object(self, Bucket, Key): self.objects.pop(Key, None)
    def get_paginator(self, name):
        owner = self
        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "Size": len(v["body"])} for k, v in owner.objects.items() if k.startswith(Prefix)]}
        return P()


def test_b2_backend_upload_download_quota_and_safe_delete(tmp_path):
    fake = FakeB2()
    backend = B2Backend("https://s3.us-east-005.backblazeb2.com", "us-east-005", "key", "secret", "bucket", max_gb=1, client=fake)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"xhakinha-final-video")
    uploaded = backend.upload(source, "clips/pending/clip_1.mp4")
    assert uploaded["size"] == source.stat().st_size
    assert backend.stat("clips/pending/clip_1.mp4")["md5"] == uploaded["md5"]
    dest = tmp_path / "download.mp4"
    backend.download("clips/pending/clip_1.mp4", dest)
    assert dest.read_bytes() == source.read_bytes()
    assert backend.quota()["usage"] == source.stat().st_size
    sidecar = tmp_path / "clip_1.json"
    sidecar.write_text('{"published": true}')
    backend.upload(sidecar, "clips/pending/clip_1.json")

    backend.delete_published_clip("clips/pending/clip_1.mp4")
    assert backend.stat("clips/pending/clip_1.mp4") is None
    assert backend.stat("clips/pending/clip_1.json") is None

    import pytest
    for invalid in (
        "originals/clip.mp4",
        "clips/other/clip.mp4",
        "clips/pending/sub/clip.mp4",
        "../clips/pending/clip.mp4",
        "clips\\pending\\clip.mp4",
        "/clips/pending/clip.mp4",
        "clips/pending/clip.json",
    ):
        with pytest.raises(PermissionError):
            backend.delete_published_clip(invalid)


def test_b2_backend_uses_multipart_for_large_files(tmp_path, monkeypatch):
    import sys
    import types

    class FakeTransferConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    boto3_module = types.ModuleType("boto3")
    s3_module = types.ModuleType("boto3.s3")
    transfer_module = types.ModuleType("boto3.s3.transfer")
    transfer_module.TransferConfig = FakeTransferConfig

    monkeypatch.setitem(sys.modules, "boto3", boto3_module)
    monkeypatch.setitem(sys.modules, "boto3.s3", s3_module)
    monkeypatch.setitem(sys.modules, "boto3.s3.transfer", transfer_module)

    fake = FakeB2()
    backend = B2Backend(
        "https://s3.us-east-005.backblazeb2.com",
        "us-east-005",
        "key",
        "secret",
        "bucket",
        client=fake,
    )

    # Reduz o limite somente neste teste para não criar um arquivo enorme.
    monkeypatch.setattr(B2Backend, "multipart_threshold", 8)

    source = tmp_path / "large-video.mp4"
    source.write_bytes(b"xhakinha-multipart-test-data")

    uploaded = backend.upload(source, "originals/large-video.mp4")

    assert fake.multipart_uploads == ["originals/large-video.mp4"]
    assert uploaded["size"] == source.stat().st_size
    assert backend.stat("originals/large-video.mp4")["sha256"] == (
        __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    )


def test_b2_backend_only_deletes_processed_original_prefix(tmp_path):
    fake = FakeB2()
    backend = B2Backend("https://s3.us-east-005.backblazeb2.com", "us-east-005", "key", "secret", "bucket", client=fake)
    import pytest
    with pytest.raises(PermissionError): backend.delete_original("clips/pending/not-original.mp4")
    with pytest.raises(PermissionError): backend.delete_original("../outside.mp4")
    source = tmp_path / "original.mp4"
    source.write_bytes(b"processed-original")
    backend.upload(source, "originals/content.mp4")
    with pytest.raises(PermissionError, match="preservados"):
        backend.delete_original("originals/content.mp4")
    assert backend.stat("originals/content.mp4") is not None


def test_b2_rejects_unsafe_keys():
    import pytest

    invalid_keys = (
        "",
        "/clips/pending/a.mp4",
        "../outside.mp4",
        "clips/../outside.mp4",
        "clips/./pending/a.mp4",
        "clips//pending/a.mp4",
        "clips\\\\pending\\\\a.mp4",
        "clips/pending/\x00a.mp4",
    )

    for key in invalid_keys:
        with pytest.raises(PermissionError):
            B2Backend._key(key)
