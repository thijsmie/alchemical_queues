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
or a reclaim after a crash, or a reclaim after `wait_for`/`until`
voluntarily suspended it). What makes this cheap rather than wasteful is that
every side-effecting or non-deterministic call inside the flow goes through
`step()` (or `wait_for`/`run_task`/`until`/`now`/`random`/`randint`, all
built on it -- these are the module-level free functions described below,
thin wrappers over `FlowContext`'s methods of the same names), which:

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
on every replay given the same step results, so don't call the real
`datetime.now()`/`random.random()` directly in a flow body, outside of a
step -- use this module's `now()`/`random()`/`randint()` instead, which
memoize their result the same way `until()` memoizes its deadline.

## How suspension works, without blocking a worker

`wait_for(name, check)` calls `check()` once. If it returns a real value,
that's recorded as the step's result, same as `step()`. If it returns `None`
(not ready yet), `wait_for` raises `FlowSuspended` instead of looping or
sleeping in-process -- [AsyncFlowWorker][] catches that, re-enqueues the
*same logical flow* with `schedule_at` set to the requested retry time, and
immediately moves on to the next entry in its `work()` loop. That re-enqueued
entry is a *new* queue row (same trick [tasks.aio.AsyncWorker][]'s retry path
already uses for retries: carry the stable id forward in `data`, let the
physical `entry_id` change) -- but it replays against the same accumulated
step history, so resuming costs nothing beyond the suspended check itself.

`run_task()`, `until()`, and calling a nested `@async_flow` (below) are
all just `wait_for` with a ready-made `check`.

## A task stays a task

Calling an `async_task`-decorated function inside a flow works exactly like
calling it anywhere else: you get back an `AsyncTask` to `.schedule(queue)`
yourself, or feed to `run_task()` below if you want it scheduled and durably
waited on as one step. There's deliberately no magic that makes
`await charge_payment(amount)` alone schedule anything -- a task is a task,
in or out of a flow; only `run_task()`, or calling another *flow*, get the
durable-wait treatment:

    @async_task
    async def charge_payment(info, amount): ...

    @async_flow
    async def order_flow(amount):
        charge = await run_task("charge", charge_payment, amount)
        ...

`run_task(name, some_task, *args, on_queue=...)` schedules `some_task` the
first time this step is reached (itself memoized as a step, so it's only
ever scheduled once) and suspends the flow until it completes, polling via
`.done()`. `on_queue` defaults to the worker's configured `task_queue`; pass
it explicitly for a non-default queue. It must be a *different* queue from
the flow's own (`AsyncFlowWorker.get()` claims whatever entry is next
regardless of shape, so a task entry sitting in the flow queue -- or a flow
entry sitting in the task queue -- would get claimed and crash the wrong
worker).

## Calling a flow from a flow gets the magic instead

A *nested* `@async_flow` call is where the durable-wait sugar lives: calling
one flow from inside another schedules the sub-flow and durably awaits its
final result, with no `run_task`-style wrapping needed, because a flow entry
is shaped exactly like any other flow entry and can run on the *same* flow
queue under the *same* `AsyncFlowWorker` pool -- no separate queue needed,
unlike tasks:

    @async_flow
    async def charge_and_notify(amount, email): ...

    @async_flow
    async def order_flow(amount, email):
        receipt = await charge_and_notify(amount, email)   # durably awaited
        ...

This is sugar over scheduling the sub-flow on `current_context().queue` and
waiting for its "final" response, under an automatically assigned step name
(same incrementing-counter scheme `run_task`'s implicit task-call sugar used
to use -- deterministic as long as the flow reaches its nested-flow calls in
the same order every replay). A sub-flow that raises surfaces as
`FlowFailed` here, so the parent can catch, compensate, or let it propagate.

Outside a flow (no `AsyncFlowWorker` currently executing), awaiting a flow
call this way raises `RuntimeError` -- schedule it the normal way with
`.schedule(queue)` instead.

## No context parameter either

A flow function takes exactly the arguments it was called with -- no leading
`ctx`. `step()`, `wait_for()`, `until()`, `run_task()`, `now()`,
`random()`, and `randint()` are plain module-level functions here that find
the flow currently executing (via a `contextvars.ContextVar`
`AsyncFlowWorker._perform` sets for the duration of the call, the same
mechanism the nested-flow sugar above uses) rather than needing it threaded
through every call. `current_context()` is the escape hatch if you need the
lower-level `FlowContext` object directly (its `.queue`/`.flow_id`, or
`.until()`, which works the same as its free-function counterpart, just
found via this object instead); ordinary flow code shouldn't need it. All of
these raise `RuntimeError` outside a flow.

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
import contextvars
import random as _random
from datetime import datetime, timedelta
from logging import getLogger
from pydoc import locate
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generator,
    Generic,
    NoReturn,
    Optional,
    TypeVar,
    Union,
    cast,
)

