# Extra functionality

*Alchemical Queues* has a couple extra functionalities that you can combine to build your system.

## Priority queues

In reality, all *Alchemical Queues* are priority queues, just all normal items you insert have priority 0.

```python
queue = queues.get("priority-queue")
queue.put(42, priority=12)
queue.put(137, priority=13)
queue.put(0)

print(queue.get().data)  # prints 137
print(queue.get().data)  # prints 42
print(queue.get().data)  # prints 0
```

This argument also applies to the `schedule` method for tasks.

```python
task_queue = queues.get_task_queue("task-queue")
add_numbers(1,2).schedule(task_queue, priority=12)
```


## Scheduling

When putting things into the queue you can pass a `schedule_at` argument. These entries can not be popped off the queue before the `schedule_at` time.

```python
import time
from datetime import datetime, timedelta

now = datetime.now()
queue = queues.get("schedule-queue")
queue.put(42, schedule_at=now+timedelta(seconds=1))

print(queue.get())  # will print None

time.sleep(1.0)

print(queue.get().data)  # will print 42
```

This argument also applies to the `schedule` method for tasks.

```python
task_queue = queues.get_task_queue("task-queue")
add_numbers(1,2).schedule(task_queue, schedule_at=now+timedelta(seconds=30))
```

## Custom tables

If you don't want to use the default queue and response tables you can configure them.

```python
queues = AlchemicalQueues(
    queue_tablename="queues",
    response_tablename="responses"
)
```

The `alchemical_worker` CLI's plain `alchemical_worker <engine-url> <queue>` form always uses the default table names and no shared base, since it only has an engine URL to go on. To run it against custom table names (or a shared `base=`, see below), use `--import` instead, pointing it at a module-level `AlchemicalQueues` instance in your own code:

```bash
alchemical_worker --import myapp.queues:queues task-queue
```

```python
# myapp/queues.py
from alchemical_queues import AlchemicalQueues

queues = AlchemicalQueues(
    engine,
    queue_tablename="queues",
    response_tablename="responses",
)
```

Or run it via Python yourself, same as with the default tables:

```python
from alchemical_queues.tasks import Worker

Worker(queues.get_task_queue("task-queue")).work()
```

## Sharing a declarative base

If your application already has its own SQLAlchemy `DeclarativeBase` (e.g. `db.Model` from Flask-SQLAlchemy), you can pass it to `AlchemicalQueues` so its tables share that registry and metadata instead of getting their own.

```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

queues = AlchemicalQueues(engine, base=Base)
queues.create_all()  # also creates any other tables defined on Base
```

This is useful when another part of your application already manages migrations or table creation for `Base.metadata`, and you want *Alchemical Queues*'s tables to be created and managed the same way. As above, run `alchemical_worker --import myapp.queues:queues task-queue` against it rather than the plain engine-URL form.

## Serializers

By default, queue entries (and task responses) are serialized with `pickle`, same as always. If you want a different wire format, pass a `Serializer` instance via the `serializer=` argument of `get()`/`get_task_queue()` and friends:

```python
from alchemical_queues.serializers import JsonSerializer

queue = queues.get("json-queue", serializer=JsonSerializer())
queue.put({"a": 1})
```

`JsonSerializer` requires the data to be JSON-serializable. For typed, schema-validated payloads, use `PydanticSerializer` with a [pydantic](https://docs.pydantic.dev/) model (requires the `pydantic` extra: `pip install alchemical_queues[pydantic]`):

```python
from pydantic import BaseModel
from alchemical_queues.serializers import PydanticSerializer

class Job(BaseModel):
    user_id: int
    payload: str

# get_serialized() infers the queue's type from the serializer, so
# entry.data below is typed as Job, no typeof= needed.
queue = queues.get_serialized("job-queue", PydanticSerializer(Job))
queue.put(Job(user_id=1, payload="hello"))

entry = queue.get()
print(entry.data.user_id)  # type-checked as int
```

The same `serializer=`/`get_serialized()` pair is available on `get_task_queue()`/`get_task_queue_serialized()`. A queue's serializer also governs `respond()`/`responses()`, so a non-pickle serializer must be able to handle whatever shape you pass to `respond()` too.

Write your own by subclassing `Serializer[T]` with `dumps(self, obj: T) -> bytes` and `loads(self, data: bytes) -> T`.
