from datetime import datetime
import pytest
from alchemical_queues import AlchemicalQueue, AlchemicalQueues


def test_create_queue(engine):
    aq = AlchemicalQueues(engine=engine)
    aq.create()


def test_create_queue_late_init(engine):
    aq = AlchemicalQueues()
    aq.set_engine(engine)
    aq.create()


def test_put_get_data(queue: AlchemicalQueues):
    q = queue["test"]
    q.put(1)
    job = q.get()

    assert job
    assert repr(job)
    assert job.data == 1


def test_put_get_data_ordered(queue: AlchemicalQueues):
    q = queue["test"]
    q.put(1)
    q.put(2)

    e1 = q.get()
    e2 = q.get()

    assert e1 and e1.data == 1
    assert e2 and e2.data == 2


def test_put_get_no_mix(queue: AlchemicalQueues):
    q1 = queue["test1"]
    q2 = queue["test2"]
    q1.put(1)
    q2.put(2)

    e1 = q2.get()
    e2 = q1.get()

    assert e1 and e1.data == 2
    assert e2 and e2.data == 1


def test_multi_instance(queue: AlchemicalQueues):
    q1 = queue["test"]
    q2 = queue["test"]
    assert q1 is q2


def test_get_dictionary(queue: AlchemicalQueues):
    q = queue["test"]
    q.put({'foo': 'bar'})
    job = q.get()
    assert job and job.data == {'foo': 'bar'}


def test_put_get_clear(queue: AlchemicalQueues):
    q1 = queue["test1"]
    q2 = queue["test2"]
    q1.put(1)
    q2.put(2)
    q1.clear()

    entry = q2.get()

    assert entry and entry.data == 2
    assert q1.get() is None


def test_fetch(queue: AlchemicalQueues):
    q = queue["test"]
    jid = q.put(1)

    entry = q.fetch(jid)
    assert entry and entry.data == 1
    assert q.fetch(0xdeadbeef) is None


def test_fetch_uneditable(queue: AlchemicalQueues):
    q = queue["test"]
    jid = q.put(1)

    with pytest.raises(NotImplementedError):
        entry = q.fetch(jid)
        assert entry
        entry.data = 12
