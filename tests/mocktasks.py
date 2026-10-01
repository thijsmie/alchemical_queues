import time
from alchemical_queues.tasks import task, TaskInfo


@task
def increment(info: TaskInfo, data: int) -> int:
    return data + 1


@task
def decrement(info: TaskInfo, data: int) -> int:
    return data - 1


@task
def fail_once(info: TaskInfo, data: int) -> int:
    if info.retries == 0:
        raise Exception("First one fails")
    return data


@task
def fail_always(info: TaskInfo, data: int) -> int:
    raise Exception("Always fails")


@task
def returns_none(info: TaskInfo) -> None:
    return None


@task
def slow_task(info: TaskInfo, sleep_for: float) -> str:
    time.sleep(sleep_for)
    return f"done after {sleep_for}s (retries={info.retries})"
