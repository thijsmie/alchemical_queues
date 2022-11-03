from datetime import datetime, timedelta
import time
from alchemical_queues import AlchemicalQueues, tasks

from mocktasks import increment, fail_once, fail_always


def test_task(queue: AlchemicalQueues):
    q = queue["tasks"]

    v = increment(12).schedule(q)

    tasks.Worker(q).work_one(False)

    assert v.result == 13


def test_task_retry(queue: AlchemicalQueues):
    q = queue["tasks"]

    v = fail_once(12).schedule(q, max_retries=1)

    tasks.Worker(q).work_one(False)  # fail
    tasks.Worker(q).work_one(False)  # success

    assert v.result == 12
    entry = q.fetch(v._entry_id)
    assert entry
    data = entry.data
    assert data['retries'] == 1


def test_task_fail(queue: AlchemicalQueues):
    q = queue["tasks"]

    v = fail_always(12).schedule(q, max_retries=1, retry_in=timedelta(seconds=0.01))

    tasks.Worker(q).work_one(False)  # fail
    time.sleep(0.1)
    tasks.Worker(q).work_one(False)  # fail
    time.sleep(0.1)

    assert v.result is None
    entry = q.fetch(v._entry_id)
    assert entry
    data = entry.data
    assert data['retries'] == 1
