# Plain Python example

The smallest possible setup: no web framework at all, just a task module and
a script that schedules work and waits for the result. This is the same
pattern as the
[tutorial](https://thijsmie.github.io/alchemical_queues/usage/tutorial/),
kept here as a runnable example.

## Run it

In one terminal, start a worker:

```bash
cd examples/plain_python
uv run alchemical_worker "sqlite:///example.db" tasks
```

In another terminal, schedule a task and wait for its result:

```bash
cd examples/plain_python
uv run python producer.py
```

You should see the worker print that it is running the task, and the
producer print the result once it is done.
