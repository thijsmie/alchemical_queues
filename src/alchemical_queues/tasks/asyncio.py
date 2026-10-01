"""Async equivalents of [tasks.Worker][alchemical_queues.tasks.Worker] and
the `task`/`Task`/`QueuedTask`/`Tasker` scheduling API, built on
[AsyncAlchemicalTaskQueue][alchemical_queues.asyncio.AsyncAlchemicalTaskQueue].

`Task.schedule()`/`QueuedTask.result` in `tasks.main` call `queue.put()`/
`queue.responses()` directly (not awaited), so they can't be reused as-is
against an `AsyncAlchemicalTaskQueue` -- `AsyncTask`/`AsyncQueuedTask` below
are async-aware equivalents for exactly that reason. `TaskInfo`/
`TaskException` have no I/O in them and are shared unchanged with
`tasks.main`.

`AsyncWorker` only runs `async def` handlers, registered via `async_task`
-- a sync handler registered with [task][alchemical_queues.tasks.task]
needs the sync [Worker][alchemical_queues.tasks.Worker] instead.
"""

import asyncio
from datetime import datetime, timedelta
from logging import getLogger
from pydoc import locate
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    NoReturn,
    TypeVar,
    Union,
    cast,
)

from typing_extensions import Concatenate, ParamSpec

from ..asyncio import AsyncAlchemicalTaskQueue
from ..main import AlchemicalEntry, ClaimExpired
from .main import TaskException, TaskInfo

Param = ParamSpec("Param")
RValue = TypeVar("RValue")


class AsyncQueuedTask(Generic[RValue]):
    """Async equivalent of [tasks.QueuedTask][alchemical_queues.tasks.QueuedTask].

    Attributes:
        entry_id (int): The id of the entry into the queue that contains the task description.
    """

    def __init__(self, queue: AsyncAlchemicalTaskQueue, entry_id: int, name: str):
        self._queue = queue
        self.entry_id = entry_id
        self._name = name

    async def done(self) -> bool:
        """Whether this task has a recorded outcome yet. See
        [QueuedTask.done][alchemical_queues.tasks.QueuedTask.done] for
        details.
        """
        return bool(await self._queue.responses(self.entry_id))

    async def result(self) -> Union[RValue, TaskException, None]:
        """Obtain the result of a queued task if it is finished, an
        exception if the task failed to run, or None if the task has not
        completed. See
        [QueuedTask.result][alchemical_queues.tasks.QueuedTask.result] for
        details.
        """
        responses = await self._queue.responses(self.entry_id)

        if not responses:
            return None

        data = responses[0].data

        if not isinstance(data, dict):
            return cast(RValue, data)

        if "error" in data:
            return TaskException(data["error"], data.get("error_type"))

        return cast(RValue, data.get("result"))


