import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy.engine import Engine

from alchemical_queues import AlchemicalQueues
from alchemical_queues.tasks.periodic import Beat, periodic

from .mocktasks import increment


@pytest.fixture(autouse=True)
def _clear_schedules(engine: Engine, queue: AlchemicalQueues):
    # `run_around_tests` (conftest.py) clears the core queue/response
    # tables after each test, but knows nothing about Beat's own schedule
    # table -- against a real shared database (CI's --engine for
    # MySQL/MariaDB/MSSQL/Postgres, as opposed to the per-test tmpdir
    # SQLite file used otherwise), every test in this file reuses the same
    # schedule names, so a row left behind by one test is still there,
    # already past its first `every`, when the next test looks it up.
    yield
    Beat(engine, queue.get_task_queue("periodic"), []).clear()


def test_periodic_first_tick_enqueues_immediately(
    engine: Engine, queue: AlchemicalQueues
):
    q = queue.get_task_queue("periodic")
    schedule = periodic(increment, name="inc", every=timedelta(hours=1))(1)
    beat = Beat(engine, q, [schedule])
    beat.create_all()

    assert beat.tick() == 1
    assert beat.tick() == 0
    assert q.qsize() == 1


def test_periodic_waits_out_the_interval(engine: Engine, queue: AlchemicalQueues):
    q = queue.get_task_queue("periodic")
    schedule = periodic(increment, name="inc", every=timedelta(milliseconds=50))(1)
    beat = Beat(engine, q, [schedule])
    beat.create_all()

    assert beat.tick() == 1
    assert beat.tick() == 0

    time.sleep(0.06)
    assert beat.tick() == 1


def test_periodic_start_at_defers_the_first_run(
    engine: Engine, queue: AlchemicalQueues
):
    q = queue.get_task_queue("periodic")
    future = datetime.now() + timedelta(milliseconds=100)
    schedule = periodic(
        increment, name="inc", every=timedelta(hours=1), start_at=future
    )(1)
    beat = Beat(engine, q, [schedule])
    beat.create_all()

    assert beat.tick() == 0
    assert q.qsize() == 0

    time.sleep(0.12)
    assert beat.tick() == 1


def test_periodic_start_at_in_the_past_is_due_immediately(
    engine: Engine, queue: AlchemicalQueues
):
    q = queue.get_task_queue("periodic")
    past = datetime.now() - timedelta(days=1)
    schedule = periodic(increment, name="inc", every=timedelta(hours=1), start_at=past)(
        1
    )
    beat = Beat(engine, q, [schedule])
    beat.create_all()

    assert beat.tick() == 1


def test_periodic_runs_stay_anchored_to_start_at_instead_of_drifting(
    engine: Engine, queue: AlchemicalQueues
):
    # Five grid steps are already due by the time Beat first sees this
    # schedule. If next_run advanced from "now" (drifting later with every
    # tick/run instead of staying on the start_at grid), only one of those
    # five would ever fire -- each tick would push next_run a fresh `every`
    # past whatever "now" happened to be, jumping straight to the front of
    # the queue instead of catching up. Anchored to start_at, each tick
    # instead advances by exactly one `every` from the schedule's own
    # previous next_run, so all five missed steps are eventually enqueued,
    # one per tick, regardless of how long ticking was paused.
    # `every` is chosen far larger than this test's own runtime. start_at
    # is set so exactly 4 grid steps (start_at, +every, +2*every, +3*every)
    # are due now, with half a second of margin before the 5th (start_at +
    # 4*every, just in the future) would be -- comfortably more than this
    # test can take to run its 10 ticks.
    q = queue.get_task_queue("periodic")
    every = timedelta(seconds=10)
    start = datetime.now() - every * 4 + timedelta(milliseconds=500)
    schedule = periodic(increment, name="inc", every=every, start_at=start)(1)
    beat = Beat(engine, q, [schedule])
    beat.create_all()

    enqueued = sum(beat.tick() for _ in range(10))

    assert enqueued == 4
    assert q.qsize() == 4


def test_periodic_concurrent_beats_never_double_enqueue(
    engine: Engine, queue: AlchemicalQueues
):
    q = queue.get_task_queue("periodic")
    schedule = periodic(increment, name="inc", every=timedelta(milliseconds=50))(1)
    beat_a = Beat(engine, q, [schedule])
    beat_a.create_all()
    beat_b = Beat(engine, q, [schedule])

    # Two Beat processes racing the same due tick -- exactly one should win.
    results = sorted([beat_a.tick(), beat_b.tick()])
    assert results == [0, 1]

    time.sleep(0.06)
    results = sorted([beat_a.tick(), beat_b.tick()])
    assert results == [0, 1]

    assert q.qsize() == 2
