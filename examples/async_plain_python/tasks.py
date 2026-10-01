"""The task(s) our async worker can run. Needs to be importable by the
worker process under this exact module path, i.e. run `worker.py` from this
directory so it can `import tasks`.
"""

import asyncio

from alchemical_queues.tasks.asyncio import async_task
from alchemical_queues.tasks.main import TaskInfo


@async_task
async def add_numbers(taskinfo: TaskInfo, a: int, b: int) -> int:
    print(f"Running task {taskinfo.entry_id}: {a} + {b}")
    # Stand in for an awaitable call (an HTTP request, another query, ...) --
    # this is the kind of work AsyncWorker lets you run concurrently with
    # other tasks on the same event loop, unlike the sync Worker.
    await asyncio.sleep(0)
    return a + b
