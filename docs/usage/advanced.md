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

## Periodic tasks

`periodic()` and `Beat` run a `@task`-decorated function on a fixed
interval, built entirely on top of the normal queue/task API -- there's no
separate "periodic queue" concept, `Beat` just enqueues a regular task run
whenever one is due.

```python
from datetime import timedelta
from alchemical_queues.tasks import TaskInfo, task
from alchemical_queues.tasks.periodic import Beat, periodic

@task
def cleanup(info: TaskInfo, max_age_days: int) -> None:
    ...

task_queue = queues.get_task_queue("task-queue")
schedule = periodic(cleanup, name="cleanup", every=timedelta(hours=1))(max_age_days=30)

beat = Beat(engine, task_queue, [schedule])
beat.create_all()  # creates Beat's own schedule table, once
beat.run()          # ticks forever; run this in its own process
```

`name` is a stable identifier for the schedule (changing it starts a fresh
one). Pass `start_at=` a `datetime` to defer the very first run; after that,
run times stay on a fixed grid anchored at `start_at` (`start_at`,
`start_at + every`, `start_at + 2 * every`, ...) rather than drifting later
depending on when each `tick()` happens to run. Run several `Beat`
processes at once for redundancy -- they never double-enqueue the same due
run. You still need a `Worker` (or several) consuming `task_queue` to
actually execute `cleanup` -- `Beat` only enqueues it.

Only fixed intervals are supported, not cron expression syntax.

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

The same `serializer=`/`get_serialized()` pair is available on `get_task_queue()`/`get_task_queue_serialized()`, which also take an independent `response_serializer=`: a task queue's entries (`put()`/`get()`) and its responses (`respond()`/`responses()`) are a different `AlchemicalTaskQueue[T, R]` type parameter each, with their own serializer, since a task's input and its result are usually different shapes.

```python
from pydantic import BaseModel
from alchemical_queues.serializers import PydanticSerializer

class Job(BaseModel):
    user_id: int
    payload: str

class JobResult(BaseModel):
    output_url: str

# get_task_queue_serialized() infers T from serializer and R from
# response_serializer, so entry.data is a Job and responses()[i].data is a
# JobResult -- independently serialized, no shared shape required.
task_queue = queues.get_task_queue_serialized(
    "job-queue", PydanticSerializer(Job), response_serializer=PydanticSerializer(JobResult)
)
```

Write your own `Serializer` by subclassing `Serializer[T]` with `dumps(self, obj: T) -> bytes` and `loads(self, data: bytes) -> T`.

### Typing a task's result with `tasks.Worker`

`tasks.Worker` always responds with `{"result": ...}` on success or `{"error": ..., "error_type": ...}` on failure -- `QueuedTask.result` needs that envelope to tell the two apart. Passing a plain serializer as `response_serializer` would mean serializing that whole envelope (and would break on the failure case, which is a different shape). `tasks.TaskResultSerializer` instead wraps an inner serializer that only ever sees the *success value* -- what your task handler returns -- and keeps Worker's envelope around it:

```python
from alchemical_queues.tasks import TaskResultSerializer

task_queue = queues.get_task_queue_serialized(
    "job-queue",
    PickleSerializer(),  # the task envelope itself (function/args/kwargs/...)
    response_serializer=TaskResultSerializer(PydanticSerializer(JobResult)),
)

handle = run_job(...).schedule(task_queue)
tasks.Worker(task_queue).work_one()
handle.result  # a JobResult, or a TaskException on failure -- same as always
```

## Async

Everything above has an async equivalent built on `sqlalchemy.ext.asyncio`'s
`AsyncEngine`/`AsyncSession`, under `alchemical_queues.aio` /
`alchemical_queues.tasks.aio`: `AsyncAlchemicalQueues`,
`AsyncAlchemicalQueue`, `AsyncAlchemicalTaskQueue`, `AsyncWorker`, and
`async_task`/`AsyncTask`/`AsyncQueuedTask`. The API is the same shape as
the sync classes -- every method that does I/O is just `async def` instead:

```python
from sqlalchemy.ext.asyncio import create_async_engine
from alchemical_queues.aio import AsyncAlchemicalQueues
from alchemical_queues.tasks.aio import AsyncWorker, async_task
from alchemical_queues.tasks.main import TaskInfo

engine = create_async_engine("sqlite+aiosqlite:///example.db")
queues = AsyncAlchemicalQueues(engine)
await queues.create_all()

@async_task
async def add_numbers(info: TaskInfo, a: int, b: int) -> int:
    return a + b

task_queue = queues.get_task_queue("tasks")
handle = await add_numbers(1, 2).schedule(task_queue)

await AsyncWorker(task_queue).work_one()
await handle.result()  # 3
```

Only `async def` handlers decorated with `async_task` can be run by
`AsyncWorker` -- a sync handler decorated with `tasks.task` needs the sync
`Worker` instead. There is no async equivalent of the `alchemical_worker`
CLI yet; run your own script with `asyncio.run(worker.work())`, as in the
[async_plain_python example](../examples/async_plain_python.md).

See the [FastAPI example](../examples/fastapi.md) for running the worker
as a plain `asyncio` task on a web app's own event loop, with no background
thread needed.
