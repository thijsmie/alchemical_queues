# Flask example

A minimal Flask app that schedules an `add_numbers` task per request and
exposes a second endpoint to poll for the result, using
[`JsonSerializer`][alchemical_queues.JsonSerializer] for task results.

## Run it

```bash
cd examples/flask_app
uv run --with flask flask run
```

Then, in another terminal:

```bash
curl -X POST localhost:5000/add -H 'Content-Type: application/json' -d '{"a": 2, "b": 3}'
# {"task_id": 1}

curl localhost:5000/result/1
# {"result": 5, "status": "done"}
```

The worker that executes the task runs in a background thread inside the
Flask process, purely to keep this example to a single command. See the
[`plain_python`](https://github.com/thijsmie/alchemical_queues/tree/main/examples/plain_python) example for the more common
pattern of running `alchemical_worker` as a separate process.
