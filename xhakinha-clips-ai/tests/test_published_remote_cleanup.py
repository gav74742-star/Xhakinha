from types import SimpleNamespace

import pytest

from xhakinha.cloud.brain import Brain
from xhakinha.cloud.drive_storage import LocalDirBackend


class FakeDB:
    def __init__(self, clips, publications, retention_h=0):
        self.clips = clips
        self.publications = publications
        self.retention_h = retention_h
        self.events = []

    def q(self, sql, args=()):
        if "SELECT id,drive_path FROM clips" in sql:
            return [dict(row) for row in self.clips if row.get("drive_path") and row.get("path") is None]
        if "FROM publications WHERE clip_id=?" in sql:
            return [dict(row) for row in self.publications.get(int(args[0]), [])]
        raise AssertionError(f"SQL inesperado no teste: {sql}")

    def get(self, key, default=None):
        return self.retention_h if key == "published_clip_retention_h" else default

    def x(self, sql, args=()):
        now, clip_id, drive_path = args
        for row in self.clips:
            if row["id"] == clip_id and row.get("drive_path") == drive_path:
                row["drive_path"] = None
                row["updated"] = now
        return 0

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))


def test_drive_backend_deletes_only_published_clip_location_and_sidecar(tmp_path):
    drive = LocalDirBackend(tmp_path)
    drive.ensure_layout()
    video = tmp_path / "clips/pending/clip_8_hash.mp4"
    sidecar = tmp_path / "clips/pending/clip_8_hash.json"
    video.write_bytes(b"video")
    sidecar.write_text('{"sha256":"x"}')

    drive.delete_published_clip("clips/pending/clip_8_hash.mp4")

    assert not video.exists()
    assert not sidecar.exists()
    with pytest.raises(PermissionError):
        drive.delete_published_clip("originals/source.mp4")
    with pytest.raises(PermissionError):
        drive.delete_published_clip("clips/pending/../../originals/source.mp4")


def test_brain_deletes_only_fully_published_remote_clips(tmp_path):
    now = 1_000_000.0
    drive = LocalDirBackend(tmp_path)
    drive.ensure_layout()
    clips = []
    for clip_id in (1, 2, 3, 4):
        path = f"clips/pending/clip_{clip_id}_hash.mp4"
        (tmp_path / path).write_bytes(f"video-{clip_id}".encode())
        (tmp_path / path.replace(".mp4", ".json")).write_text("{}")
        clips.append({"id": clip_id, "path": None, "drive_path": path})

    publications = {
        1: [{"status": "published", "published_at": now - 100_000}, {"status": "cancelled", "published_at": None}],
        2: [{"status": "published", "published_at": now - 100_000}, {"status": "publish_failed", "published_at": None}],
        3: [{"status": "cancelled", "published_at": None}],
        4: [{"status": "published", "published_at": now - 3600}],
    }
    db = FakeDB(clips, publications, retention_h=24)
    brain = Brain.__new__(Brain)
    brain.engine = SimpleNamespace(db=db)
    brain.drive = drive
    brain.clock = lambda: now
    brain.report = {"remote_clips_deleted": 0}

    removed = brain.cleanup_published_remote()

    assert removed == 1
    assert clips[0]["drive_path"] is None
    assert clips[1]["drive_path"] is not None
    assert clips[2]["drive_path"] is not None
    assert clips[3]["drive_path"] is not None
    assert not (tmp_path / "clips/pending/clip_1_hash.mp4").exists()
    assert (tmp_path / "clips/pending/clip_2_hash.mp4").exists()
    assert brain.report["remote_clips_deleted"] == 1
