import pytest
import time
from datetime import datetime, timedelta
from alchemical_queues import AlchemicalQueues


def test_get_noblock(queue: AlchemicalQueues):
    q = queue["test"]
    assert q.get() == None
    q.put(1)
    entry = q.get()
    assert entry and entry.data == 1
    assert q.get() == None


def test_get_scheduled(queue: AlchemicalQueues):
    q = queue["test"]
    t1 = datetime.now()
    t2 = t1 + timedelta(seconds=0.5)

    q.put(1, schedule_at=t2)

    assert q.get() is None, f"Scheduled at {t2} but it is now {datetime.now()}."

    time.sleep(0.6)

    entry = q.get()
    assert entry and entry.data == 1


def test_get_expected(queue: AlchemicalQueues):
    q = queue["test"]
    t1 = datetime.now()
    t2 = t1 + timedelta(seconds=12)

    q.put(1, expected_at=t2)
    q.put(2, expected_at=t2)
    q.put(3, expected_at=t1)
    q.put(4)

    e1 = q.get()
    e2 = q.get()
    e3 = q.get()
    e4 = q.get()

    assert q.get() is None

    assert e1 and e1.data == 4
    assert e2 and e2.data == 3
    assert e3 and e3.data == 1
    assert e4 and e4.data == 2
