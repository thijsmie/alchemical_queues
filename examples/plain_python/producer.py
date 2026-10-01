"""Schedules an `add_numbers` task and waits for its result.

Run a worker in another terminal first (from this directory, so it can
import `tasks`):

    alchemical_worker "sqlite:///example.db" tasks

Then run this script:

    python producer.py
"""

import os
import time

from sqlalchemy import create_engine
from tasks import add_numbers

from alchemical_queues import AlchemicalQueues


def build_queues(database_url: str) -> AlchemicalQueues:
    engine = create_engine(database_url)
    queues = AlchemicalQueues(engine)
    queues.create_all()
    return queues


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "sqlite:///example.db")
    queues = build_queues(database_url)
    task_queue = queues.get_task_queue("tasks")

    task = add_numbers(2, 3).schedule(task_queue)
    print(f"Scheduled task {task.entry_id}, waiting for a worker to pick it up...")

    while not task.done:
        time.sleep(0.5)

    print(f"Result: {task.result}")


if __name__ == "__main__":
    main()
