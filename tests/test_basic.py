import pytest
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from alchemical_queues import AlchemicalQueues


def test_create_queue(engine):
    aq = AlchemicalQueues(engine=engine)
    aq.create_all()


def test_create_queue_late_init(engine):
    aq = AlchemicalQueues()
    aq.set_engine(engine)
    aq.create_all()


def test_no_multiset(engine):
    aq = AlchemicalQueues(engine=engine)

    with pytest.raises(Exception):  # noqa: B017
        aq.set_engine(engine)


def test_no_multiset_2(engine):
    aq = AlchemicalQueues()
    aq.set_engine(engine)

    with pytest.raises(Exception):  # noqa: B017
        aq.set_engine(engine)


def test_no_uninitialized():
    aq = AlchemicalQueues()

    with pytest.raises(Exception):  # noqa: B017
        aq.get("test")


def test_queue_name(queue: AlchemicalQueues):
    q = queue.get("test")
    assert q.name == "test"


def test_put_get_data(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)
    job = q.get()

    assert job
    assert repr(job)
    assert job.data == 1


def test_put_get_data_typed(queue: AlchemicalQueues):
    q = queue.get_typed("test", dict)
    q.put({"1": 1})
    job = q.get()

    assert job
    assert repr(job)
    assert job.data["1"] == 1


def test_put_get_data_ordered(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)
    q.put(2)

    e1 = q.get()
    e2 = q.get()

    assert e1 and e1.data == 1
    assert e2 and e2.data == 2


def test_put_get_no_mix(queue: AlchemicalQueues):
    q1 = queue.get("test1")
    q2 = queue.get("test2")
    q1.put(1)
    q2.put(2)

    e1 = q2.get()
    e2 = q1.get()

    assert e1 and e1.data == 2
    assert e2 and e2.data == 1


def test_multi_instance(queue: AlchemicalQueues):
    q1 = queue.get("test")
    q2 = queue.get("test")
    assert q1 is q2


def test_get_dictionary(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put({"foo": "bar"})
    job = q.get()
    assert job and job.data == {"foo": "bar"}


def test_put_get_clear(queue: AlchemicalQueues):
    q1 = queue.get("test1")
    q2 = queue.get("test2")
    q1.put(1)
    q2.put(2)
    q1.clear()

    entry = q2.get()

    assert entry and entry.data == 2
    assert q1.get() is None


def test_type_errors(queue: AlchemicalQueues):
    q = queue.get_task_queue("test")

    with pytest.raises(TypeError):
        q.respond("a", "b")

    with pytest.raises(TypeError):
        q.responses("a")


def test_queue_size_empty(queue: AlchemicalQueues):
    q = queue.get("test")

    assert q.empty()
    assert q.qsize() == 0

    q.put(1)

    assert not q.empty()
    assert q.qsize() == 1

    entry = q.get()
    assert entry is not None

    # the simple queue's get() removes the entry outright, unlike the task
    # queue's claim-then-discard semantics.
    assert q.empty()
    assert q.qsize() == 0


def test_entry_id_not_reused_after_clear(queue: AlchemicalQueues):
    # Regression test: SQLite reuses a table's rowids once it is emptied unless
    # told not to, which would let a later entry silently collide with an
    # earlier, unrelated one's entry_id.
    q = queue.get("test")

    first = q.put(1)
    q.clear()
    second = q.put(2)

    assert second.entry_id != first.entry_id
    assert second.entry_id > first.entry_id


def test_entry_id_not_reused_after_drain(queue: AlchemicalQueues):
    q = queue.get("test")

    first = q.put(1)
    assert q.get() is not None

    second = q.put(2)

    assert second.entry_id != first.entry_id


def test_custom_declarative_base(engine):
    # AlchemicalQueues can attach its tables to an existing declarative base,
    # so they share a registry/metadata with tables defined elsewhere in the
    # application (e.g. Flask-SQLAlchemy's `db.Model`).
    class AppBase(DeclarativeBase):
        pass

    class Widget(AppBase):
        __tablename__ = "widget"
        id: Mapped[int] = mapped_column(primary_key=True)

    aq = AlchemicalQueues(engine=engine, base=AppBase)
    aq.create_all()

    assert aq._base is AppBase  # pylint: disable=protected-access
    assert "widget" in AppBase.metadata.tables

    q = aq.get("test")
    q.put(1)
    assert q.qsize() == 1
