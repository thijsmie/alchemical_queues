"""Litestar example: schedule a task from one route, poll for its result
from another.

For this mini example the worker runs in a background thread started on
Litestar's `on_startup` hook, so `litestar run` alone is enough to see it
work end to end. In a real deployment you would instead run
`alchemical_worker` as its own process (see the plain_python example) so it
can be scaled independently of the web app.
"""

import os
import threading
from datetime import timedelta
from typing import Union

from litestar import Litestar, get, post
from litestar.params import FromPath
from pydantic import BaseModel
from sqlalchemy import create_engine

from alchemical_queues import AlchemicalQueues
from alchemical_queues.tasks import TaskInfo, Worker, task


class AddRequest(BaseModel):
    a: int
    b: int


@task
def add_numbers(taskinfo: TaskInfo, a: int, b: int) -> int:
    return a + b


def create_app(database_url: Union[str, None] = None) -> Litestar:
    database_url = database_url or os.environ.get(
        "DATABASE_URL", "sqlite:///litestar_example.db"
    )
    engine = create_engine(database_url)
    queues = AlchemicalQueues(engine)
    queues.create_all()
    task_queue = queues.get_task_queue("add-queue")

    def start_worker() -> None:
        worker = Worker(task_queue, poll_every=timedelta(milliseconds=100))
        threading.Thread(target=worker.work, daemon=True).start()

    @post("/add")
    async def schedule_add(data: AddRequest) -> dict:
        entry = add_numbers(data.a, data.b).schedule(task_queue)
        return {"task_id": entry.entry_id}

    @get("/result/{task_id:int}")
    async def get_result(task_id: FromPath[int]) -> dict:
        queued = add_numbers.retrieve(task_queue, task_id)
        if not queued.done:
            return {"status": "pending"}
        return {"status": "done", "result": queued.result}

    return Litestar(
        route_handlers=[schedule_add, get_result], on_startup=[start_worker]
    )


app = create_app()
