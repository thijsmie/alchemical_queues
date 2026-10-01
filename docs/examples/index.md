# Examples

Runnable mini example apps live in the
[`examples/`](https://github.com/thijsmie/alchemical_queues/tree/main/examples)
directory of the repository, each with its own `README.md` with run
instructions. They are also covered by a smoke test in the test suite, so
they are guaranteed to keep working.

- [Plain Python](plain_python.md) -- no web framework at all, the same
  producer/worker split as the [tutorial](../usage/tutorial.md).
- [Async plain Python](async_plain_python.md) -- the same split again, but
  built on [`AsyncAlchemicalQueues`][alchemical_queues.asyncio.AsyncAlchemicalQueues]
  / [`AsyncWorker`][alchemical_queues.tasks.asyncio.AsyncWorker] instead of
  their sync equivalents.
- [FastAPI](fastapi.md) -- schedules a task per request, result serialized
  as a pydantic model. Also built on `AsyncAlchemicalQueues`/`AsyncWorker`,
  running the worker as a plain `asyncio` task on the app's own event loop.
- [Flask](flask.md) -- schedules a task per request, result serialized as
  JSON. See also the [Flask-SQLAlchemy example](../usage/flask_example.md)
  for sharing an engine with `flask_sqlalchemy`.
- [Starlette](starlette.md) -- the same pattern as FastAPI on plain ASGI,
  with the default pickle serializer.
- [Litestar](litestar.md) -- the same pattern again on
  [Litestar](https://litestar.dev/).

All of them use [`alchemical_queues.tasks`](../usage/tutorial.md#tasks).
FastAPI is the only one on the async core/worker so far; Flask, Starlette
and Litestar run their (sync) worker in a background thread inside the web
process purely to keep the example to a single command -- see each
example's README for why you would normally run `alchemical_worker` (or,
for the async examples, your own worker script) as a separate process
instead.
