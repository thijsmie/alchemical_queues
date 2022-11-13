# Tutorial

To work with *Alchemical Queues* you will need a SQLAlchemy *Engine* which defines the connection to your database. For this tutorial we will use a SQLite database but you can use whatever you already have.

```python
from sqlalchemy import create_engine

engine = create_engine(f"sqlite:///test.db")
```

We can now initialize the *Alchemical Queues* manager object on this engine. This will initialize some SQLAlchemy metadata on the engine.

```python
from alchemical_queues import AlchemicalQueues

queues = AlchemicalQueues(engine)
queues.create_all()
```

If you're used to Flask-SQLAlchemy: `queues.create_all` is like `db.create_all`. For the pure SQLAlchemy users, it is like `metadata.create_all`. It is creating the tables *Alchemical Queues* uses to store the queues and response data.

## Queues

The Queues in *Alchemical Queues* are named and distributed. They are thread and interprocess-safe. By being powered by a database and not something "smarter" they are not the fastest, but they are as reliable as the database backend powering them.

Let's obtain a named [`AlchemicalQueue`][alchemical_queues.AlchemicalQueue] and push some data to it:

```python
queue = queues.get("test-queue")
queue.put(42)
queue.put(137)
```

We can now run a second script to read back what we put in:

```python
print(queue.get().data)  # prints 42
print(queue.get().data)  # prints 137
```

Note that entries are returned in the same order as we put them in. The Queue is a so called *FIFO* Queue: first-in, first-out. Secondly, note that we access the `.data` attribute of what is returned, and not the data returned directly. Unlike the standard library `queue.Queue` the items are not returned as-is but have some additional information. What is returned is a [`AlchemicalEntry`][alchemical_queues.AlchemicalEntry] which has several other properties. This entry is also returned when you `put` something into the queue.

```python
entry1 = queue.put(12)
entry2 = queue.get()

# entry1 and entry2 represent the same entry
```

!!! note "What types of data are allowed in a Queue?"

    Data you put in a Queue is serialized to `bytes` using `pickle`. Any `pickle`able datatype is allowed.

## Tasks

Implemented on top of [`AlchemicalQueues`][alchemical_queues.AlchemicalQueues] is a Task Queue implementation like [celery](https://docs.celeryq.dev/). If you want to have a robust task queue with lots of features I would definitely recommend celery instead. However, `alchemical_queues.tasks` has the advantage of just needing `SQLite` to run, thus not requiring any installation of Redis or RabbitMQ or other broker. Running a worker is just another Python script.

We first define the task to run as a python function. This task function must be *importable* by the worker, so for the example put it in a file `tasks.py` in your working directory.

```python
from alchemical_queues.tasks import task

@task
def add_numbers(taskinfo, a, b):
    return a + b
```

The first argument to any task function is the taskinfo. It is sort of like the celery `current_task` containing some basic info. Now we can schedule the task:

```python
from tasks import add_numbers

todo = add_numbers(2, 3)
# Todo will not be 5 but an *unqueued task* we can schedule

# Obtain a task queue
queues = AlchemicalQueues(engine)
queue = queues.get("task-queue")

# Schedule the todo action on the queue
task = todo.schedule(queue)
```

We can wait for the task to finish:

```python
import time
while not task.result:
    time.sleep(1)
print(task.result)
```

In a separate terminal we can run the worker. Make sure it can import `add_numbers` by using the same working directory. We will start an
`alchemical_worker` on the same engine and queue as we used in the example.

```bash
$ alchemical_worker "sqlite:///test.db" task-queue
```

After the worker has completed the task the task wait loop will exit, printing `5`.
