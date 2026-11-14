import json
import time

import pytest

from xhakinha.config import ConfigError, Settings, redact
from xhakinha.queue import JobQueue
from xhakinha.retry import NotReadyYet, PermanentError, TransientError, backoff_delay, call_with_retry
from xhakinha.sources.quota import Quota


def test_weak_or_missing_secret_rejected(tmp_path):
    for bad in ("", "changeme", "short", "a" * 31):
        with pytest.raises(ConfigError):
            Settings(secret_key=bad, data_dir=tmp_path).validate()
    Settings(secret_key="k" * 40, data_dir=tmp_path).validate()


def test_redact_hides_tokens():
    t = redact("Authorization: Bearer abc123SECRETxyz&access_token=ZZZ999token password=hunter22")
    assert "abc123SECRETxyz" not in t and "ZZZ999token" not in t and "hunter22" not in t


def test_backoff_exponential_with_jitter_and_cap():
    d = [backoff_delay(n, 10, 100, 0.0) for n in range(1, 8)]
    assert d == [10, 20, 40, 80, 100, 100, 100]
    lo, hi = backoff_delay(3, 10, 100, 0.3, lambda: 0.0), backoff_delay(3, 10, 100, 0.3, lambda: 1.0)
    assert lo < 40 < hi


def test_call_with_retry_only_transient():
    calls = []
    def f():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("x")
        return "ok"
    assert call_with_retry(f, attempts=4, sleep=lambda s: None) == "ok"
    with pytest.raises(PermanentError):
        call_with_retry(lambda: (_ for _ in ()).throw(PermanentError("p")), sleep=lambda s: None)


def test_queue_transient_backoff_then_dead(engine):
    q = engine.q
    jid = q.enqueue("x", max_attempts=3)
    seen = []
    for i in range(3):
        job = q.claim("w", now=time.time() + 10_000 * i)
        assert job and job["attempts"] == i + 1
        seen.append(q.fail(job, TransientError("net")))
    assert seen == ["pending", "pending", "dead"]
    assert engine.db.one("SELECT status, error_kind FROM jobs WHERE id=?", (jid,))["error_kind"] == "transient_exhausted"


def test_queue_permanent_does_not_waste_retries(engine):
    q = engine.q
    q.enqueue("x")
    job = q.claim("w")
    assert q.fail(job, PermanentError("yt-dlp ausente")) == "dead"
    assert job["attempts"] == 1 and q.claim("w") is None


def test_not_ready_does_not_consume_attempts(engine):
    q = engine.q
    jid = q.enqueue("x", {"deadline": time.time() + 9999}, max_attempts=2)
    for _ in range(5):
        job = q.claim("w", now=time.time() + 100000)
        assert q.fail(job, NotReadyYet("vod", retry_after=1)) == "pending"
    assert engine.db.one("SELECT attempts FROM jobs WHERE id=?", (jid,))["attempts"] <= 1


def test_queue_recovery_after_restart_and_dedupe(engine):
    q = engine.q
    assert q.enqueue("x", dedupe_key="k") and q.enqueue("x", dedupe_key="k") is None
    job = q.claim("w")
    assert q.recover_all_running() == 1          # simula reinício do processo
    assert q.claim("w")["id"] == job["id"]


def test_queue_dead_can_be_retried_by_admin(engine):
    q = engine.q
    jid = q.enqueue("x")
    q.fail(q.claim("w"), PermanentError("p"))
    assert q.retry_dead(jid) and q.claim("w")["id"] == jid


def test_youtube_quota_counter_and_reset_window(engine):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    qu = Quota(engine.db, "youtube", 10)
    now = datetime(2026, 3, 3, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    qu.charge(10, now)
    with pytest.raises(Exception):
        qu.charge(1, now)
    nxt = datetime(2026, 3, 4, 0, 5, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    qu.charge(5, nxt)            # novo dia (PT) -> zera
    assert qu.used(nxt) == 5
