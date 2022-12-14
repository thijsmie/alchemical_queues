import pytest
import time
from datetime import datetime, timedelta
from alchemical_queues import AlchemicalQueues


def test_get_noblock(queue: AlchemicalQueues):
    q = queue.get("test")
    assert q.get() == None
    q.put(1)
    entry = q.get()
    assert entry and entry.data == 1
    assert q.get() == None


def test_get_scheduled(queue: AlchemicalQueues):
    q = queue.get("test")
    t1 = datetime.now()
    t2 = t1 + timedelta(seconds=0.5)

    q.put(1, schedule_at=t2)

    assert q.get() is None, f"Scheduled at {t2} but it is now {datetime.now()}."

    time.sleep(0.6)

    entry = q.get()
    assert entry and entry.data == 1


def test_get_priority(queue: AlchemicalQueues):
    q = queue.get("test")

    q.put(1, priority=2)
    q.put(2, priority=3)
    q.put(3, priority=1)
    q.put(4)

    e1 = q.get()
    e2 = q.get()
    e3 = q.get()
    e4 = q.get()

    assert q.get() is None

    assert e1 and e1.data == 2
    assert e2 and e2.data == 1
    assert e3 and e3.data == 3
    assert e4 and e4.data == 4
