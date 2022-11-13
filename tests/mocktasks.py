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
