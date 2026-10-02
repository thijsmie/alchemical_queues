"""Sync mirror of [flows.aio][alchemical_queues.flows.aio] -- built on
[AlchemicalTaskQueue][alchemical_queues.AlchemicalTaskQueue] instead of its
async equivalent. See that module's docstring for the durable-execution
design (replay + step memoization), why suspension doesn't actually need
asyncio to avoid blocking a worker, why a task stays a task while a nested
flow call gets the durable-wait sugar, and the `now()`/`random()`/`randint()`
determinism helpers -- all of it carries over unchanged, just synchronous.

The one real difference: a sync `FlowWorker` is one thread doing one claim at
a time, so where the async worker gets to interleave many suspended flows on
one loop "for free", getting the same concurrency here means running
multiple `FlowWorker` instances (threads/processes) against the same queue,
same as you would for [tasks.Worker][alchemical_queues.tasks.Worker] today.

A flow function takes no `ctx` parameter -- `step`/`wait_for`/`until`/
`run_task`/`now`/`random`/`randint` are module-level free functions here
too, same as `flows.aio`'s "No context parameter either". A `task`-decorated
function called inside a flow works exactly like calling it anywhere else
(use `run_task()` for the durable-wait treatment); a nested `flow`-decorated
call gets that treatment automatically, by calling it directly -- since
there's no sync `await`, `Flow.__call__` does the dynamic dispatch instead:
inside a running flow it resolves and returns the sub-flow's result
directly (or suspends, or raises), outside one it raises `TypeError` telling
you to `.schedule(queue)` instead.
"""

import contextvars
import random as _random
import threading
import time
from datetime import datetime, timedelta
from logging import getLogger
from pydoc import locate
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    NoReturn,
    Optional,
    TypeVar,
    Union,
    cast,
)

from typing_extensions import ParamSpec

from ..main import AlchemicalEntry, AlchemicalTaskQueue, ClaimExpired
from ..tasks.main import Tasker, TaskException

Param = ParamSpec("Param")
RValue = TypeVar("RValue")

DEFAULT_RETRY_IN = timedelta(seconds=5)


class FlowSuspended(Exception):
    """See [flows.aio.FlowSuspended][alchemical_queues.flows.aio.FlowSuspended]."""

    def __init__(self, retry_in: timedelta):
        super().__init__(f"flow suspended, retry in {retry_in}")
        self.retry_in = retry_in


class FlowTaskFailed(Exception):
    """See [flows.aio.FlowTaskFailed][alchemical_queues.flows.aio.FlowTaskFailed]."""

    def __init__(self, task_exception: TaskException):
        super().__init__(str(task_exception))
        self.task_exception = task_exception


class FlowFailed(Exception):
    """See [flows.aio.FlowFailed][alchemical_queues.flows.aio.FlowFailed]."""

    def __init__(self, message: str, error_type: Union[str, None] = None):
        super().__init__(message)
        self.message = message
        self.error_type = error_type


# See flows.aio._current_context / _current_subflow_runner for why these
# live here rather than being passed as parameters.
_current_context: "contextvars.ContextVar[Optional[FlowContext]]" = (
    contextvars.ContextVar("alchemical_queues_current_flow_context_sync", default=None)
)

_current_subflow_runner: "contextvars.ContextVar[Optional[Callable[['Flow'], Any]]]" = (
    contextvars.ContextVar(
        "alchemical_queues_current_subflow_runner_sync", default=None
    )
)


def current_context() -> "FlowContext":
    """See [flows.aio.current_context][alchemical_queues.flows.aio.current_context]."""
    ctx = _current_context.get()
    if ctx is None:
        raise RuntimeError("current_context() called outside a @flow function")
    return ctx


def step(name: str, fn: Callable[..., RValue], *args: Any, **kwargs: Any) -> RValue:
    """See [flows.aio.step][alchemical_queues.flows.aio.step]."""
    return current_context().step(name, fn, *args, **kwargs)


