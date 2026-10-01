"""FastAPI example: schedule a task from one endpoint, poll for its result
from another, with the task's result serialized as a pydantic model via
[`TaskResultSerializer`][alchemical_queues.tasks.TaskResultSerializer] +
[`PydanticSerializer`][alchemical_queues.PydanticSerializer].

For this mini example the worker runs in a background thread started from
FastAPI's lifespan, so `uvicorn app:app` alone is enough to see it work end
to end. In a real deployment you would instead run `alchemical_worker` as
its own process (see the plain_python example) so it can be scaled
independently of the web app.
"""

import os
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Union

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine

from alchemical_queues import AlchemicalQueues, PydanticSerializer
from alchemical_queues.tasks import TaskInfo, TaskResultSerializer, Worker, task


class AddRequest(BaseModel):
    a: int
    b: int


class AddResult(BaseModel):
    sum: int


@task
def add_numbers(taskinfo: TaskInfo, a: int, b: int) -> AddResult:
    return AddResult(sum=a + b)


def create_app(database_url: Union[str, None] = None) -> FastAPI:
    database_url = database_url or os.environ.get(
        "DATABASE_URL", "sqlite:///fastapi_example.db"
    )
    engine = create_engine(database_url)
    queues = AlchemicalQueues(engine)
    queues.create_all()

    task_queue = queues.get_task_queue(
        "add-queue",
        response_serializer=TaskResultSerializer(PydanticSerializer(AddResult)),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = Worker(task_queue, poll_every=timedelta(milliseconds=100))
        # Worker.work() runs forever; a daemon thread is enough for this
        # example since it is killed when the process exits anyway.
        threading.Thread(target=worker.work, daemon=True).start()
        yield

    app = FastAPI(title="Alchemical Queues - FastAPI example", lifespan=lifespan)

    @app.post("/add")
    def schedule_add(req: AddRequest) -> dict:
        entry = add_numbers(req.a, req.b).schedule(task_queue)
        return {"task_id": entry.entry_id}

    @app.get("/result/{task_id}")
    def get_result(task_id: int) -> dict:
        queued = add_numbers.retrieve(task_queue, task_id)
        if not queued.done:
            return {"status": "pending"}

        result = queued.result
        if isinstance(result, AddResult):
            return {"status": "done", "sum": result.sum}
        return {"status": "error", "error": str(result)}

    return app


app = create_app()
