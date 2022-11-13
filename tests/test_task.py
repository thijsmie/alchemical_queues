from datetime import datetime, timedelta
import time
import signal
import time
from threading import Thread
from alchemical_queues import AlchemicalQueues, AlchemicalQueue, tasks

from .mocktasks import increment, fail_once, fail_always


def handler(signum, stack):
    raise KeyboardInterrupt


def test_task(queue: AlchemicalQueues):
    q = queue.get("tasks")

    v = increment(12).schedule(q)

    tasks.Worker(q).work_one(False)

    assert v.result == 13

    assert increment.retrieve(q, v.entry_id)


def test_task_retry(queue: AlchemicalQueues):
    q = queue.get("tasks")

    v = fail_once(12).schedule(q, max_retries=1)

    tasks.Worker(q).work_one(False)  # fail
    tasks.Worker(q).work_one(False)  # success

    assert v.result == 12


def test_task_fail(queue: AlchemicalQueues):
    q = queue.get("tasks")

    v = fail_always(12).schedule(q, max_retries=1, retry_in=timedelta(seconds=0.01))

    assert v.result is None

    tasks.Worker(q).work_one(False)  # fail
    time.sleep(0.1)
    tasks.Worker(q).work_one(False)  # fail
    time.sleep(0.1)

    assert isinstance(v.result, tasks.TaskException)


def test_task_namefail(queue: AlchemicalQueues):
    q = queue.get("tasks")

    class A:
        __module__ = "nothing"
        __qualname__ = "nothing"

    fn = tasks.task(A)
    v = fn().schedule(q, max_retries=10)

    tasks.Worker(q).work_one(False)  # fail
    time.sleep(0.1)

    assert isinstance(v.result, tasks.TaskException)


def schedule_something_soon(q: AlchemicalQueue, r: dict):
    time.sleep(1)
    v = increment(12).schedule(q, max_retries=1)
    r['v'] = v


def test_task_work_one_delayed(queue: AlchemicalQueues):
    q = queue.get("tasks")
    r = {}
    t = Thread(target=schedule_something_soon, args=(q,r))
    t.start()
    tasks.Worker(q).work_one(True)
    assert r['v'].result == 13


def test_task_work_delayed(queue: AlchemicalQueues):
    q = queue.get("tasks")
    r = {}
    t = Thread(target=schedule_something_soon, args=(q,r))
    t.start()
    h = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(3)

    try:
        tasks.Worker(q).work()
    except KeyboardInterrupt:
        assert r['v'].result == 13
    finally:
        signal.signal(signal.SIGALRM, h)
