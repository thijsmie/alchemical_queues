"""Flask example: schedule a task from a request handler, poll for its
result from another one.

For this mini example the worker runs in a background thread inside the
same process, so `flask run` alone is enough to see it work end to end. In a
real deployment you would instead run `alchemical_worker` as its own
process (see the plain_python example) so it can be scaled independently of
the web app.
"""

import os
import threading
from datetime import timedelta
from typing import Union

from flask import Flask, jsonify, request
from sqlalchemy import create_engine

from alchemical_queues import AlchemicalQueues, JsonSerializer
from alchemical_queues.tasks import TaskInfo, Worker, task


@task
def add_numbers(taskinfo: TaskInfo, a: int, b: int) -> int:
    return a + b


def create_app(database_url: Union[str, None] = None) -> Flask:
    app = Flask(__name__)

    database_url = database_url or os.environ.get(
        "DATABASE_URL", "sqlite:///flask_example.db"
    )
    engine = create_engine(database_url)
    queues = AlchemicalQueues(engine)
    queues.create_all()

    # JsonSerializer for responses: results only ever need to be plain ints
    # here, and JSON is a more portable storage format than pickle.
    task_queue = queues.get_task_queue(
        "add-queue", response_serializer=JsonSerializer()
    )

    worker = Worker(task_queue, poll_every=timedelta(milliseconds=100))
    threading.Thread(target=worker.work, daemon=True).start()

    @app.post("/add")
    def schedule_add():
        payload = request.get_json()
        entry = add_numbers(payload["a"], payload["b"]).schedule(task_queue)
        return jsonify(task_id=entry.entry_id)

    @app.get("/result/<int:task_id>")
    def get_result(task_id: int):
        queued = add_numbers.retrieve(task_queue, task_id)
        if not queued.done:
            return jsonify(status="pending"), 202
        return jsonify(status="done", result=queued.result)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