def wait_for(
    name: str,
    check: Callable[[], Optional[RValue]],
    *,
    retry_in: timedelta = DEFAULT_RETRY_IN,
) -> RValue:
    """See [flows.aio.wait_for][alchemical_queues.flows.aio.wait_for]."""
    return current_context().wait_for(name, check, retry_in=retry_in)


def until(when: Union[timedelta, datetime], *, name: Union[str, None] = None) -> None:
    """See [flows.aio.until][alchemical_queues.flows.aio.until]."""
    current_context().until(when, name=name)


def run_task(
    name: str,
    task: "Tasker[Any, RValue]",
    *args: Any,
    on_queue: Union[AlchemicalTaskQueue, None] = None,
    poll_every: timedelta = timedelta(seconds=1),
    **kwargs: Any,
) -> RValue:
    """See [flows.aio.run_task][alchemical_queues.flows.aio.run_task]."""
    ctx = current_context()
    resolved_queue = on_queue if on_queue is not None else ctx.task_queue
    if resolved_queue is None:
        raise RuntimeError(
            f"run_task({name!r}, ...) needs an `on_queue` -- this "
            "FlowWorker has no task_queue configured and none was passed "
            "explicitly."
        )
    return ctx.run_task(
        name, task, *args, on_queue=resolved_queue, poll_every=poll_every, **kwargs
    )


def now(*, name: Union[str, None] = None) -> datetime:
    """See [flows.aio.now][alchemical_queues.flows.aio.now]."""
    return current_context().now(name=name)


def random(*, name: Union[str, None] = None) -> float:
    """See [flows.aio.random][alchemical_queues.flows.aio.random]."""
    return current_context().random(name=name)


def randint(a: int, b: int, *, name: Union[str, None] = None) -> int:
    """See [flows.aio.randint][alchemical_queues.flows.aio.randint]."""
    return current_context().randint(a, b, name=name)