from typing_extensions import ParamSpec

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


class FlowFailed(Exception):
    """Raised when a nested `@async_flow` call (awaited directly, see the
    module docstring's "Calling a flow from a flow") finished with an error,
    so the parent flow can `except` it, retry, compensate, or let it
    propagate and fail in turn."""

    def __init__(self, message: str, error_type: Union[str, None] = None):
        super().__init__(message)
        self.message = message
        self.error_type = error_type


# Set by AsyncFlowWorker for the duration of a flow function's call, so the
# free functions below (and the nested-flow-call sugar) can find the
# FlowContext for the flow currently executing without it being threaded
# through as a parameter. See the module docstring, "No context parameter
# either".
_current_context: "contextvars.ContextVar[Optional[FlowContext]]" = (
    contextvars.ContextVar("alchemical_queues_current_flow_context", default=None)
)

# Set alongside _current_context, used only by AsyncFlow.__await__ -- see
# "Calling a flow from a flow" in the module docstring. Kept separate from
# tasks.aio (which has no equivalent any more: a task stays a task) so that
# awaiting a *task* directly is a plain Python TypeError, not something this
# module tries to interpret.
_current_subflow_runner: "contextvars.ContextVar[Optional[Callable[[AsyncFlow], Awaitable[Any]]]]" = contextvars.ContextVar(
    "alchemical_queues_current_subflow_runner", default=None
)


def current_context() -> "FlowContext":
    """The `FlowContext` for the flow currently executing. An escape hatch
    for the rare case you need it directly (`.queue`, `.flow_id`,
    `.until()`) -- ordinary flow code uses
    `step()`/`wait_for()`/`until()`/`run_task()`/`now()`/`random()`
    instead and never needs this.

    Raises:
        RuntimeError: if called outside an `async_flow` function.
    """
    ctx = _current_context.get()
    if ctx is None:
        raise RuntimeError("current_context() called outside an @async_flow function")
    return ctx


async def step(
    name: str, fn: Callable[..., Awaitable[RValue]], *args: Any, **kwargs: Any
) -> RValue:
    """See `FlowContext.step` -- same thing, found via `current_context()`
    so it reads as a plain function call inside a flow."""
    return await current_context().step(name, fn, *args, **kwargs)


async def wait_for(
    name: str,
    check: Callable[[], Awaitable[Optional[RValue]]],
    *,
    retry_in: timedelta = DEFAULT_RETRY_IN,
) -> RValue:
    """See `FlowContext.wait_for`."""
    return await current_context().wait_for(name, check, retry_in=retry_in)


async def until(
    when: Union[timedelta, datetime], *, name: Union[str, None] = None
) -> None:
    """Suspend the current flow until `when` -- a `timedelta` (relative to
    the moment this is first reached) or a `datetime` (an absolute wake
    time) -- see `FlowContext.until`."""
    await current_context().until(when, name=name)


async def run_task(
    name: str,
    task: "AsyncTasker[Any, RValue]",
    *args: Any,
    on_queue: Union[AsyncAlchemicalTaskQueue, None] = None,
    poll_every: timedelta = timedelta(seconds=1),
    **kwargs: Any,
) -> RValue:
    """See `FlowContext.run_task`. `on_queue` defaults to the current flow's
    configured `task_queue` -- pass it explicitly for a non-default queue."""
    ctx = current_context()
    resolved_queue = on_queue if on_queue is not None else ctx.task_queue
    if resolved_queue is None:
        raise RuntimeError(
            f"run_task({name!r}, ...) needs an `on_queue` -- this "
            "AsyncFlowWorker has no task_queue configured and none was "
            "passed explicitly."
        )
    return await ctx.run_task(
        name, task, *args, on_queue=resolved_queue, poll_every=poll_every, **kwargs
    )


async def now(*, name: Union[str, None] = None) -> datetime:
    """`datetime.now()`, but memoized as a step the first time it's reached
    so every replay of this flow sees the same value -- see `FlowContext.now`."""
    return await current_context().now(name=name)


async def random(*, name: Union[str, None] = None) -> float:
    """`random.random()`, but memoized as a step -- see
    `FlowContext.random`."""
    return await current_context().random(name=name)


async def randint(a: int, b: int, *, name: Union[str, None] = None) -> int:
    """`random.randint(a, b)`, but memoized as a step -- see
    `FlowContext.randint`."""
    return await current_context().randint(a, b, name=name)


