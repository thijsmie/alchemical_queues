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
add_numbers(1,2).schedule(queue, priority=12)
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
add_numbers(1,2).schedule(queue, schedule_at=now+timedelta(seconds=30))
```

## Custom tables

If you don't want to use the default `AlchemicalQueue` and `AlchemicalResponse` tables you can configure them.

```python
queues = AlchemicalQueues(
    queue_tablename="queues",
    response_tablename="responses"
)
```

When you use `alchemical_worker` it will use the default names. You can run the worker via python on your custom queues.

```python
from alchemical_queues.tasks import Worker

Worker(queues.get("task-queue")).work()
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

This is useful when another part of your application already manages migrations or table creation for `Base.metadata`, and you want *Alchemical Queues*'s tables to be created and managed the same way.