class FlowContext:
    """See [flows.aio.FlowContext][alchemical_queues.flows.aio.FlowContext]."""

    def __init__(
        self,
        queue: AlchemicalTaskQueue,
        flow_id: int,
        history: Dict[str, Any],
        task_queue: Union[AlchemicalTaskQueue, None] = None,
    ):
        self.flow_id = flow_id
        self.queue = queue
        self.task_queue = task_queue
        self._queue = queue
        self._history = history
        self._seen_this_run: set = set()
        self._auto_step_counter = 0

    def _next_auto_step_name(self, hint: str) -> str:
        name = f"_auto:{self._auto_step_counter}:{hint}"
        self._auto_step_counter += 1
        return name

    def step(
        self,
        name: str,
        fn: Callable[..., RValue],
        *args: Any,
        **kwargs: Any,
    ) -> RValue:
        """See [flows.aio.FlowContext.step][alchemical_queues.flows.aio.FlowContext.step]."""
        if name in self._history:
            return cast(RValue, self._history[name])

        if name in self._seen_this_run:
            raise RuntimeError(
                f"step {name!r} reached twice in the same flow run -- step "
                "names must be unique within a flow function"
            )
        self._seen_this_run.add(name)

        result = fn(*args, **kwargs)
        self._queue.respond(self.flow_id, {"step": name, "result": result})
        self._history[name] = result
        return result

    def wait_for(
        self,
        name: str,
        check: Callable[[], Optional[RValue]],
        *,
        retry_in: timedelta = DEFAULT_RETRY_IN,
    ) -> RValue:
        """See [flows.aio.FlowContext.wait_for][alchemical_queues.flows.aio.FlowContext.wait_for]."""
        if name in self._history:
            return cast(RValue, self._history[name])

        value = check()
        if value is None:
            raise FlowSuspended(retry_in)

        self._queue.respond(self.flow_id, {"step": name, "result": value})
        self._history[name] = value
        return cast(RValue, value)

    def until(
        self, when: Union[timedelta, datetime], *, name: Union[str, None] = None
    ) -> None:
        """See [flows.aio.FlowContext.until][alchemical_queues.flows.aio.FlowContext.until]."""
        step_name = name if name is not None else self._next_auto_step_name("until")

        if isinstance(when, timedelta):

            def compute_deadline() -> datetime:
                return datetime.now() + when

            deadline = self.step(f"{step_name}:deadline", compute_deadline)
        else:
            deadline = when

        def check() -> Optional[bool]:
            return True if datetime.now() >= deadline else None

        retry_in = max(timedelta(0), deadline - datetime.now())
        self.wait_for(step_name, check, retry_in=retry_in)

    def now(self, *, name: Union[str, None] = None) -> datetime:
        """See [flows.aio.FlowContext.now][alchemical_queues.flows.aio.FlowContext.now]."""
        step_name = name if name is not None else self._next_auto_step_name("now")

        def compute() -> datetime:
            return datetime.now()

        return self.step(step_name, compute)

    def random(self, *, name: Union[str, None] = None) -> float:
        """See [flows.aio.FlowContext.random][alchemical_queues.flows.aio.FlowContext.random]."""
        step_name = name if name is not None else self._next_auto_step_name("random")

        def compute() -> float:
            return _random.random()

        return self.step(step_name, compute)

    def randint(self, a: int, b: int, *, name: Union[str, None] = None) -> int:
        """See [flows.aio.FlowContext.randint][alchemical_queues.flows.aio.FlowContext.randint]."""
        step_name = name if name is not None else self._next_auto_step_name("randint")

        def compute() -> int:
            return _random.randint(a, b)

        return self.step(step_name, compute)

    def run_task(
        self,
        name: str,
        task: "Tasker[Any, RValue]",
        *args: Any,
        on_queue: AlchemicalTaskQueue,
        poll_every: timedelta = timedelta(seconds=1),
        **kwargs: Any,
    ) -> RValue:
        """See [flows.aio.FlowContext.run_task][alchemical_queues.flows.aio.FlowContext.run_task].
        `on_queue` must be a *different* queue from `ctx.queue`, serviced by
        a plain [Worker][alchemical_queues.tasks.Worker] -- never this
        flow's own queue; see the async version's docstring for why.
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

            queued = task(*args, **kwargs).schedule(on_queue)
            entry_id = queued.entry_id
            self._queue.respond(
                self.flow_id, {"step": scheduled_key, "result": entry_id}
            )
            self._history[scheduled_key] = entry_id

        def check() -> Optional[Dict[str, Any]]:
            responses = on_queue.responses(entry_id)
            if not responses:
                return None
            return {"data": responses[0].data}

        wrapped = self.wait_for(name, check, retry_in=poll_every)
        data = wrapped["data"]

        if isinstance(data, dict) and "error" in data:
            raise FlowTaskFailed(TaskException(data["error"], data.get("error_type")))

        return cast(RValue, data.get("result") if isinstance(data, dict) else data)

    def _run_called_subflow(self, sub: "Flow[Any, RValue]") -> RValue:
        # Entry point for `some_flow(...)` called directly inside a flow --
        # see FlowWorker._perform, which registers this as the
        # _current_subflow_runner for the duration of the flow call. See
        # flows.aio.FlowContext._run_awaited_subflow for the async twin.
        scheduled_key = self._next_auto_step_name(f"{sub.name}:scheduled")

        if scheduled_key in self._history:
            sub_flow_id = cast(int, self._history[scheduled_key])
        else:
            handle = sub.schedule(self.queue)
            sub_flow_id = handle.flow_id
            self._queue.respond(
                self.flow_id, {"step": scheduled_key, "result": sub_flow_id}
            )
            self._history[scheduled_key] = sub_flow_id

        def check() -> Optional[Dict[str, Any]]:
            for response in self.queue.responses(sub_flow_id):
                if isinstance(response.data, dict) and response.data.get("final"):
                    return {"data": response.data}
            return None

        name = self._next_auto_step_name(f"{sub.name}:result")
        wrapped = self.wait_for(name, check, retry_in=timedelta(seconds=1))
        data = wrapped["data"]

        if "error" in data:
            raise FlowFailed(data["error"], data.get("error_type"))

        return cast(RValue, data.get("result"))


class FlowHandle(Generic[RValue]):
    """See [flows.aio.AsyncFlowHandle][alchemical_queues.flows.aio.AsyncFlowHandle]."""

    def __init__(self, queue: AlchemicalTaskQueue, flow_id: int):
        self._queue = queue
        self.flow_id = flow_id

    def _final(self) -> Optional[Dict[str, Any]]:
        for response in self._queue.responses(self.flow_id):
            if isinstance(response.data, dict) and response.data.get("final"):
                return response.data
        return None

    @property
    def done(self) -> bool:
        """See [flows.aio.AsyncFlowHandle.done][alchemical_queues.flows.aio.AsyncFlowHandle.done]."""
        return self._final() is not None

    @property
    def result(self) -> Union[RValue, TaskException, None]:
        """See [flows.aio.AsyncFlowHandle.result][alchemical_queues.flows.aio.AsyncFlowHandle.result]."""
        final = self._final()
        if final is None:
            return None
        if "error" in final:
            return TaskException(final["error"], final.get("error_type"))
        return cast(RValue, final.get("result"))


class Flow(Generic[Param, RValue]):
    """See [flows.aio.AsyncFlow][alchemical_queues.flows.aio.AsyncFlow]. Calling
    a `Flow` directly (`.__call__`, not `.schedule()`) is what gets the
    durable-wait sugar when done inside another running flow -- the sync
    equivalent of awaiting an `AsyncFlow`, since there's no sync `await`."""

    def __init__(
        self,
        handler: Callable[Param, RValue],
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

    def schedule(
        self,
        on_queue: AlchemicalTaskQueue,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
    ) -> FlowHandle[RValue]:
        """See [flows.aio.AsyncFlow.schedule][alchemical_queues.flows.aio.AsyncFlow.schedule]."""
        entry = on_queue.put(
            {
                "flow": self.name,
                "args": self._args,
                "kwargs": self._kwargs,
            },
            schedule_at=schedule_at,
            priority=priority,
        )
        return FlowHandle(on_queue, entry.entry_id)


class Flower(Generic[Param, RValue]):
    """See [flows.aio.AsyncFlower][alchemical_queues.flows.aio.AsyncFlower]."""

    def __init__(self, handler: Callable[Param, RValue]):
        self._handler = handler

    def __call__(
        self, *args: Param.args, **kwargs: Param.kwargs
    ) -> Union[Flow[Param, RValue], RValue]:
        """Inside a running flow, calling a nested `flow`-decorated function
        resolves and returns its result directly, durably awaited (the sync
        twin of `AsyncFlow.__await__`). Outside a flow, returns a `Flow` to
        `.schedule(queue)` yourself, same as always."""
        flow_call = Flow(self._handler, *args, **kwargs)
        runner = _current_subflow_runner.get()
        if runner is None:
            return flow_call
        return runner(flow_call)

    def get_handler(self) -> Callable[Param, RValue]:
        """Retrieve the original flow function."""
        return self._handler


def flow(
    function: Callable[Param, RValue],
) -> Flower[Param, RValue]:
    """See [flows.aio.async_flow][alchemical_queues.flows.aio.async_flow]."""
    return Flower[Param, RValue](function)


class FlowWorker:
    """See [flows.aio.AsyncFlowWorker][alchemical_queues.flows.aio.AsyncFlowWorker]."""

    def __init__(
        self,
        queue: AlchemicalTaskQueue,
        poll_every: timedelta = timedelta(seconds=1),
        *,
        task_queue: Union[AlchemicalTaskQueue, None] = None,
        keepalive_every: Union[timedelta, None] = None,
    ):
        self.queue = queue
        self.task_queue = task_queue
        self.poll_every = poll_every
        self.keepalive_every = keepalive_every
        self._handler_registry: Dict[str, "Flower"] = {}
        self._logger = getLogger("alchemical_queues.flows")

    def _keepalive_loop(
        self, flow_entry: AlchemicalEntry, stop: threading.Event
    ) -> None:
        assert self.keepalive_every is not None
        assert flow_entry.claim_token is not None
        while not stop.wait(self.keepalive_every.total_seconds()):
            try:
                self.queue.extend(flow_entry.entry_id, flow_entry.claim_token)
            except ClaimExpired:
                return

    def _perform(self, flow_entry: AlchemicalEntry) -> None:
        assert flow_entry.claim_token is not None
        claim_token: int = flow_entry.claim_token

        data = flow_entry.data
        flow_id = data.get("flow_id", flow_entry.entry_id)
        function_path = data["flow"]

        flower = self._handler_registry.get(function_path)
        if function_path not in self._handler_registry:
            flower = self._handler_registry[function_path] = cast(
                Flower, locate(function_path)
            )

        if flower is None:
            self.queue.discard(flow_entry.entry_id, claim_token)
            self.queue.respond(
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
            for response in self.queue.responses(flow_id)
            if isinstance(response.data, dict) and "step" in response.data
        }
        ctx = FlowContext(self.queue, flow_id, history, task_queue=self.task_queue)

        keepalive_stop = threading.Event()
        keepalive_thread = None
        if self.keepalive_every is not None:
            keepalive_thread = threading.Thread(
                target=self._keepalive_loop,
                args=(flow_entry, keepalive_stop),
                daemon=True,
            )
            keepalive_thread.start()

        suspended: Union[FlowSuspended, None] = None
        succeeded = False
        result: Any = None
        error: Union[BaseException, None] = None

        context_token = _current_context.set(ctx)
        runner_token = _current_subflow_runner.set(ctx._run_called_subflow)
        try:
            func = flower.get_handler()
            result = func(*data["args"], **data["kwargs"])
            succeeded = True
        except FlowSuspended as exc:
            suspended = exc
        except KeyboardInterrupt:
            try:
                self.queue.release(flow_entry.entry_id, claim_token)
            except ClaimExpired:
                pass
            raise
        except Exception as caught:  # pylint: disable=broad-except
            error = caught
        finally:
            _current_context.reset(context_token)
            _current_subflow_runner.reset(runner_token)
            if keepalive_thread is not None:
                keepalive_stop.set()
                keepalive_thread.join()

        if suspended is not None:
            data["flow_id"] = flow_id
            try:
                self.queue.discard(flow_entry.entry_id, claim_token)
            except ClaimExpired:
                self._logger.warning(
                    "Claim for flow %s expired before it could be "
                    "re-suspended; it may already be running elsewhere.",
                    flow_id,
                )
                return
            self.queue.put(data, schedule_at=datetime.now() + suspended.retry_in)
            return

        try:
            self.queue.discard(flow_entry.entry_id, claim_token)
        except ClaimExpired:
            self._logger.warning(
                "Claim for flow %s expired before we could act on its "
                "outcome; discarding our own result rather than risking a "
                "duplicate.",
                flow_id,
            )
            return

        if succeeded:
            self.queue.respond(flow_id, {"final": True, "result": result})
            return

        self._logger.warning("Flow %s raised while running", flow_id)
        self._logger.exception(error)
        self.queue.respond(
            flow_id,
            {
                "final": True,
                "error": str(error),
                "error_type": type(error).__name__ if error else None,
            },
        )

    def work(self) -> NoReturn:
        """Run flows forever."""
        self._logger.info("FlowWorker starting on queue `%s`.", self.queue.name)

        while True:
            flow_entry = self.queue.get()

            if flow_entry is None:
                time.sleep(self.poll_every.total_seconds())
            else:
                self._perform(flow_entry)

    def work_one(self, block: bool = True) -> None:
        """Run exactly one flow claim. See
        [flows.aio.AsyncFlowWorker.work_one][alchemical_queues.flows.aio.AsyncFlowWorker.work_one].

        Args:
            block (bool): whether to wait until an entry is available, or
                return immediately if none is available.
        """
        while True:
            flow_entry = self.queue.get()

            if flow_entry is not None:
                self._perform(flow_entry)
                return

            if block:
                time.sleep(self.poll_every.total_seconds())
            else:
                break