class AsyncTask(Generic[Param, RValue]):
    """Async equivalent of [tasks.Task][alchemical_queues.tasks.Task]. Not
    constructed by the user, but returned when calling an `async_task`
    function."""

    def __init__(
        self,
        handler: Callable[Concatenate[TaskInfo, Param], Awaitable[RValue]],
        *args: Param.args,
        **kwargs: Param.kwargs,
    ):
        self._handler = handler
        self._args = args
        self._kwargs = kwargs

    async def schedule(
        self,
        on_queue: AsyncAlchemicalTaskQueue,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
        max_retries: int = 0,
        retry_in: Union[timedelta, None] = None,
    ) -> AsyncQueuedTask[RValue]:
        """Schedule a task on a queue to be executed. See
        [Task.schedule][alchemical_queues.tasks.Task.schedule] for details.

        Args:
            on_queue (AsyncAlchemicalTaskQueue): the queue used as task queue.
                You are expected to run an `AsyncWorker` connected to this queue.
            schedule_at (datetime, optional): do not run the task before this time.
            priority (int, optional): the task priority, using normal priority queue semantics.
            max_retries (int, optional): how many times the task should be retried before reporting failure.
            retry_in (timedelta, optional): the minimal timespan between two tries.
        """
        name = f"{self._handler.__module__}.{self._handler.__qualname__}"
        entry = await on_queue.put(
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
        return AsyncQueuedTask(queue=on_queue, entry_id=entry.entry_id, name=name)


class AsyncTasker(Generic[Param, RValue]):
    """Async equivalent of [tasks.Tasker][alchemical_queues.tasks.Tasker]:
    container for a schedulable async task handler."""

    def __init__(
        self, handler: Callable[Concatenate[TaskInfo, Param], Awaitable[RValue]]
    ):
        self._handler = handler

    def __call__(
        self, *args: Param.args, **kwargs: Param.kwargs
    ) -> AsyncTask[Param, RValue]:
        return AsyncTask(self._handler, *args, **kwargs)

    def retrieve(
        self, queue: AsyncAlchemicalTaskQueue, entry_id: int
    ) -> AsyncQueuedTask[RValue]:
        """Retrieve an instance of this task that is already running."""
        name = f"{self._handler.__module__}.{self._handler.__qualname__}"
        return AsyncQueuedTask[RValue](queue=queue, entry_id=entry_id, name=name)

    def get_handler(self) -> Callable[Concatenate[TaskInfo, Param], Awaitable[RValue]]:
        """Retrieve the original async handler function."""
        return self._handler


def async_task(
    function: Callable[Concatenate[TaskInfo, Param], Awaitable[RValue]],
) -> AsyncTasker[Param, RValue]:
    """Decorator to turn an `async def` function into a runnable task, for
    use with [AsyncWorker][alchemical_queues.tasks.asyncio.AsyncWorker]. See
    [tasks.task][alchemical_queues.tasks.task] for the sync equivalent.

    Args:
        function (Callable): An `async def` function you want to run as a
            task. It should take a [TaskInfo][alchemical_queues.tasks.TaskInfo]
            as first argument.
    """
    return AsyncTasker[Param, RValue](function)


class AsyncWorker:
    """Async equivalent of [tasks.Worker][alchemical_queues.tasks.Worker].

    Attributes:
        queue (AsyncAlchemicalTaskQueue): the queue this worker runs on
        poll_every (timedelta): how often to poll for new tasks
        keepalive_every (timedelta | None): how often to extend a task's claim
            while it's still running. See `__init__`.
    """

    def __init__(
        self,
        queue: AsyncAlchemicalTaskQueue,
        poll_every: timedelta = timedelta(seconds=1),
        *,
        keepalive_every: Union[timedelta, None] = None,
    ):
        """
        Args:
            queue (AsyncAlchemicalTaskQueue): the queue this worker runs on.
                Obtain one via
                [AsyncAlchemicalQueues.get_task_queue][alchemical_queues.asyncio.AsyncAlchemicalQueues.get_task_queue].
            poll_every (timedelta, optional): how often to poll for new tasks
                when the queue is empty.
            keepalive_every (timedelta | None, optional): if set, a background
                asyncio task extends a task's claim by this often while its
                handler is still running -- see
                [tasks.Worker][alchemical_queues.tasks.Worker] for why you'd
                want this. Left `None` (the default), a task running longer
                than `visibility_timeout` risks its result being discarded
                rather than responded with, to avoid risking a duplicate.

        Only `async def` handlers registered via `async_task` can be run by
        this worker -- a plain sync handler registered with
        [task][alchemical_queues.tasks.task] needs the sync
        [Worker][alchemical_queues.tasks.Worker] instead.
        """
        self.queue = queue
        self.poll_every: timedelta = poll_every
        self.keepalive_every = keepalive_every
        self._handler_registry: Dict[str, "AsyncTasker"] = {}
        self._logger = getLogger("alchemical_queues.tasks")

    async def _fail(
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
            new_entry = await self.queue.put(data, schedule_at=retry_at)
            self._logger.info(
                "Retrying failed task %s as `%s`", entry_id, new_entry.entry_id
            )
            return False

        self._logger.warning("Failed to perform task %s", entry_id)
        self._logger.exception(exception)
        await self.queue.respond(
            entry_id,
            {
                "error": str(exception),
                "error_type": type(exception).__name__ if exception else None,
            },
        )

        return False

    async def _keepalive_loop(self, task_entry: AlchemicalEntry) -> None:
        assert self.keepalive_every is not None
        assert task_entry.claim_token is not None
        while True:
            await asyncio.sleep(self.keepalive_every.total_seconds())
            try:
                await self.queue.extend(task_entry.entry_id, task_entry.claim_token)
            except ClaimExpired:
                # Nothing more we can do here -- the main task's own
                # discard() call will discover the same thing and log it
                # once, so don't duplicate the warning.
                return

    async def _perform(self, task_entry: AlchemicalEntry):
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
                AsyncTasker, locate(function_path)
            )

        succeeded = False
        result: Any = None
        error: Union[BaseException, None] = None
        fatal = False

        if task_handler is None:
            error = KeyError(f"AlchemicalEntry handler `{function_path}` not found.")
            fatal = True
        else:
            keepalive_task: Union["asyncio.Task[None]", None] = None
            if self.keepalive_every is not None:
                keepalive_task = asyncio.create_task(self._keepalive_loop(task_entry))

            try:
                self._logger.info("Running task `%s`.", task_entry.entry_id)
                func = task_handler.get_handler()
                result = await func(
                    TaskInfo(task_entry.entry_id, data["retries"], data["max_retries"]),
                    *data["args"],
                    **data["kwargs"],
                )
                succeeded = True
            except asyncio.CancelledError:
                # Allow cancellation (e.g. worker shutdown), but let the
                # claim go back to the queue immediately rather than leaving
                # it to time out, since we're not actually going to finish it.
                try:
                    await self.queue.release(task_entry.entry_id, claim_token)
                except ClaimExpired:
                    pass
                raise
            except Exception as caught:  # pylint: disable=broad-except
                error = caught
            finally:
                if keepalive_task is not None:
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except asyncio.CancelledError:
                        pass

        # Fencing gate: only act on the outcome above if we still hold this
        # exact claim -- see Worker._perform (tasks/main.py) for why.
        try:
            await self.queue.discard(task_entry.entry_id, claim_token)
        except ClaimExpired:
            self._logger.warning(
                "Claim for task %s expired before we could act on its outcome "
                "(it may already have been redelivered and handled elsewhere); "
                "discarding our own result rather than risking a duplicate. If "
                "this task can run longer than the queue's visibility_timeout, "
                "pass keepalive_every= to AsyncWorker() or raise visibility_timeout.",
                task_entry.entry_id,
            )
            return False

        if succeeded:
            await self.queue.respond(entry_id, {"result": result})
            return True

        return await self._fail(entry_id, data, error, fatal=fatal)

    async def work(self) -> NoReturn:
        """Run tasks forever."""
        self._logger.info("AsyncWorker starting on queue `%s`.", self.queue.name)

        while True:
            task_entry = await self.queue.get()

            if task_entry is None:
                await asyncio.sleep(self.poll_every.total_seconds())
            else:
                await self._perform(task_entry)

    async def work_one(self, block: bool = True) -> None:
        """Run exactly one task.

        Args:
            block (bool): whether to wait until a task is available, or
                return immediately if none is available.
        """
        while True:
            task_entry = await self.queue.get()

            if task_entry is not None:
                await self._perform(task_entry)
                return

            if block:
                await asyncio.sleep(self.poll_every.total_seconds())
            else:
                break
