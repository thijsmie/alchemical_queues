"""Prototype: durable execution flows on top of
[AsyncAlchemicalTaskQueue][alchemical_queues.aio.AsyncAlchemicalTaskQueue].

A "flow" is an `async def` function that can contain multiple suspension
points (waiting on a sub-task, waiting until a point in time, waiting on an
arbitrary condition) without ever holding a worker hostage for the duration
of the wait, and without losing progress if the process running it crashes
or is restarted.

This is NOT built on top of [tasks.aio][alchemical_queues.tasks.aio] -- a
flow function is not a task handler, [AsyncFlowWorker][] is not an
[AsyncWorker][alchemical_queues.tasks.aio.AsyncWorker]. It's a separate,
parallel runner over the same [AsyncAlchemicalTaskQueue][] primitive, because
a flow's execution model (replay + step memoization, below) is different
enough from a task's (run once to completion) that forcing them to share a
worker loop would make both harder to read.

## How durability works here, deterministic replay

Nothing about Python coroutines survives a process crash -- there is no
"paused stack" to pickle and resume. So instead of trying to literally
suspend and resume a running coroutine, a flow function is *re-run from the
top* every time its queue entry is claimed (whether that's the first claim,
or a reclaim after a crash, or a reclaim after `ctx.wait_for`/`ctx.sleep_until`
voluntarily suspended it). What makes this cheap rather than wasteful is that
every side-effecting or non-deterministic call inside the flow goes through
`FlowContext.step()` (or `wait_for`/`run_task`/`sleep_until`, all built on
it), which:

- the *first* time a given step `name` is reached, actually does the work and
  durably records its result (as a response row against the flow's stable
  `flow_id` -- see below) before letting the flow function continue past it;
- on every later replay, sees that `name` already has a recorded result and
  returns it immediately *without re-running the step*.

So replaying only ever re-executes the (cheap, in-memory) control flow
between steps; every step itself runs exactly once. This is the same idea
Temporal/DBOS call "durable execution", just without the bytecode-level
determinism enforcement those use -- here, determinism is the programmer's
responsibility: a flow function must reach the *same sequence of step names*
on every replay given the same step results, so don't branch on `random()`,
`datetime.now()`, etc. outside of a step.

## How suspension works, without blocking a worker

`ctx.wait_for(name, check)` calls `check()` once. If it returns a real value,
that's recorded as the step's result, same as `step()`. If it returns `None`
(not ready yet), `wait_for` raises `FlowSuspended` instead of looping or
sleeping in-process -- [AsyncFlowWorker][] catches that, re-enqueues the
*same logical flow* with `schedule_at` set to the requested retry time, and
immediately moves on to the next entry in its `work()` loop. That re-enqueued
entry is a *new* queue row (same trick [tasks.aio.AsyncWorker][]'s retry path
already uses for retries: carry the stable id forward in `data`, let the
physical `entry_id` change) -- but it replays against the same accumulated
step history, so resuming costs nothing beyond the suspended check itself.

`ctx.run_task()` and `ctx.sleep_until()` are both just `wait_for` with a
ready-made `check`: run_task schedules a sub-task (itself memoized as a step,
so it's only ever scheduled once) and polls its `.done()`; sleep_until checks
`datetime.now()` against a target.

## Why async first

None of the above actually requires asyncio -- `wait_for` raising to let the
worker move to the next entry would work exactly the same way with a plain
synchronous `Worker.work()` loop (see `alchemical_queues.flows.main` for that
mirror). What asyncio buys here is that a *single* `AsyncFlowWorker.work()`
loop, run as one task among many on one event loop, can service a large
number of concurrently-suspended flows without a thread per flow -- the
`poll_every` sleep between empty `get()`s is the only time it isn't making
progress on *something*.
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
    Optional,
    TypeVar,
    Union,
    cast,
)

from typing_extensions import Concatenate, ParamSpec

from ..aio import AsyncAlchemicalTaskQueue
from ..main import AlchemicalEntry, ClaimExpired
from ..tasks.aio import AsyncTasker
from ..tasks.main import TaskException

Param = ParamSpec("Param")
RValue = TypeVar("RValue")

DEFAULT_RETRY_IN = timedelta(seconds=5)


class FlowSuspended(Exception):
    """Raised by `FlowContext.wait_for` (and anything built on it) to signal
    that this flow run cannot make further progress right now. Caught by
    [AsyncFlowWorker][] -- never meant to escape a flow function to the
    caller of `.schedule()`."""

    def __init__(self, retry_in: timedelta):
        super().__init__(f"flow suspended, retry in {retry_in}")
        self.retry_in = retry_in


class FlowTaskFailed(Exception):
    """Raised by `FlowContext.run_task` when the sub-task it was waiting on
    finished with an error, so the flow function can `except` it, retry,
    compensate, or just let it propagate and fail the flow."""

    def __init__(self, task_exception: TaskException):
        super().__init__(str(task_exception))
        self.task_exception = task_exception


class FlowContext:
    """Passed as the first argument to every flow function. Not constructed
    by the user -- [AsyncFlowWorker][] builds one per claim, pre-loaded with
    whatever steps earlier runs of this same flow already completed.
    """

    def __init__(
        self,
        queue: AsyncAlchemicalTaskQueue,
        flow_id: int,
        history: Dict[str, Any],
    ):
        self.flow_id = flow_id
        self.queue = queue
        self._queue = queue
        self._history = history
        self._seen_this_run: set = set()

    async def step(
        self,
        name: str,
        fn: Callable[..., Awaitable[RValue]],
        *args: Any,
        **kwargs: Any,
    ) -> RValue:
        """Run `fn(*args, **kwargs)` and durably record its result under
        `name`, exactly once across every replay of this flow -- a later
        replay that reaches `step(name, ...)` again gets the recorded result
        back without calling `fn` at all.

        `name` must be unique within one flow function and, crucially,
        stable across replays: it identifies this step in the durable
        history, not just within one call.
        """
        if name in self._history:
            return cast(RValue, self._history[name])

        if name in self._seen_this_run:
            raise RuntimeError(
                f"step {name!r} reached twice in the same flow run -- step "
                "names must be unique within a flow function"
            )
        self._seen_this_run.add(name)

        result = await fn(*args, **kwargs)
        await self._queue.respond(self.flow_id, {"step": name, "result": result})
        self._history[name] = result
        return result

    async def wait_for(
        self,
        name: str,
        check: Callable[[], Awaitable[Optional[RValue]]],
        *,
        retry_in: timedelta = DEFAULT_RETRY_IN,
    ) -> RValue:
        """Call `check()` once. A non-`None` return is treated as "ready" and
        durably recorded under `name`, same as `step()`. A `None` return
        means "not ready yet" -- this raises `FlowSuspended(retry_in)`
        instead of looping, so the worker can requeue this flow for
        `retry_in` from now and go serve other work in the meantime.

        Like `step()`, a replay that finds `name` already recorded returns it
        immediately without calling `check()` again.
        """
        if name in self._history:
            return cast(RValue, self._history[name])

        value = await check()
        if value is None:
            raise FlowSuspended(retry_in)

        await self._queue.respond(self.flow_id, {"step": name, "result": value})
        self._history[name] = value
        return cast(RValue, value)

    async def sleep_until(self, name: str, when: datetime) -> None:
        """Suspend this flow until `when`. Memoized like any other step, so
        once `when` has passed, replays skip straight past it."""

        async def check() -> Optional[bool]:
            return True if datetime.now() >= when else None

        retry_in = max(timedelta(0), when - datetime.now())
        await self.wait_for(name, check, retry_in=retry_in)

    async def run_task(
        self,
        name: str,
        task: "AsyncTasker[Any, RValue]",
        *args: Any,
        on_queue: AsyncAlchemicalTaskQueue,
        poll_every: timedelta = timedelta(seconds=1),
        **kwargs: Any,
    ) -> RValue:
        """Schedule `task(*args, **kwargs)` on `on_queue` the first time this
        step is reached, then suspend this flow until it completes. A failed
        sub-task raises `FlowTaskFailed` here (on every replay that reaches
        this point, consistently -- the sub-task's own response doesn't
        change).

        `on_queue` must be a queue serviced by a plain
        [AsyncWorker][alchemical_queues.tasks.aio.AsyncWorker] -- **never**
        `ctx.queue` (this flow's own queue). `AsyncFlowWorker.get()` claims
        whatever entry is next regardless of shape, so a task entry sitting
        in the flow queue would get claimed and crash as badly-shaped flow
        data; a flow entry sitting in the task queue would equally confuse
        an `AsyncWorker`. Flows and the tasks they schedule always need
        separate queues.
        """
        scheduled_key = f"{name}:scheduled"

        if scheduled_key in self._history:
            entry_id = cast(int, self._history[scheduled_key])
        else:
            if scheduled_key in self._seen_this_run:
                raise RuntimeError(
                    f"step {name!r} reached twice in the same flow run -- step "
                    "names must be unique within a flow function"
                )
            self._seen_this_run.add(scheduled_key)

            queued = await task(*args, **kwargs).schedule(on_queue)
            entry_id = queued.entry_id
            await self._queue.respond(
                self.flow_id, {"step": scheduled_key, "result": entry_id}
            )
            self._history[scheduled_key] = entry_id

        async def check() -> Optional[Dict[str, Any]]:
            responses = await on_queue.responses(entry_id)
            if not responses:
                return None
            return {"data": responses[0].data}

        wrapped = await self.wait_for(name, check, retry_in=poll_every)
        data = wrapped["data"]

        if isinstance(data, dict) and "error" in data:
            raise FlowTaskFailed(TaskException(data["error"], data.get("error_type")))

        return cast(RValue, data.get("result") if isinstance(data, dict) else data)


class AsyncFlowHandle(Generic[RValue]):
    """Represents a flow run in the queue. Returned by `AsyncFlow.schedule`,
    mirroring [AsyncQueuedTask][alchemical_queues.tasks.aio.AsyncQueuedTask].

    Attributes:
        flow_id (int): the stable id identifying this flow run -- every
            response row recorded against it (steps and the final outcome
            alike) carries this id, even though the physical queue entry
            backing an in-progress flow is re-created (with a new
            `entry_id`) on every suspend/resume.
    """

    def __init__(self, queue: AsyncAlchemicalTaskQueue, flow_id: int):
        self._queue = queue
        self.flow_id = flow_id

    async def _final(self) -> Optional[Dict[str, Any]]:
        for response in await self._queue.responses(self.flow_id):
            if isinstance(response.data, dict) and response.data.get("final"):
                return response.data
        return None

    async def done(self) -> bool:
        """Whether this flow has finished (successfully or not) -- `True`
        does not mean every step succeeded quietly, just that there is a
        final outcome to read with `result()`."""
        return (await self._final()) is not None

    async def result(self) -> Union[RValue, TaskException, None]:
        """The flow's return value, a `TaskException` if it raised, or
        `None` if it hasn't finished yet."""
        final = await self._final()
        if final is None:
            return None
        if "error" in final:
            return TaskException(final["error"], final.get("error_type"))
        return cast(RValue, final.get("result"))


class AsyncFlow(Generic[Param, RValue]):
    """A flow call not yet scheduled. Returned by calling an `async_flow`
    decorated function, mirroring
    [AsyncTask][alchemical_queues.tasks.aio.AsyncTask]."""

    def __init__(
        self,
        handler: Callable[Concatenate[FlowContext, Param], Awaitable[RValue]],
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
    ) -> AsyncFlowHandle[RValue]:
        """Schedule this flow to run on `on_queue`. You are expected to run
        an `AsyncFlowWorker` connected to the same queue."""
        name = f"{self._handler.__module__}.{self._handler.__qualname__}"
        entry = await on_queue.put(
            {
                "flow": name,
                "args": self._args,
                "kwargs": self._kwargs,
            },
            schedule_at=schedule_at,
            priority=priority,
        )
        return AsyncFlowHandle(on_queue, entry.entry_id)


class AsyncFlower(Generic[Param, RValue]):
    """Container for a schedulable flow handler, mirroring
    [AsyncTasker][alchemical_queues.tasks.aio.AsyncTasker]."""

    def __init__(
        self, handler: Callable[Concatenate[FlowContext, Param], Awaitable[RValue]]
    ):
        self._handler = handler

    def __call__(
        self, *args: Param.args, **kwargs: Param.kwargs
    ) -> AsyncFlow[Param, RValue]:
        return AsyncFlow(self._handler, *args, **kwargs)

    def get_handler(
        self,
    ) -> Callable[Concatenate[FlowContext, Param], Awaitable[RValue]]:
        """Retrieve the original async flow function."""
        return self._handler


def async_flow(
    function: Callable[Concatenate[FlowContext, Param], Awaitable[RValue]],
) -> AsyncFlower[Param, RValue]:
    """Decorator to turn an `async def` function into a runnable flow, for
    use with [AsyncFlowWorker][]. The function must take a [FlowContext][]
    as first argument, and route every non-deterministic or side-effecting
    operation through it (`ctx.step`, `ctx.wait_for`, `ctx.sleep_until`,
    `ctx.run_task`) -- see the module docstring for why.
    """
    return AsyncFlower[Param, RValue](function)


class AsyncFlowWorker:
    """Claims and runs flows from a queue, mirroring
    [AsyncWorker][alchemical_queues.tasks.aio.AsyncWorker]'s shape but with a
    replay-and-suspend execution model instead of run-to-completion -- see
    the module docstring.

    Attributes:
        queue (AsyncAlchemicalTaskQueue): the queue this worker runs on.
        poll_every (timedelta): how often to poll for new flow entries when
            the queue is empty.
        keepalive_every (timedelta | None): how often to extend a flow run's
            claim while it's actively executing (not while suspended --
            nothing holds a claim during a suspension, see below).
    """

    def __init__(
        self,
        queue: AsyncAlchemicalTaskQueue,
        poll_every: timedelta = timedelta(seconds=1),
        *,
        keepalive_every: Union[timedelta, None] = None,
    ):
        self.queue = queue
        self.poll_every = poll_every
        self.keepalive_every = keepalive_every
        self._handler_registry: Dict[str, "AsyncFlower"] = {}
        self._logger = getLogger("alchemical_queues.flows")

    async def _keepalive_loop(self, flow_entry: AlchemicalEntry) -> None:
        assert self.keepalive_every is not None
        assert flow_entry.claim_token is not None
        while True:
            await asyncio.sleep(self.keepalive_every.total_seconds())
            try:
                await self.queue.extend(flow_entry.entry_id, flow_entry.claim_token)
            except ClaimExpired:
                return

    async def _perform(self, flow_entry: AlchemicalEntry) -> None:
        assert flow_entry.claim_token is not None
        claim_token: int = flow_entry.claim_token

        data = flow_entry.data
        flow_id = data.get("flow_id", flow_entry.entry_id)
        function_path = data["flow"]

        flower = self._handler_registry.get(function_path)
        if function_path not in self._handler_registry:
            flower = self._handler_registry[function_path] = cast(
                AsyncFlower, locate(function_path)
            )

        if flower is None:
            await self.queue.discard(flow_entry.entry_id, claim_token)
            await self.queue.respond(
                flow_id,
                {
                    "final": True,
                    "error": f"flow handler `{function_path}` not found.",
                    "error_type": "KeyError",
                },
            )
            return

        history: Dict[str, Any] = {
            response.data["step"]: response.data["result"]
            for response in await self.queue.responses(flow_id)
            if isinstance(response.data, dict) and "step" in response.data
        }
        ctx = FlowContext(self.queue, flow_id, history)

        keepalive_task: Union["asyncio.Task[None]", None] = None
        if self.keepalive_every is not None:
            keepalive_task = asyncio.create_task(self._keepalive_loop(flow_entry))

        suspended: Union[FlowSuspended, None] = None
        succeeded = False
        result: Any = None
        error: Union[BaseException, None] = None

        try:
            func = flower.get_handler()
            result = await func(ctx, *data["args"], **data["kwargs"])
            succeeded = True
        except FlowSuspended as exc:
            suspended = exc
        except asyncio.CancelledError:
            try:
                await self.queue.release(flow_entry.entry_id, claim_token)
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

        if suspended is not None:
            # Carry the flow forward as a new physical entry, same trick
            # tasks.aio.AsyncWorker's retry path uses: the stable flow_id
            # travels in `data`, the queue entry_id is free to change.
            data["flow_id"] = flow_id
            try:
                await self.queue.discard(flow_entry.entry_id, claim_token)
            except ClaimExpired:
                self._logger.warning(
                    "Claim for flow %s expired before it could be "
                    "re-suspended; it may already be running elsewhere.",
                    flow_id,
                )
                return
            await self.queue.put(data, schedule_at=datetime.now() + suspended.retry_in)
            return

        try:
            await self.queue.discard(flow_entry.entry_id, claim_token)
        except ClaimExpired:
            self._logger.warning(
                "Claim for flow %s expired before we could act on its "
                "outcome; discarding our own result rather than risking a "
                "duplicate.",
                flow_id,
            )
            return

        if succeeded:
            await self.queue.respond(flow_id, {"final": True, "result": result})
            return

        self._logger.warning("Flow %s raised while running", flow_id)
        self._logger.exception(error)
        await self.queue.respond(
            flow_id,
            {
                "final": True,
                "error": str(error),
                "error_type": type(error).__name__ if error else None,
            },
        )

    async def work(self) -> NoReturn:
        """Run flows forever."""
        self._logger.info("AsyncFlowWorker starting on queue `%s`.", self.queue.name)

        while True:
            flow_entry = await self.queue.get()

            if flow_entry is None:
                await asyncio.sleep(self.poll_every.total_seconds())
            else:
                await self._perform(flow_entry)

    async def work_one(self, block: bool = True) -> None:
        """Run exactly one flow claim -- note this is one *claim*, not one
        flow to completion: a flow that suspends returns here having made
        partial progress, same as it would inside `work()`.

        Args:
            block (bool): whether to wait until an entry is available, or
                return immediately if none is available.
        """
        while True:
            flow_entry = await self.queue.get()

            if flow_entry is not None:
                await self._perform(flow_entry)
                return

            if block:
                await asyncio.sleep(self.poll_every.total_seconds())
            else:
                break
