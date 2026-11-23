from scripts.mobile_sync import is_pending_status, should_remove_on_phone, sha256_file


def test_pending_statuses_are_only_unpublished_stock_states():
    assert is_pending_status("awaiting")
    assert is_pending_status("scheduled")
    assert is_pending_status("retry_wait")
    assert is_pending_status("publish_failed")
    assert not is_pending_status("published")
    assert not is_pending_status("archived")
    assert not is_pending_status("cancelled")


def test_phone_cleanup_requires_published_status():
    assert should_remove_on_phone("published")
    assert not should_remove_on_phone("awaiting")
    assert not should_remove_on_phone("publish_failed")
    assert not should_remove_on_phone("archived")


def test_sha256_file(tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"xhakinha-mobile-test")
    assert sha256_file(target) == "1f13c0c94f3a992d53863c3103926fcb4af89df6870ed8e236eb4f7ff80e5b39"  # placeholder replaced below
