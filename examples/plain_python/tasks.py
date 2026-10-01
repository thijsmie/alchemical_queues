"""The task(s) our worker can run. Needs to be importable by the worker
process under this exact module path, e.g. run `alchemical_worker` from this
directory so it can `import tasks`.
"""

from alchemical_queues.tasks import TaskInfo, task


@task
def add_numbers(taskinfo: TaskInfo, a: int, b: int) -> int:
    print(f"Running task {taskinfo.entry_id}: {a} + {b}")
    return a + b
