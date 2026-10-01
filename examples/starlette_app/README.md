# Starlette example

A minimal Starlette (plain ASGI) app that schedules an `add_numbers` task
per request and exposes a second route to poll for the result, using the
default [`PickleSerializer`][alchemical_queues.PickleSerializer].

## Run it

```bash
cd examples/starlette_app
pip install starlette uvicorn
uvicorn app:app
```

Then, in another terminal:

```bash
curl -X POST localhost:8000/add -H 'Content-Type: application/json' -d '{"a": 2, "b": 3}'
# {"task_id": 1}

curl localhost:8000/result/1
# {"status": "done", "result": 5}
```

The worker that executes the task runs in a background thread started from
Starlette's lifespan, purely to keep this example to a single command. See
the [`plain_python`](https://github.com/thijsmie/alchemical_queues/tree/main/examples/plain_python) example for the more common
pattern of running `alchemical_worker` as a separate process.
