"""Implementation of the Alchemical Task Queues"""

import time
from datetime import datetime, timedelta
from logging import getLogger
from pydoc import locate
from typing import Callable, TypeVar, Union, Generic, Dict, cast, Any, NoReturn
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
    """Worker implementation that can take tasks from queues and execute them.

    Attributes:
        queue (AlchemicalQueue): the queue this worker runs on
        poll_every (timedelta): how often to poll for new tasks
    """

    def __init__(
        self, queue: AlchemicalQueue, poll_every: timedelta = timedelta(seconds=1)
    ):
        self.queue = queue
        self.poll_every: timedelta = poll_every
        self._handler_registry: Dict[str, "Tasker"] = {}
        self._logger = getLogger("alchemical_queues.tasks")

    def _fail(
        self,
        entry_id: int,
        data: Dict[str, Any],
        exception: Union[BaseException, None] = None,
        fatal: bool = False,
    ):
        retries = data["retries"]

        if data.get("max_retries", 0) > retries and not fatal:
            data["retries"] = retries + 1

            retry_at = datetime.now()
            if data["retry_in"] is not None:
                retry_at += data["retry_in"]

            data["entry_id"] = entry_id
            new_entry = self.queue.put(data, schedule_at=retry_at)
            self._logger.info(
                "Retrying failed task %s as `%s`", entry_id, new_entry.entry_id
            )
            return False

        self._logger.warning("Failed to perform task %s", entry_id)
        self._logger.exception(exception)
        self.queue.respond(entry_id, {"error": str(exception)})

        return False

    def _perform(self, task_entry: AlchemicalEntry):
        data = task_entry.data
        entry_id = task_entry.data.get("entry_id") or task_entry.entry_id
        function_path = data["function"]
        task_handler = self._handler_registry.get(function_path)

        if function_path not in self._handler_registry:
            task_handler = self._handler_registry[function_path] = cast(
                Tasker, locate(function_path)
            )

        if task_handler is None:
            return self._fail(
                entry_id,
                data,
                KeyError(
                    f"AlchemicalEntry handler `{function_path}` not found.",
                ),
                fatal=True,
            )

        try:
            self._logger.info("Running task `%s`.", task_entry.entry_id)
            func = task_handler.get_handler()
            result = func(
                TaskInfo(task_entry.entry_id, data["retries"], data["max_retries"]),
                *data["args"],
                **data["kwargs"],
            )
            self.queue.respond(entry_id, {"result": result})
            return True
        except KeyboardInterrupt as interrupt:
            # Allow cancellation via interrupt signal
            raise interrupt
        except Exception as error:  # pylint: disable=broad-except
            return self._fail(entry_id, data, error)

    def work(self) -> NoReturn:
        """Run tasks forever."""
        self._logger.info("Worker starting on queue `%s`.", self.queue.name)

        while True:
            task_entry = self.queue.get()

            if task_entry is None:
                time.sleep(self.poll_every.total_seconds())
            else:
                self._perform(task_entry)

    def work_one(self, block: bool = True) -> None:
        """Run exactly one task.

        Args:
            block (bool): wether to block until a task is available, or exit immediately if not is available.
        """

        while True:
            task_entry = self.queue.get()

            if task_entry is not None:
                self._perform(task_entry)
                return

            if block:
                time.sleep(self.poll_every.total_seconds())
            else:
                break


class TaskException:
    """Represent a failed task.

    Attributes:
        msg (str): Stringified exception
    """

    __slots__ = ["msg"]

    def __init__(self, msg: str) -> None:
        self.msg: str = msg


class QueuedTask(Generic[RValue]):
    """Represent a task in the queue.

    Attributes:
        entry_id (int): The id of the entry into the queue that contains the task description.
    """

    def __init__(self, queue: AlchemicalQueue, entry_id: int, name: str):
        self._queue = queue
        self.entry_id = entry_id
        self._name = name

    @property
    def result(self) -> Union[RValue, TaskException, None]:
        """Obtain the result of a queued task if it is finished,
        an exception if the task failed to run, or None if the task
        has not completed.

        Returns:
            RValue: the value you return from the task handler.
            TaskException: the task failed to execute.
            None: the task has not completed.
        """

        responses = self._queue.responses(self.entry_id)

        if not responses:
            return None

        response = responses[0]
        data: dict = cast(dict, response.data)

        if "error" in data:
            return TaskException(data["error"])

        return cast(RValue, data.get("result"))


class Task(Generic[Param, RValue]):
    """Represent a task that is not yet queued to be executed. It is not
    constructed by the user, but it is returned when calling a task function."""

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
        priority: int = 0,
        max_retries: int = 0,
        retry_in: Union[timedelta, None] = None,
    ) -> QueuedTask[RValue]:
        """Schedule a task on a queue to be executed.

        Args:
            on_queue (AlchemicalQueue): the queue used as task queue.
                                        You are expected to run a worker connected to this queue.
            schedule_at (datetime, optional): do not run the task before this time.
            priority (int, optional): the task priority, using normal priority queue semantics.
            max_retries (int, optional): how many times the task should be retried before reporting failure.
            retry_in (timedelta, optional): the minimal timespan between two tries.
        """

        name = f"{self._handler.__module__}.{self._handler.__qualname__}"
        entry = on_queue.put(
            {
                "function": name,
                "args": self._args,
                "kwargs": self._kwargs,
                "retries": 0,
                "retry_in": retry_in,
                "max_retries": max_retries,
            },
            schedule_at=schedule_at,
            priority=priority,
        )
        return QueuedTask(queue=on_queue, entry_id=entry.entry_id, name=name)


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
    """Decorator to turn a function into a runnable task.

    Args:
        function (Callable): Any function you want to run as task. It should take a [TaskInfo][alchemical_queues.tasks.TaskInfo]
                             as first argument."""

    return Tasker[Param, RValue](function)