class FlowContext:
    """Holds one flow run's accumulated step history and the queues it needs
    -- the implementation behind the free functions above, which is what
    ordinary flow code uses instead of this directly. Not constructed by the
    user -- [AsyncFlowWorker][] builds one per claim, pre-loaded with
    whatever steps earlier runs of this same flow already completed, and
    makes it reachable via `current_context()` for the duration of the call.
    """

    def __init__(
        self,
        queue: AsyncAlchemicalTaskQueue,
        flow_id: int,
        history: Dict[str, Any],
        task_queue: Union[AsyncAlchemicalTaskQueue, None] = None,
    ):
        self.flow_id = flow_id
        self.queue = queue
        self.task_queue = task_queue
        self._queue = queue
        self._history = history
        self._seen_this_run: set = set()
        self._auto_step_counter = 0

    def _next_auto_step_name(self, hint: str) -> str:
        # Deterministic as long as the flow reaches its auto-named calls
        # (until, now/random, nested-flow calls) in the same order every
        # replay -- same requirement step()'s caller-chosen names already
        # have.
        name = f"_auto:{self._auto_step_counter}:{hint}"
        self._auto_step_counter += 1
        return name

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

    async def until(
        self, when: Union[timedelta, datetime], *, name: Union[str, None] = None
    ) -> None:
        """Suspend this flow until `when` has passed.

        `when` can be:

        - a `timedelta`, relative to the moment this step is first reached.
          The deadline is computed once and memoized as a step
          (`f"{name}:deadline"`) before the suspend check uses it, so a
          replay reuses the original deadline rather than computing a new
          one -- deterministic by construction.
        - a `datetime`, an absolute wake time. **Not** memoized itself --
          it's the caller's job to make sure computing it was deterministic
          (e.g. via `now()`/`step()` on first use, never a freshly-computed
          `datetime.now() + ...` passed straight in, which would pick a new
          deadline on every replay).

        Memoized like any other step, so once the deadline has passed,
        replays skip straight past it.
        """
        step_name = name if name is not None else self._next_auto_step_name("until")

        if isinstance(when, timedelta):

            async def compute_deadline() -> datetime:
                return datetime.now() + when

            deadline = await self.step(f"{step_name}:deadline", compute_deadline)
        else:
            deadline = when

        async def check() -> Optional[bool]:
            return True if datetime.now() >= deadline else None

        retry_in = max(timedelta(0), deadline - datetime.now())
        await self.wait_for(step_name, check, retry_in=retry_in)

    async def now(self, *, name: Union[str, None] = None) -> datetime:
        """The real `datetime.now()`, called and memoized exactly once (on
        whichever replay first reaches this step) -- safe to call from
        anywhere in a flow body, unlike the stdlib `datetime.now()`, which
        would return a different value on every replay and break
        determinism (see the module docstring)."""
        step_name = name if name is not None else self._next_auto_step_name("now")

        async def compute() -> datetime:
            return datetime.now()

        return await self.step(step_name, compute)

    async def random(self, *, name: Union[str, None] = None) -> float:
        """`random.random()`, memoized like `now()` -- safe to call from
        anywhere in a flow body."""
        step_name = name if name is not None else self._next_auto_step_name("random")

        async def compute() -> float:
            return _random.random()

        return await self.step(step_name, compute)

    async def randint(self, a: int, b: int, *, name: Union[str, None] = None) -> int:
        """`random.randint(a, b)`, memoized like `now()` -- safe to call
        from anywhere in a flow body."""
        step_name = name if name is not None else self._next_auto_step_name("randint")

        async def compute() -> int:
            return _random.randint(a, b)

        return await self.step(step_name, compute)

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

    async def _run_awaited_subflow(self, sub: "AsyncFlow[Any, RValue]") -> RValue:
        # Entry point for `await some_flow(...)` used directly inside a flow
        # -- see AsyncFlowWorker._perform, which registers this as the
        # _current_subflow_runner for the duration of the flow call. Unlike
        # tasks, a nested flow is scheduled on this flow's own queue: a flow
        # entry is shaped the same regardless of nesting, so the same
        # AsyncFlowWorker pool can claim and run it.
        scheduled_key = self._next_auto_step_name(f"{sub.name}:scheduled")

        if scheduled_key in self._history:
            sub_flow_id = cast(int, self._history[scheduled_key])
        else:
            handle = await sub.schedule(self.queue)
            sub_flow_id = handle.flow_id
            await self._queue.respond(
                self.flow_id, {"step": scheduled_key, "result": sub_flow_id}
            )
            self._history[scheduled_key] = sub_flow_id

        async def check() -> Optional[Dict[str, Any]]:
            for response in await self.queue.responses(sub_flow_id):
                if isinstance(response.data, dict) and response.data.get("final"):
                    return {"data": response.data}
            return None

        name = self._next_auto_step_name(f"{sub.name}:result")
        wrapped = await self.wait_for(name, check, retry_in=timedelta(seconds=1))
        data = wrapped["data"]

        if "error" in data:
            raise FlowFailed(data["error"], data.get("error_type"))

        return cast(RValue, data.get("result"))


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
        handler: Callable[Param, Awaitable[RValue]],
        *args: Param.args,
        **kwargs: Param.kwargs,
    ):
        self._handler = handler
        self._args = args
        self._kwargs = kwargs

    @property
    def name(self) -> str:
        """The dotted path this flow will be registered and looked up under
        -- the same value `schedule()` stores as `data["flow"]`."""
        return f"{self._handler.__module__}.{self._handler.__qualname__}"

    def __await__(self) -> Generator[Any, None, RValue]:
        """Lets a flow called inside another `async_flow` function be
        awaited directly (`receipt = await charge_and_notify(amount, email)`)
        -- it's scheduled on the current flow's own queue and durably
        awaited, under an automatically assigned step name. See the module
        docstring's "Calling a flow from a flow".

        Raises:
            RuntimeError: if awaited outside an `async_flow` function -- use
                `.schedule(queue)` there instead, there's no running flow to
                attach this to.
        """
        runner = _current_subflow_runner.get()
        if runner is None:
            raise RuntimeError(
                f"`{self.name}(...)` was awaited directly, but that only "
                "works inside an @async_flow function. Outside a flow, "
                "call `.schedule(queue)` on it instead."
            )
        return runner(self).__await__()

    async def schedule(
        self,
        on_queue: AsyncAlchemicalTaskQueue,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
    ) -> AsyncFlowHandle[RValue]:
        """Schedule this flow to run on `on_queue`. You are expected to run
        an `AsyncFlowWorker` connected to the same queue."""
        entry = await on_queue.put(
            {
                "flow": self.name,
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

    def __init__(self, handler: Callable[Param, Awaitable[RValue]]):
        self._handler = handler

    def __call__(
        self, *args: Param.args, **kwargs: Param.kwargs
    ) -> AsyncFlow[Param, RValue]:
        return AsyncFlow(self._handler, *args, **kwargs)

    def get_handler(self) -> Callable[Param, Awaitable[RValue]]:
        """Retrieve the original async flow function."""
        return self._handler


def async_flow(
    function: Callable[Param, Awaitable[RValue]],
) -> AsyncFlower[Param, RValue]:
    """Decorator to turn an `async def` function into a runnable flow, for
    use with [AsyncFlowWorker][]. The function takes exactly the arguments
    it's called with -- no framework parameter -- and routes every
    non-deterministic or side-effecting operation through `step`,
    `wait_for`, `until`, `run_task`, `now`/`random`/`randint`, or an
    awaited nested `async_flow` call, all module-level free functions here
    -- see the module docstring for why and how they find the running flow
    without it being passed in.
    """
    return AsyncFlower[Param, RValue](function)


class AsyncFlowWorker:
    """Claims and runs flows from a queue, mirroring
    [AsyncWorker][alchemical_queues.tasks.aio.AsyncWorker]'s shape but with a
    replay-and-suspend execution model instead of run-to-completion -- see
    the module docstring.

    Attributes:
        queue (AsyncAlchemicalTaskQueue): the queue this worker runs on.
            Nested flow calls are scheduled and run here too -- never a
            separate queue, unlike tasks.
        task_queue (AsyncAlchemicalTaskQueue | None): the default queue
            `run_task()` schedules on when not given an explicit `on_queue`.
            Must be a different queue from `queue`.
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
        task_queue: Union[AsyncAlchemicalTaskQueue, None] = None,
        keepalive_every: Union[timedelta, None] = None,
    ):
        self.queue = queue
        self.task_queue = task_queue
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
        ctx = FlowContext(self.queue, flow_id, history, task_queue=self.task_queue)

        keepalive_task: Union["asyncio.Task[None]", None] = None
        if self.keepalive_every is not None:
            keepalive_task = asyncio.create_task(self._keepalive_loop(flow_entry))

        suspended: Union[FlowSuspended, None] = None
        succeeded = False
        result: Any = None
        error: Union[BaseException, None] = None

        context_token = _current_context.set(ctx)
        runner_token = _current_subflow_runner.set(ctx._run_awaited_subflow)
        try:
            func = flower.get_handler()
            result = await func(*data["args"], **data["kwargs"])
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
            _current_context.reset(context_token)
            _current_subflow_runner.reset(runner_token)
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
