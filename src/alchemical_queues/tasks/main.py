"""Implementation of the Alchemical Task Queues"""

import threading
import time
from datetime import datetime, timedelta
from logging import getLogger
from pydoc import locate
from typing import Any, Callable, Dict, Generic, NoReturn, TypeVar, Union, cast

from typing_extensions import Concatenate, ParamSpec

from ..main import AlchemicalEntry, AlchemicalTaskQueue, ClaimExpired


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
        queue (AlchemicalTaskQueue): the queue this worker runs on
        poll_every (timedelta): how often to poll for new tasks
        keepalive_every (timedelta | None): how often to extend a task's claim
            while it's still running. See `__init__`.
    """

    def __init__(
        self,
        queue: AlchemicalTaskQueue,
        poll_every: timedelta = timedelta(seconds=1),
        *,
        keepalive_every: Union[timedelta, None] = None,
    ):
        """
        Args:
            queue (AlchemicalTaskQueue): the queue this worker runs on. Obtain
                one via [AlchemicalQueues.get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue].
            poll_every (timedelta, optional): how often to poll for new tasks
                when the queue is empty.
            keepalive_every (timedelta | None, optional): if set, a background
                thread extends a task's claim by this often while its handler
                is still running, so a task that runs longer than the queue's
                `visibility_timeout` doesn't get redelivered to (and
                double-processed by) another worker. Pick something
                comfortably shorter than `visibility_timeout` -- a third of
                it is a reasonable starting point. Left `None` (the default),
                a task running longer than `visibility_timeout` risks exactly
                that: if its claim lapses, this worker's eventual result is
                discarded (logged as a warning) rather than responded with,
                to avoid risking a duplicate.
        """
        self.queue = queue
        self.poll_every: timedelta = poll_every
        self.keepalive_every = keepalive_every
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
        self.queue.respond(
            entry_id,
            {
                "error": str(exception),
                "error_type": type(exception).__name__ if exception else None,
            },
        )

        return False

    def _keepalive_loop(
        self, task_entry: AlchemicalEntry, stop: threading.Event
    ) -> None:
        assert self.keepalive_every is not None
        assert task_entry.claim_token is not None
        while not stop.wait(self.keepalive_every.total_seconds()):
            try:
                self.queue.extend(task_entry.entry_id, task_entry.claim_token)
            except ClaimExpired:
                # Nothing more we can do here -- the main thread's own
                # discard() call below will discover the same thing and log
                # it once, so don't duplicate the warning.
                return

    def _perform(self, task_entry: AlchemicalEntry):
        # task_entry always comes from self.queue.get(), which always sets a
        # claim_token -- unlike a bare put() result, where it would be None.
        assert task_entry.claim_token is not None
        claim_token: int = task_entry.claim_token

        data = task_entry.data
        entry_id = task_entry.data.get("entry_id") or task_entry.entry_id
        function_path = data["function"]

        task_handler = self._handler_registry.get(function_path)
        if function_path not in self._handler_registry:
            task_handler = self._handler_registry[function_path] = cast(
                Tasker, locate(function_path)
            )

        succeeded = False
        result: Any = None
        error: Union[BaseException, None] = None
        fatal = False

        if task_handler is None:
            error = KeyError(f"AlchemicalEntry handler `{function_path}` not found.")
            fatal = True
        else:
            keepalive_stop = threading.Event()
            keepalive_thread = None
            if self.keepalive_every is not None:
                keepalive_thread = threading.Thread(
                    target=self._keepalive_loop,
                    args=(task_entry, keepalive_stop),
                    daemon=True,
                )
                keepalive_thread.start()

            try:
                self._logger.info("Running task `%s`.", task_entry.entry_id)
                func = task_handler.get_handler()
                result = func(
                    TaskInfo(task_entry.entry_id, data["retries"], data["max_retries"]),
                    *data["args"],
                    **data["kwargs"],
                )
                succeeded = True
            except KeyboardInterrupt:
                # Allow cancellation via interrupt signal, but let the claim
                # go back to the queue immediately rather than leaving it to
                # time out, since we're not actually going to finish it.
                try:
                    self.queue.release(task_entry.entry_id, claim_token)
                except ClaimExpired:
                    pass
                raise
            except Exception as caught:  # pylint: disable=broad-except
                error = caught
            finally:
                if keepalive_thread is not None:
                    keepalive_stop.set()
                    keepalive_thread.join()

        # Fencing gate: only act on the outcome above if we still hold this
        # exact claim. If our visibility_timeout lapsed while we were working
        # (and nothing extended it in time) this entry may already have been
        # redelivered to, and handled by, another worker -- discard() then
        # raises instead of succeeding, and we discard our own outcome rather
        # than risk responding (or retrying) a second time for it.
        try:
            self.queue.discard(task_entry.entry_id, claim_token)
        except ClaimExpired:
            self._logger.warning(
                "Claim for task %s expired before we could act on its outcome "
                "(it may already have been redelivered and handled elsewhere); "
                "discarding our own result rather than risking a duplicate. If "
                "this task can run longer than the queue's visibility_timeout, "
                "pass keepalive_every= to Worker() or raise visibility_timeout.",
                task_entry.entry_id,
            )
            return False

        if succeeded:
            self.queue.respond(entry_id, {"result": result})
            return True

        return self._fail(entry_id, data, error, fatal=fatal)

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
        exception_type (str | None): The original exception's class name, e.g.
            `"ValueError"`, if known. None for failures alchemical_queues itself
            raised without an underlying exception object (this shouldn't
            normally happen).
    """

    __slots__ = ["msg", "exception_type"]

    def __init__(self, msg: str, exception_type: Union[str, None] = None) -> None:
        self.msg: str = msg
        self.exception_type: Union[str, None] = exception_type

    def __repr__(self) -> str:
        label = self.exception_type or "TaskException"
        return f"<{label}: {self.msg}>"

    def __str__(self) -> str:
        return self.__repr__()


