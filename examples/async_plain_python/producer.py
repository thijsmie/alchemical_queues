"""Schedules an `add_numbers` task and awaits its result.

Run a worker in another terminal first (from this directory, so it can
import `tasks`):

    python worker.py "sqlite+aiosqlite:///example.db"

Then run this script:

    python producer.py
"""

import asyncio
import os

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tasks import add_numbers

from alchemical_queues.asyncio import AsyncAlchemicalQueues


def build_queues(database_url: str) -> AsyncAlchemicalQueues:
    engine: AsyncEngine = create_async_engine(database_url)
    return AsyncAlchemicalQueues(engine)


async def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///example.db")
    queues = build_queues(database_url)
    await queues.create_all()
    task_queue = queues.get_task_queue("tasks")

    task = await add_numbers(2, 3).schedule(task_queue)
    print(f"Scheduled task {task.entry_id}, waiting for a worker to pick it up...")

    while not await task.done():
        await asyncio.sleep(0.5)

    print(f"Result: {await task.result()}")


if __name__ == "__main__":
    asyncio.run(main())
