"""Tests for the claim/visibility-timeout model: get()/release()/discard()/
extend(), and the claim_token fencing that keeps a stale claim holder from
corrupting a later one."""

import time
from datetime import timedelta
import pytest
from alchemical_queues import AlchemicalQueues, ClaimExpired


def test_get_claims_does_not_delete(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)

    entry = q.get()
    assert entry is not None
    assert entry.claim_token is not None

    # The entry is claimed, not gone -- a second get() can't see it...
    assert q.get() is None
    # ...but it's still sitting in the queue as outstanding work.
    assert q.qsize() == 1


def test_put_entries_have_no_claim_token(queue: AlchemicalQueues):
    q = queue.get("test")
    entry = q.put(1)
    assert entry.claim_token is None


def test_release_frees_a_claimed_entry_for_immediate_reclaim(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put("important")

    entry = q.get()
    assert entry is not None

    q.release(entry.entry_id, entry.claim_token)

    # Still in the queue -- release() puts it back, it doesn't discard it.
    assert q.qsize() == 1

    reclaimed = q.get()
    assert reclaimed is not None
    assert reclaimed.entry_id == entry.entry_id
    assert reclaimed.data == "important"
    # A fresh claim, not the old one.
    assert reclaimed.claim_token != entry.claim_token


def test_release_with_wrong_claim_token_raises(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)
    entry = q.get()
    assert entry is not None

    with pytest.raises(ClaimExpired):
        q.release(entry.entry_id, entry.claim_token + 1)

    # The real claim is untouched.
    assert q.qsize() == 1
    assert q.get() is None


def test_release_on_unclaimed_entry_raises(queue: AlchemicalQueues):
    q = queue.get("test")
    with pytest.raises(ClaimExpired):
        q.release(12345, 999)


def test_discard_removes_a_claimed_entry_entirely(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)

    entry = q.get()
    assert entry is not None

    q.discard(entry.entry_id, entry.claim_token)

    assert q.qsize() == 0
    assert q.get() is None


def test_discard_with_wrong_claim_token_raises(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)
    entry = q.get()
    assert entry is not None

    with pytest.raises(ClaimExpired):
        q.discard(entry.entry_id, entry.claim_token + 1)

    assert q.qsize() == 1


def test_discard_on_unclaimed_entry_raises(queue: AlchemicalQueues):
    q = queue.get("test")
    with pytest.raises(ClaimExpired):
        q.discard(12345, 999)


def test_respond_does_not_touch_the_entry(queue: AlchemicalQueues):
    # respond() only files a response; it's unaware of claims entirely.
    q = queue.get("test")
    q.put(1)

    entry = q.get()
    assert entry is not None
    q.respond(entry.entry_id, "answer")

    assert q.qsize() == 1  # still claimed -- respond() didn't discard it
    assert q.responses(entry.entry_id)[0].data == "answer"

    q.discard(entry.entry_id, entry.claim_token)
    assert q.qsize() == 0


def test_redelivery_after_visibility_timeout(queue_factory):
    # Needs its own queue instance so we control visibility_timeout.
    aq: AlchemicalQueues = queue_factory()
    q = aq.get("test", visibility_timeout=timedelta(milliseconds=50))

    q.put("do not lose me")
    first = q.get()
    assert first is not None

    # Simulate a worker crashing: never respond()/release()/discard().
    assert q.get() is None  # still claimed, not yet redeliverable

    time.sleep(0.1)

    redelivered = q.get()
    assert redelivered is not None
    assert redelivered.data == "do not lose me"
    assert redelivered.entry_id == first.entry_id
    assert redelivered.claim_token != first.claim_token

    # The original (now-stale) claim_token no longer works.
    with pytest.raises(ClaimExpired):
        q.discard(first.entry_id, first.claim_token)


def test_get_accepts_a_per_call_visibility_timeout_override(queue: AlchemicalQueues):
    q = queue.get("test")  # default (long) visibility_timeout
    q.put(1)

    entry = q.get(visibility_timeout=timedelta(milliseconds=50))
    assert entry is not None

    time.sleep(0.1)

    redelivered = q.get()
    assert redelivered is not None
    assert redelivered.entry_id == entry.entry_id


def test_extend_keeps_a_claim_alive_past_its_original_timeout(queue_factory):
    aq: AlchemicalQueues = queue_factory()
    q = aq.get("test", visibility_timeout=timedelta(milliseconds=80))

    q.put("slow task")
    entry = q.get()
    assert entry is not None

    # Without extending, this sleep would outlive the claim.
    time.sleep(0.05)
    q.extend(entry.entry_id, entry.claim_token)
    time.sleep(0.05)
    q.extend(entry.entry_id, entry.claim_token)
    time.sleep(0.05)

    # Still ours: nobody else could have claimed it.
    assert q.get() is None
    q.discard(entry.entry_id, entry.claim_token)


def test_extend_on_an_expired_claim_raises(queue_factory):
    aq: AlchemicalQueues = queue_factory()
    q = aq.get("test", visibility_timeout=timedelta(milliseconds=30))

    q.put(1)
    entry = q.get()
    assert entry is not None

    time.sleep(0.05)  # let it expire, and have someone else claim it first
    reclaimed = q.get()
    assert reclaimed is not None
    assert reclaimed.entry_id == entry.entry_id

    with pytest.raises(ClaimExpired):
        q.extend(entry.entry_id, entry.claim_token)


def test_extend_past_its_own_timeout_self_heals_if_nobody_else_claimed_it(
    queue_factory,
):
    # extend() is keyed on claim_token, not on claimed_until, so calling it a
    # little late still works as long as nobody else grabbed the entry in the
    # meantime -- there's no reason to fail it just because the clock ran out
    # a moment before the call landed.
    aq: AlchemicalQueues = queue_factory()
    q = aq.get("test", visibility_timeout=timedelta(milliseconds=30))

    q.put(1)
    entry = q.get()
    assert entry is not None

    time.sleep(0.05)  # past the original timeout, but nobody else called get()

    q.extend(entry.entry_id, entry.claim_token)  # should not raise
    assert q.get() is None  # still ours
    q.discard(entry.entry_id, entry.claim_token)


def test_extend_accepts_a_custom_duration(queue_factory):
    aq: AlchemicalQueues = queue_factory()
    q = aq.get("test", visibility_timeout=timedelta(milliseconds=30))

    q.put(1)
    entry = q.get()
    assert entry is not None

    q.extend(entry.entry_id, entry.claim_token, by=timedelta(milliseconds=200))
    time.sleep(0.05)  # past the original 30ms, within the extended 200ms

    assert q.get() is None  # still claimed
    q.discard(entry.entry_id, entry.claim_token)
