# Async plain Python example

The async equivalent of the
[`plain_python`](https://github.com/thijsmie/alchemical_queues/tree/main/examples/plain_python)
example: a task module, a worker script, and a producer script that
schedules work and awaits the result -- built on
[`AsyncAlchemicalQueues`][alchemical_queues.aio.AsyncAlchemicalQueues] /
[`AsyncWorker`][alchemical_queues.tasks.aio.AsyncWorker] instead of their
sync equivalents.

## Run it

```bash
cd examples/async_plain_python
```

In one terminal, start a worker (`--with aiosqlite` for the async SQLite
driver used here):

```bash
uv run --with aiosqlite python worker.py "sqlite+aiosqlite:///example.db"
```

In another terminal, schedule a task and await its result:

```bash
uv run --with aiosqlite python producer.py
```

You should see the worker print that it is running the task, and the
producer print the result once it is done.

There is no `alchemical_worker`-style CLI for async queues yet, which is
why `worker.py` is a plain script rather than a command -- see its
docstring.
