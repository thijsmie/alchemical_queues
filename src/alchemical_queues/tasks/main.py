"""Implementation of the Alchemical Task Queues"""

import time
from datetime import datetime, timedelta
from logging import getLogger
from pydoc import locate
from typing import Callable, TypeVar, Union, Generic, Dict, cast, Any
from typing_extensions import ParamSpec, Concatenate
from ..main import AlchemicalQueue, AlchemicalEntry


class TaskInfo:
    """Meta description of the current task, as passed to task worker functions."""

    __slots__ = ["entry_id", "retries", "max_retries"]

    def __init__(self, entry_id: int, retries: int, max_retries: int) -> None:
        self.entry_id = entry_id
        self.retries = retries
        self.max_retries = max_retries


Param = ParamSpec("Param")
RValue = TypeVar("RValue")


class Worker:
    """Worker implementation that can take tasks from queues and execute them."""

    def __init__(
        self, queue: AlchemicalQueue, poll_every: timedelta = timedelta(seconds=1)
    ):
        self.queue = queue
        self.poll_every: timedelta = poll_every
        self.handler_registry: Dict[str, "Tasker"] = {}
        self.logger = getLogger("alchemical_queues.tasks")

    def _fail(
        self,
        job: AlchemicalEntry,
        data: Dict[str, Any],
        exception: BaseException = None,
    ):
        """Fail the specified task."""

        retries = data["retries"]

        if data.get("max_retries", 0) > retries:
            data["retries"] = retries + 1

            retry_at = datetime.now()
            if data["retry_in"] is not None:
                retry_at += data["retry_in"]

            job.data = data
            new_id = self.queue.put({"retry": job.entry_id}, schedule_at=retry_at)

            self.logger.info("Retrying failed job %s as `%s`", job.entry_id, new_id)
            return False

        self.logger.warning("Failed to perform job %s", job.entry_id)
        self.logger.exception(exception)

        return False

    def _perform(self, set_job: AlchemicalEntry):
        """Perform the specified task"""

        if "retry" in set_job.data:
            job = self.queue.fetch(set_job.data["retry"], editable=True)
            if job is None:
                self.logger.warning(
                    "Could not locate original job to retry (retry %s with %s)",
                    set_job.entry_id,
                    set_job.data["retry"],
                )
                return False
        else:
            job = set_job

        data = job.data
        function_path = data["function"]

        task_handler = self.handler_registry.get(function_path)

        if function_path not in self.handler_registry:
            task_handler = self.handler_registry[function_path] = cast(
                Tasker, locate(function_path)
            )

        if task_handler is None:
            return self._fail(
                job,
                data,
                KeyError(
                    f"AlchemicalEntry handler `{function_path}` not found.",
                ),
            )

        try:
            func = task_handler.get_handler()
            result = func(
                TaskInfo(job.entry_id, data["retries"], data["max_retries"]),
                *data["args"],
                **data["kwargs"],
            )
            data.update({"result": result})
            job.data = data
            return True
        except KeyboardInterrupt as interrupt:
            # Allow cancellation via interrupt signal
            raise interrupt
        except Exception as error:  # pylint: disable=broad-except
            return self._fail(job, data, error)

    def work(self):
        """Run jobs forever."""
        self.logger.info("Worker starting on queue `%s`.", self.queue.name)

        while True:
            job = self.queue.get()

            if job is None:
                time.sleep(self.poll_every.total_seconds())
            else:
                self._perform(job)

    def work_one(self, block=True):
        """Run exactly one job."""

        while True:
            job = self.queue.get()

            if job is not None:
                self._perform(job)
                return

            if block:
                time.sleep(self.poll_every.total_seconds())
            else:
                break


class QueuedTask(Generic[RValue]):
    """Represent a task in the queue."""

    def __init__(self, queue: AlchemicalQueue, entry_id: int, name: str):
        self._queue = queue
        self._entry_id = entry_id
        self._name = name

    @property
    def result(self) -> Union[RValue, None]:
        """Obtain the result of a queued task if it is finished, or None."""

        entry = self._queue.fetch(self._entry_id)

        if entry is None or entry.data.get("function") != self._name:
            return None

        return entry.data.get("result")


class Task(Generic[Param, RValue]):
    """Represent a task that is not yet queued to be executed"""

    def __init__(
        self,
        handler: Callable[Concatenate[TaskInfo, Param], RValue],
        *args: Param.args,
        **kwargs: Param.kwargs,
    ):
        self._handler = handler
        self._args = args
        self._kwargs = kwargs

    def schedule(
        self,
        on_queue: AlchemicalQueue,
        *,
        schedule_at: Union[datetime, None] = None,
        expected_at: Union[datetime, None] = None,
        max_retries: int = 0,
        retry_in: Union[timedelta, None] = None,
    ) -> QueuedTask[RValue]:
        """Schedule a task on a queue to be executed."""

        name = f"{self._handler.__module__}.{self._handler.__qualname__}"
        entry_id = on_queue.put(
            {
                "function": name,
                "args": self._args,
                "kwargs": self._kwargs,
                "retries": 0,
                "retry_in": retry_in,
                "max_retries": max_retries,
            },
            schedule_at=schedule_at,
            expected_at=expected_at,
        )
        return QueuedTask(queue=on_queue, entry_id=entry_id, name=name)


class Tasker(Generic[Param, RValue]):
    """Container for the schedulable task."""

    def __init__(self, handler: Callable[Concatenate[TaskInfo, Param], RValue]):
        self._handler = handler

    def __call__(
        self, *args: Param.args, **kwargs: Param.kwargs
    ) -> Task[Param, RValue]:
        return Task(self._handler, *args, **kwargs)

    def retrieve(self, queue: AlchemicalQueue, entry_id: int) -> QueuedTask[RValue]:
        """Retrieve an instance of this task that is already running."""

        name = f"{self._handler.__module__}.{self._handler.__qualname__}"
        return QueuedTask[RValue](queue=queue, entry_id=entry_id, name=name)

    def get_handler(self) -> Callable[Concatenate[TaskInfo, Param], RValue]:
        """Retrieve the original function."""
        return self._handler


def task(
    function: Callable[Concatenate[TaskInfo, Param], RValue]
) -> Tasker[Param, RValue]:
    """Decorator to turn a function into a runnable task."""

    return Tasker[Param, RValue](function)
