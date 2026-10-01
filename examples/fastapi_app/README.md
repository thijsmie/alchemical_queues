# FastAPI example (async)

A minimal FastAPI app that schedules an `add_numbers` task per request and
exposes a second endpoint to poll for the result. The result is a pydantic
model, serialized with
[`TaskResultSerializer`][alchemical_queues.tasks.TaskResultSerializer] +
[`PydanticSerializer`][alchemical_queues.PydanticSerializer]. Built on
[`AsyncAlchemicalQueues`][alchemical_queues.asyncio.AsyncAlchemicalQueues] /
[`AsyncWorker`][alchemical_queues.tasks.asyncio.AsyncWorker].

## Run it

```bash
cd examples/fastapi_app
pip install fastapi uvicorn aiosqlite
uvicorn app:app
```

Then, in another terminal:

```bash
curl -X POST localhost:8000/add -H 'Content-Type: application/json' -d '{"a": 2, "b": 3}'
# {"task_id": 1}

curl localhost:8000/result/1
# {"status": "done", "sum": 5}
```

The worker that executes the task runs as a plain `asyncio` task on the
app's own event loop, started from FastAPI's lifespan -- no background
thread needed, purely to keep this example to a single command. See the
[`async_plain_python`](https://github.com/thijsmie/alchemical_queues/tree/main/examples/async_plain_python)
example for the more common pattern of running the worker as a separate
process.
