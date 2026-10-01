"""Runs an AsyncWorker on the `tasks` queue forever.

There is no `alchemical_worker`-style CLI for async queues yet -- run this
script directly instead, from this directory (so it can `import tasks`):

    python worker.py "sqlite+aiosqlite:///example.db"
"""

import asyncio
import os
import sys
from datetime import timedelta

from sqlalchemy.ext.asyncio import create_async_engine
from tasks import add_numbers  # noqa: F401  (registers the handler by import)

from alchemical_queues.aio import AsyncAlchemicalQueues
from alchemical_queues.tasks.aio import AsyncWorker


async def main() -> None:
    database_url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///example.db")
    )
    engine = create_async_engine(database_url)
    queues = AsyncAlchemicalQueues(engine)
    await queues.create_all()

    task_queue = queues.get_task_queue("tasks")
    worker = AsyncWorker(task_queue, poll_every=timedelta(milliseconds=200))
    await worker.work()


if __name__ == "__main__":
    asyncio.run(main())