class QueuedTask(Generic[RValue]):
    """Represent a task in the queue.

    Attributes:
        entry_id (int): The id of the entry into the queue that contains the task description.
    """

    def __init__(self, queue: AlchemicalTaskQueue, entry_id: int, name: str):
        self._queue = queue
        self.entry_id = entry_id
        self._name = name

    @property
    def done(self) -> bool:
        """Whether this task has a recorded outcome yet (success or failure).

        `result` returns `None` both when the task hasn't completed yet and
        when it has completed successfully with a return value of `None`, so
        if your task handler's success value can legitimately be `None` (a
        "send this email" task that returns nothing, say), check `done`
        rather than relying on `result`'s truthiness to know whether it's
        finished.

        Returns:
            bool: True once `result` reflects a real outcome.
        """

        return bool(self._queue.responses(self.entry_id))

    @property
    def result(self) -> Union[RValue, TaskException, None]:
        """Obtain the result of a queued task if it is finished,
        an exception if the task failed to run, or None if the task
        has not completed.

        This reads whatever was last recorded with
        [AlchemicalTaskQueue.respond][alchemical_queues.AlchemicalTaskQueue.respond]
        for this task's entry_id. `tasks.Worker` always records
        `{"result": ...}` on success or `{"error": ..., "error_type": ...}`
        on failure, which is what lets this property tell those two apart.
        If you call `respond()` yourself with something else entirely (it's
        a general queue-level API, not Worker-specific), there's no such
        shape to interpret -- you get that data back as-is, same as `RValue`.

        Returns:
            RValue: the value you return from the task handler (or, verbatim,
                whatever was passed to `respond()` if it wasn't a dict with a
                `result`/`error` key).
            TaskException: the task failed to execute.
            None: the task has not completed -- but also what you get if the
                task completed successfully and returned None itself; see `done`.
        """

        responses = self._queue.responses(self.entry_id)

        if not responses:
            return None

        data = responses[0].data

        if not isinstance(data, dict):
            return cast(RValue, data)

        if "error" in data:
            return TaskException(data["error"], data.get("error_type"))

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

    @property
    def name(self) -> str:
        """The dotted path this task will be registered and looked up under
        -- the same value `schedule()` stores as `data["function"]`."""
        return f"{self._handler.__module__}.{self._handler.__qualname__}"

    def schedule(
        self,
        on_queue: AlchemicalTaskQueue,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
        max_retries: int = 0,
        retry_in: Union[timedelta, None] = None,
    ) -> QueuedTask[RValue]:
        """Schedule a task on a queue to be executed.

        Args:
            on_queue (AlchemicalTaskQueue): the queue used as task queue.
                                        You are expected to run a worker connected to this queue.
            schedule_at (datetime, optional): do not run the task before this time.
            priority (int, optional): the task priority, using normal priority queue semantics.
            max_retries (int, optional): how many times the task should be retried before reporting failure.
            retry_in (timedelta, optional): the minimal timespan between two tries.
        """

        name = self.name
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

    def retrieve(self, queue: AlchemicalTaskQueue, entry_id: int) -> QueuedTask[RValue]:
        """Retrieve an instance of this task that is already running."""

        name = f"{self._handler.__module__}.{self._handler.__qualname__}"
        return QueuedTask[RValue](queue=queue, entry_id=entry_id, name=name)

    def get_handler(self) -> Callable[Concatenate[TaskInfo, Param], RValue]:
        """Retrieve the original function."""
        return self._handler


def task(
    function: Callable[Concatenate[TaskInfo, Param], RValue],
) -> Tasker[Param, RValue]:
    """Decorator to turn a function into a runnable task.

    Args:
        function (Callable): Any function you want to run as task. It should take a [TaskInfo][alchemical_queues.tasks.TaskInfo]
                             as first argument."""

    return Tasker[Param, RValue](function)
