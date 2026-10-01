"""Tests for the claim/visibility-timeout model behind get()/release()/respond()."""

import time
from datetime import timedelta
from alchemical_queues import AlchemicalQueues


def test_get_claims_does_not_delete(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)

    entry = q.get()
    assert entry is not None

    # The entry is claimed, not gone -- a second get() can't see it...
    assert q.get() is None
    # ...but it's still sitting in the queue as outstanding work.
    assert q.qsize() == 1


def test_release_frees_a_claimed_entry(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)

    entry = q.get()
    assert entry is not None

    q.release(entry.entry_id)

    assert q.qsize() == 0
    assert q.get() is None


def test_release_on_unclaimed_entry_is_a_noop(queue: AlchemicalQueues):
    q = queue.get("test")
    # No put() happened; nothing is claimed. Shouldn't raise.
    q.release(12345)


def test_respond_releases_the_claim(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)

    entry = q.get()
    assert entry is not None
    q.respond(entry.entry_id, "answer")

    assert q.qsize() == 0
    assert q.get() is None
    assert q.responses(entry.entry_id)[0].data == "answer"


def test_redelivery_after_visibility_timeout(queue_factory):
    # Needs its own queue instance so we control visibility_timeout.
    aq: AlchemicalQueues = queue_factory()
    q = aq.get("test", visibility_timeout=timedelta(milliseconds=50))

    q.put("do not lose me")
    first = q.get()
    assert first is not None

    # Simulate a worker crashing: never respond()/release().
    assert q.get() is None  # still claimed, not yet redeliverable

    time.sleep(0.1)

    redelivered = q.get()
    assert redelivered is not None
    assert redelivered.data == "do not lose me"
    assert redelivered.entry_id == first.entry_id


def test_get_accepts_a_per_call_visibility_timeout_override(queue: AlchemicalQueues):
    q = queue.get("test")  # default (long) visibility_timeout
    q.put(1)

    entry = q.get(visibility_timeout=timedelta(milliseconds=50))
    assert entry is not None

    time.sleep(0.1)

    redelivered = q.get()
    assert redelivered is not None
    assert redelivered.entry_id == entry.entry_id
