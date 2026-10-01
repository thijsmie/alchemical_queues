"""Multi-threaded stress tests for the claim/visibility-timeout mechanics
against real SQLite, covering the scenario a short, single-threaded test
can't: concurrent workers racing claims, timeouts, and redelivery.

These found a real bug during development (a stale, "zombie" claim holder
could still successfully respond() after its claim had already expired and
been redelivered to, and finished by, a second worker -- see the
claim_token fencing in AlchemicalQueue.release()/discard()/extend() and the
changelog) and are kept here, not just as throwaway scripts, so a regression
would be caught again.
"""

import random
import threading
import time
from collections import Counter
from datetime import timedelta
from typing import Callable

from alchemical_queues import AlchemicalQueues, ClaimExpired
from alchemical_queues.tasks import Worker

from .mocktasks import slow_task


def test_chaos_claims_never_lose_or_duplicate(queue_factory):
    """Many worker threads hammer one queue with a chaotic mix of
    successful (discard-then-respond, the correct order), released,
    discarded (give up on purpose), and abandoned ("crashed") claims, with a
    short visibility_timeout so redelivery happens constantly. Afterwards,
    every originally-put entry must have been either explicitly discarded
    (on purpose) or responded to successfully *exactly once* -- never zero
    times (lost) and never more than once (duplicated).
    """

    num_tasks = 150
    num_workers = 8
    run_seconds = 4.0
    visibility_timeout = timedelta(milliseconds=60)

    seed_q: AlchemicalQueues = queue_factory()
    q = seed_q.get("chaos", visibility_timeout=visibility_timeout)
    seeded_ids = [q.put(i).entry_id for i in range(num_tasks)]

    success_counts: Counter = Counter()
    discarded_ids: set = set()
    action_counts: Counter = Counter()
    errors = []
    lock = threading.Lock()
    stop_at = time.time() + run_seconds

    def worker(worker_id: int, queue_factory_fn: Callable[[], AlchemicalQueues]):
        try:
            wq = queue_factory_fn().get("chaos", visibility_timeout=visibility_timeout)
            rng = random.Random(worker_id * 7919 + 13)
            while time.time() < stop_at:
                entry = wq.get()
                if entry is None:
                    time.sleep(0.003)
                    continue

                assert entry.claim_token is not None
                roll = rng.random()
                try:
                    if roll < 0.4:
                        # Correct order: prove we still hold the claim
                        # (discard) *before* filing a response -- the same
                        # order tasks.Worker uses, and the fix for the bug
                        # this test is named after.
                        wq.discard(entry.entry_id, entry.claim_token)
                        wq.respond(entry.entry_id, "ok")
                        with lock:
                            success_counts[entry.entry_id] += 1
                            action_counts["respond"] += 1
                    elif roll < 0.6:
                        wq.release(entry.entry_id, entry.claim_token)
                        with lock:
                            action_counts["release"] += 1
                    elif roll < 0.8:
                        wq.discard(entry.entry_id, entry.claim_token)
                        with lock:
                            discarded_ids.add(entry.entry_id)
                            action_counts["discard"] += 1
                    else:
                        with lock:
                            action_counts["crash"] += 1
                        # deliberately do nothing: simulate a dead worker
                except ClaimExpired:
                    # Can legitimately happen if our claim lapsed between
                    # get() and here under heavy contention.
                    with lock:
                        action_counts["claim_expired_race"] += 1
        except Exception as exc:  # pylint: disable=broad-except
            with lock:
                errors.append((worker_id, repr(exc)))

    threads = [
        threading.Thread(target=worker, args=(i, queue_factory))
        for i in range(num_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Drain phase: finish off anything left (claimed-but-abandoned, or just
    # unlucky) deterministically, using the same discard-then-respond order.
    drain_q = queue_factory().get("chaos", visibility_timeout=visibility_timeout)
    drain_deadline = time.time() + visibility_timeout.total_seconds() * 50 + 5.0
    while drain_q.qsize() > 0 and time.time() < drain_deadline:
        entry = drain_q.get()
        if entry is None:
            time.sleep(visibility_timeout.total_seconds())
            continue
        assert entry.claim_token is not None
        drain_q.discard(entry.entry_id, entry.claim_token)
        drain_q.respond(entry.entry_id, "ok (drained)")
        success_counts[entry.entry_id] += 1

    assert not errors, f"worker threads raised: {errors}"

    missing = [
        i for i in seeded_ids if success_counts[i] == 0 and i not in discarded_ids
    ]
    duplicated = {i: c for i, c in success_counts.items() if c > 1}

    assert not missing, f"{len(missing)} tasks were lost: {missing[:10]}"
    assert (
        not duplicated
    ), f"{len(duplicated)} tasks got duplicate responses: {duplicated}"
    assert drain_q.qsize() == 0


def test_long_running_task_without_keepalive_discards_stale_result(queue_factory):
    """Without a keepalive, a task slower than visibility_timeout can be
    redelivered and finished by a second worker while the first is still
    running it. Both run the handler, but only one response is ever
    recorded: the fencing in Worker._perform() makes the late worker's
    discard() raise ClaimExpired, so it logs a warning and discards its own
    result instead of risking a duplicate.
    """

    aq: AlchemicalQueues = queue_factory()
    q = aq.get("slow", visibility_timeout=timedelta(milliseconds=150))
    entry = slow_task(0.4).schedule(q)

    worker_a_done = threading.Event()

    def run_worker_a():
        Worker(aq.get("slow"), poll_every=timedelta(milliseconds=20)).work_one(
            block=True
        )
        worker_a_done.set()

    thread_a = threading.Thread(target=run_worker_a)
    thread_a.start()
    time.sleep(0.03)  # let worker A claim it first

    # Worker B polls aggressively and redelivers the instant the claim lapses.
    stop_b = threading.Event()

    def run_worker_b():
        wb = Worker(queue_factory().get("slow"), poll_every=timedelta(milliseconds=15))
        while not stop_b.is_set():
            wb.work_one(block=False)
            time.sleep(0.015)

    thread_b = threading.Thread(target=run_worker_b)
    thread_b.start()

    thread_a.join(timeout=5)
    stop_b.set()
    thread_b.join(timeout=5)

    responses = q.responses(entry.entry_id)
    assert (
        len(responses) == 1
    ), f"expected exactly one response despite redelivery, got {len(responses)}"


def test_long_running_task_with_keepalive_prevents_redelivery(queue_factory):
    """With keepalive_every set, Worker extends the claim while the handler
    runs, so a slower vulture worker never gets a chance to redeliver it at
    all -- the task runs exactly once.
    """

    aq: AlchemicalQueues = queue_factory()
    q = aq.get("slow", visibility_timeout=timedelta(milliseconds=150))
    entry = slow_task(0.4).schedule(q)

    run_count = 0
    run_lock = threading.Lock()

    def run_worker_a():
        nonlocal run_count
        w = Worker(
            aq.get("slow"),
            poll_every=timedelta(milliseconds=20),
            keepalive_every=timedelta(milliseconds=40),
        )
        w.work_one(block=True)
        with run_lock:
            run_count += 1

    thread_a = threading.Thread(target=run_worker_a)
    thread_a.start()
    time.sleep(0.03)

    stop_b = threading.Event()
    redelivered = threading.Event()

    def run_worker_b():
        wb = Worker(queue_factory().get("slow"), poll_every=timedelta(milliseconds=15))
        while not stop_b.is_set():
            claimed = wb.queue.get()
            if claimed is not None:
                redelivered.set()
                wb._perform(claimed)  # pylint: disable=protected-access
            time.sleep(0.015)

    thread_b = threading.Thread(target=run_worker_b)
    thread_b.start()

    thread_a.join(timeout=5)
    stop_b.set()
    thread_b.join(timeout=5)

    assert not redelivered.is_set(), "keepalive should have prevented redelivery"
    responses = q.responses(entry.entry_id)
    assert len(responses) == 1
