from datetime import datetime, timedelta
import time
import signal
import time
from threading import Thread
from alchemical_queues import AlchemicalQueues, AlchemicalTaskQueue, tasks

from .mocktasks import increment, fail_once, fail_always, returns_none


def handler(signum, stack):
    raise KeyboardInterrupt


def test_task(queue: AlchemicalQueues):
    q = queue.get_task_queue("tasks")

    v = increment(12).schedule(q)

    assert v.done is False
    assert v.result is None

    tasks.Worker(q).work_one(False)

    assert v.done is True
    assert v.result == 13

    assert increment.retrieve(q, v.entry_id)

    # The claim get() took to run the task should have been released, not
    # left sitting around until its visibility timeout.
    assert q.qsize() == 0


def test_task_done_distinguishes_none_result_from_not_done(queue: AlchemicalQueues):
    q = queue.get_task_queue("tasks")

    v = returns_none().schedule(q)

    assert v.done is False
    assert v.result is None  # not done yet

    tasks.Worker(q).work_one(False)

    assert v.done is True
    assert v.result is None  # done, legitimately returned None


def test_task_retry(queue: AlchemicalQueues):
    q = queue.get_task_queue("tasks")

    v = fail_once(12).schedule(q, max_retries=1)

    tasks.Worker(q).work_one(False)  # fail
    tasks.Worker(q).work_one(False)  # success

    assert v.result == 12

    # The first attempt's claimed entry shouldn't be left claimed forever
    # just because a *different* entry (the retry) is the one that eventually
    # produced the response.
    assert q.qsize() == 0


def test_task_fail(queue: AlchemicalQueues):
    q = queue.get_task_queue("tasks")

    v = fail_always(12).schedule(q, max_retries=1, retry_in=timedelta(seconds=0.01))

    assert v.result is None

    tasks.Worker(q).work_one(False)  # fail
    time.sleep(0.1)
    tasks.Worker(q).work_one(False)  # fail
    time.sleep(0.1)

    assert v.done is True
    assert isinstance(v.result, tasks.TaskException)
    assert v.result.exception_type == "Exception"
    assert v.result.msg == "Always fails"
    assert repr(v.result) == "<Exception: Always fails>"

    # Neither the original claimed entry nor the retried one should be left
    # sitting around claimed forever.
    assert q.qsize() == 0


def test_task_namefail(queue: AlchemicalQueues):
    q = queue.get_task_queue("tasks")

    class A:
        __module__ = "nothing"
        __qualname__ = "nothing"

    fn = tasks.task(A)
    v = fn().schedule(q, max_retries=10)

    tasks.Worker(q).work_one(False)  # fail
    time.sleep(0.1)

    assert isinstance(v.result, tasks.TaskException)


def schedule_something_soon(q: AlchemicalTaskQueue, r: dict):
    time.sleep(1)
    v = increment(12).schedule(q, max_retries=1)
    r["v"] = v


def test_task_work_one_delayed(queue: AlchemicalQueues):
    q = queue.get_task_queue("tasks")
    r = {}
    t = Thread(target=schedule_something_soon, args=(q, r))
    t.start()
    tasks.Worker(q).work_one(True)
    assert r["v"].result == 13


def test_task_work_delayed(queue: AlchemicalQueues):
    q = queue.get_task_queue("tasks")
    r = {}
    t = Thread(target=schedule_something_soon, args=(q, r))
    t.start()
    h = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(3)

    try:
        tasks.Worker(q).work()
    except KeyboardInterrupt:
        assert r["v"].result == 13
    finally:
        signal.signal(signal.SIGALRM, h)
