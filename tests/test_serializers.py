from pydantic import BaseModel

from alchemical_queues import AlchemicalQueues, tasks
from alchemical_queues.serializers import (
    JsonSerializer,
    PickleSerializer,
    PydanticSerializer,
)
from alchemical_queues.tasks import TaskResultSerializer

from .mocktasks import make_point


class Point(BaseModel):
    x: int
    y: int


class Receipt(BaseModel):
    invoice_id: int
    paid: bool


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


def test_entry_and_response_serializers_are_independent(queue: AlchemicalQueues):
    # Different types, different serializers, on the same task queue -- this
    # used to be impossible: respond()/responses() always shared the
    # entry-side serializer, so a PydanticSerializer(Invoice) queue couldn't
    # respond() with anything but an Invoice.
    tq = queue.get_task_queue_serialized(
        "test",
        PydanticSerializer(Point),
        response_serializer=PydanticSerializer(Receipt),
    )

    entry = tq.put(Point(x=1, y=2))
    job = tq.get()
    assert job and isinstance(job.data, Point)

    tq.respond(entry.entry_id, Receipt(invoice_id=42, paid=True))
    responses = tq.responses(entry.entry_id)

    assert len(responses) == 1
    assert isinstance(responses[0].data, Receipt)
    assert responses[0].data == Receipt(invoice_id=42, paid=True)


def test_task_result_serializer_unwraps_success_through_inner_serializer(
    queue: AlchemicalQueues,
):
    # TaskResultSerializer lets a Worker-driven queue's *result* go through
    # a chosen serializer (here PydanticSerializer(Point)) while keeping
    # Worker's own {"result": ...}/{"error": ...} envelope, which
    # QueuedTask.result needs to tell success from failure.
    tq = queue.get_task_queue_serialized(
        "test",
        PickleSerializer(),  # the task envelope itself (function/args/...)
        response_serializer=TaskResultSerializer(PydanticSerializer(Point)),
    )

    handle = make_point(3, 4).schedule(tq)
    tasks.Worker(tq).work_one(False)

    assert handle.done is True
    result = handle.result
    assert isinstance(result, Point)
    assert result == Point(x=3, y=4)


def test_task_result_serializer_keeps_failures_as_task_exception(
    queue: AlchemicalQueues,
):
    from .mocktasks import fail_always

    tq = queue.get_task_queue_serialized(
        "test",
        PickleSerializer(),
        response_serializer=TaskResultSerializer(PydanticSerializer(Point)),
    )

    handle = fail_always(1).schedule(tq)
    tasks.Worker(tq).work_one(False)

    assert handle.done is True
    result = handle.result
    assert isinstance(result, tasks.TaskException)
    assert result.exception_type == "Exception"
