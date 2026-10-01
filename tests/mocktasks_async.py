import asyncio

from alchemical_queues.tasks.asyncio import async_task
from alchemical_queues.tasks.main import TaskInfo


@async_task
async def increment(info: TaskInfo, data: int) -> int:
    return data + 1


@async_task
async def fail_once(info: TaskInfo, data: int) -> int:
    if info.retries == 0:
        raise Exception("First one fails")
    return data


@async_task
async def fail_always(info: TaskInfo, data: int) -> int:
    raise Exception("Always fails")


@async_task
async def returns_none(info: TaskInfo) -> None:
    return None


@async_task
async def slow_task(info: TaskInfo, sleep_for: float) -> str:
    await asyncio.sleep(sleep_for)
    return f"done after {sleep_for}s (retries={info.retries})"
