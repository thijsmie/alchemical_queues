"""Starlette example: schedule a task from one route, poll for its result
from another.

For this mini example the worker runs in a background thread started from
Starlette's lifespan, so `uvicorn app:app` alone is enough to see it work
end to end. In a real deployment you would instead run `alchemical_worker`
as its own process (see the plain_python example) so it can be scaled
independently of the web app.
"""

import os
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Union

from sqlalchemy import create_engine
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from alchemical_queues import AlchemicalQueues
from alchemical_queues.tasks import TaskInfo, Worker, task


@task
def add_numbers(taskinfo: TaskInfo, a: int, b: int) -> int:
    return a + b


def create_app(database_url: Union[str, None] = None) -> Starlette:
    database_url = database_url or os.environ.get(
        "DATABASE_URL", "sqlite:///starlette_example.db"
    )
    engine = create_engine(database_url)
    queues = AlchemicalQueues(engine)
    queues.create_all()
    task_queue = queues.get_task_queue("add-queue")

    @asynccontextmanager
    async def lifespan(_: Starlette):
        worker = Worker(task_queue, poll_every=timedelta(milliseconds=100))
        threading.Thread(target=worker.work, daemon=True).start()
        yield

    async def schedule_add(request):
        payload = await request.json()
        entry = add_numbers(payload["a"], payload["b"]).schedule(task_queue)
        return JSONResponse({"task_id": entry.entry_id})

    async def get_result(request):
        task_id = int(request.path_params["task_id"])
        queued = add_numbers.retrieve(task_queue, task_id)
        if not queued.done:
            return JSONResponse({"status": "pending"}, status_code=202)
        return JSONResponse({"status": "done", "result": queued.result})

    return Starlette(
        routes=[
            Route("/add", schedule_add, methods=["POST"]),
            Route("/result/{task_id}", get_result),
        ],
        lifespan=lifespan,
    )


app = create_app()
