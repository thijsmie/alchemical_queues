from pydantic import BaseModel

from alchemical_queues import AlchemicalQueues
from alchemical_queues.serializers import (
    JsonSerializer,
    PickleSerializer,
    PydanticSerializer,
)


class Point(BaseModel):
    x: int
    y: int


def test_default_serializer_is_pickle(queue: AlchemicalQueues):
    q = queue.get("test")
    assert isinstance(q._serializer, PickleSerializer)  # pylint: disable=protected-access


def test_pickle_serializer_roundtrip(queue: AlchemicalQueues):
    q = queue.get_serialized("test", PickleSerializer())
    q.put({"a": 1, "b": (1, 2, 3)})
    job = q.get()

    assert job
    assert job.data == {"a": 1, "b": (1, 2, 3)}


def test_json_serializer_roundtrip(queue: AlchemicalQueues):
    q = queue.get_serialized("test", JsonSerializer())
    q.put({"a": 1, "b": [1, 2, 3]})
    job = q.get()

    assert job
    assert job.data == {"a": 1, "b": [1, 2, 3]}


def test_json_serializer_stored_as_json(queue: AlchemicalQueues):
    q = queue.get_serialized("test", JsonSerializer())
    entry = q.put({"a": 1})

    assert entry.data == {"a": 1}


def test_pydantic_serializer_roundtrip(queue: AlchemicalQueues):
    q = queue.get_serialized("test", PydanticSerializer(Point))
    q.put(Point(x=1, y=2))
    job = q.get()

    assert job
    assert isinstance(job.data, Point)
    assert job.data.x == 1
    assert job.data.y == 2


def test_pydantic_serializer_task_queue_roundtrip(queue: AlchemicalQueues):
    tq = queue.get_task_queue_serialized("test", PydanticSerializer(Point))
    tq.put(Point(x=3, y=4))
    job = tq.get()

    assert job
    assert isinstance(job.data, Point)
    assert job.data == Point(x=3, y=4)

    tq.discard(job.entry_id, job.claim_token)


def test_get_typed_with_serializer(queue: AlchemicalQueues):
    q = queue.get_typed("test", Point, serializer=PydanticSerializer(Point))
    q.put(Point(x=5, y=6))
    job = q.get()

    assert job
    assert job.data == Point(x=5, y=6)


def test_task_queue_responds_through_its_serializer(queue: AlchemicalQueues):
    tq = queue.get_task_queue_serialized("test", JsonSerializer())
    entry = tq.put({"payload": 1})
    job = tq.get()
    assert job

    tq.respond(entry.entry_id, {"result": "ok"})
    responses = tq.responses(entry.entry_id)

    assert len(responses) == 1
    assert responses[0].data == {"result": "ok"}
