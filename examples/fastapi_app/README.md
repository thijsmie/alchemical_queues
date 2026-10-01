# FastAPI example

A minimal FastAPI app that schedules an `add_numbers` task per request and
exposes a second endpoint to poll for the result. The result is a pydantic
model, serialized with
[`TaskResultSerializer`][alchemical_queues.tasks.TaskResultSerializer] +
[`PydanticSerializer`][alchemical_queues.PydanticSerializer].

## Run it

```bash
cd examples/fastapi_app
pip install fastapi uvicorn
uvicorn app:app
```

Then, in another terminal:

```bash
curl -X POST localhost:8000/add -H 'Content-Type: application/json' -d '{"a": 2, "b": 3}'
# {"task_id": 1}

curl localhost:8000/result/1
# {"status": "done", "sum": 5}
```

The worker that executes the task runs in a background thread started from
FastAPI's lifespan, purely to keep this example to a single command. See
the [`plain_python`](https://github.com/thijsmie/alchemical_queues/tree/main/examples/plain_python) example for the more common
pattern of running `alchemical_worker` as a separate process.
