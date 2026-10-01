"""FastAPI example: schedule a task from one endpoint, poll for its result
from another, with the task's result serialized as a pydantic model via
[`TaskResultSerializer`][alchemical_queues.tasks.TaskResultSerializer] +
[`PydanticSerializer`][alchemical_queues.PydanticSerializer].

Built on [`AsyncAlchemicalQueues`][alchemical_queues.aio.AsyncAlchemicalQueues]
/ [`AsyncWorker`][alchemical_queues.tasks.aio.AsyncWorker]: the worker
runs as a plain `asyncio` task on FastAPI's own event loop, started from the
lifespan, so `uvicorn app:app` alone is enough to see it work end to end --
no background thread needed, unlike the sync queue classes. In a real
deployment you would instead run the worker as its own process (see the
async_plain_python example) so it can be scaled independently of the web app.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Union

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine

from alchemical_queues import PydanticSerializer
from alchemical_queues.aio import AsyncAlchemicalQueues
from alchemical_queues.tasks import TaskResultSerializer
from alchemical_queues.tasks.aio import AsyncWorker, async_task
from alchemical_queues.tasks.main import TaskInfo


class AddRequest(BaseModel):
    a: int
    b: int


class AddResult(BaseModel):
    sum: int


@async_task
async def add_numbers(taskinfo: TaskInfo, a: int, b: int) -> AddResult:
    return AddResult(sum=a + b)


def create_app(database_url: Union[str, None] = None) -> FastAPI:
    database_url = database_url or os.environ.get(
        "DATABASE_URL", "sqlite+aiosqlite:///fastapi_example.db"
    )
    engine = create_async_engine(database_url)
    queues = AsyncAlchemicalQueues(engine)

    task_queue = queues.get_task_queue(
        "add-queue",
        response_serializer=TaskResultSerializer(PydanticSerializer(AddResult)),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await queues.create_all()
        worker = AsyncWorker(task_queue, poll_every=timedelta(milliseconds=100))
        # AsyncWorker.work() runs forever; this task shares the app's own
        # event loop and is cancelled on shutdown rather than left to leak.
        worker_task = asyncio.create_task(worker.work())
        try:
            yield
        finally:
            worker_task.cancel()

    app = FastAPI(title="Alchemical Queues - async FastAPI example", lifespan=lifespan)

    @app.post("/add")
    async def schedule_add(req: AddRequest) -> dict:
        entry = await add_numbers(req.a, req.b).schedule(task_queue)
        return {"task_id": entry.entry_id}

    @app.get("/result/{task_id}")
    async def get_result(task_id: int) -> dict:
        queued = add_numbers.retrieve(task_queue, task_id)
        if not await queued.done():
            return {"status": "pending"}

        result = await queued.result()
        if isinstance(result, AddResult):
            return {"status": "done", "sum": result.sum}
        return {"status": "error", "error": str(result)}

    return app


app = create_app()
