"""Sync mirror of [flows.aio][alchemical_queues.flows.aio] -- built on
[AlchemicalTaskQueue][alchemical_queues.AlchemicalTaskQueue] instead of its
async equivalent. See that module's docstring for the durable-execution
design (replay + step memoization) and why suspension doesn't actually need
asyncio to avoid blocking a worker: a flow that isn't ready to progress
raises instead of sleeping, so `FlowWorker.work()`'s loop moves on to the
next queue entry exactly like `AsyncFlowWorker.work()` does, just without an
event loop backing it.

The one real difference: a sync `FlowWorker` is one thread doing one claim at
a time, so where the async worker gets to interleave many suspended flows on
one loop "for free", getting the same concurrency here means running
multiple `FlowWorker` instances (threads/processes) against the same queue,
same as you would for [tasks.Worker][alchemical_queues.tasks.Worker] today.

Give `FlowWorker` a `task_queue` and a `task`-decorated function can be
called directly inside a flow, like a plain function, instead of going
through `ctx.run_task()` explicitly -- see
[flows.aio][alchemical_queues.flows.aio]'s "Calling a task like a plain
function" for the async version of the same sugar; the only difference is
there's no `await` here, the call itself resolves (or suspends, or raises)
synchronously.
"""

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

from typing_extensions import Concatenate, ParamSpec

from ..main import AlchemicalEntry, AlchemicalTaskQueue, ClaimExpired
from ..tasks.main import Task, Tasker, TaskException, _current_flow_runner

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

    def sleep_until(self, name: str, when: datetime) -> None:
        """See [flows.aio.FlowContext.sleep_until][alchemical_queues.flows.aio.FlowContext.sleep_until]."""

        def check() -> Optional[bool]:
            return True if datetime.now() >= when else None

        retry_in = max(timedelta(0), when - datetime.now())
        self.wait_for(name, check, retry_in=retry_in)

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

        Builds the `Task` via `task.get_handler()` rather than calling
        `task(*args, **kwargs)` directly -- a flow function is already
        running with the implicit-task-call sugar active (see module
        docstring), so a plain call here would recurse into that instead of
        the explicit scheduling this method is for.
        """
        return self._run_scheduled_task(
            name,
            Task(task.get_handler(), *args, **kwargs),
            on_queue=on_queue,
            poll_every=poll_every,
        )

    def _run_scheduled_task(
        self,
        name: str,
        task: "Task[Any, RValue]",
        *,
        on_queue: AlchemicalTaskQueue,
        poll_every: timedelta,
    ) -> RValue:
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

            queued = task.schedule(on_queue)
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

    def _run_called_task(self, task: "Task[Any, RValue]") -> RValue:
        # Entry point for `some_task(...)` called directly inside a flow --
        # see FlowWorker._perform, which registers this as the tasks.main
        # contextvar runner for the duration of the flow call.
        if self.task_queue is None:
            raise RuntimeError(
                f"`{task.name}(...)` was called directly inside a flow, but "
                "this FlowWorker has no task_queue configured -- pass one "
                "to FlowWorker(...), or call "
                "`ctx.run_task(name, task, ..., on_queue=...)` explicitly."
            )
        name = self._next_auto_step_name(task.name)
        return self._run_scheduled_task(
            name, task, on_queue=self.task_queue, poll_every=timedelta(seconds=1)
        )


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
    """See [flows.aio.AsyncFlow][alchemical_queues.flows.aio.AsyncFlow]."""

    def __init__(
        self,
        handler: Callable[Concatenate[FlowContext, Param], RValue],
        *args: Param.args,
        **kwargs: Param.kwargs,
    ):
        self._handler = handler
        self._args = args
        self._kwargs = kwargs

    def schedule(
        self,
        on_queue: AlchemicalTaskQueue,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
    ) -> FlowHandle[RValue]:
        """See [flows.aio.AsyncFlow.schedule][alchemical_queues.flows.aio.AsyncFlow.schedule]."""
        name = f"{self._handler.__module__}.{self._handler.__qualname__}"
        entry = on_queue.put(
            {
                "flow": name,
                "args": self._args,
                "kwargs": self._kwargs,
            },
            schedule_at=schedule_at,
            priority=priority,
        )
        return FlowHandle(on_queue, entry.entry_id)


class Flower(Generic[Param, RValue]):
    """See [flows.aio.AsyncFlower][alchemical_queues.flows.aio.AsyncFlower]."""

    def __init__(self, handler: Callable[Concatenate[FlowContext, Param], RValue]):
        self._handler = handler

    def __call__(
        self, *args: Param.args, **kwargs: Param.kwargs
    ) -> Flow[Param, RValue]:
        return Flow(self._handler, *args, **kwargs)

    def get_handler(self) -> Callable[Concatenate[FlowContext, Param], RValue]:
        """Retrieve the original flow function."""
        return self._handler


def flow(
    function: Callable[Concatenate[FlowContext, Param], RValue],
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

        runner_token = _current_flow_runner.set(ctx._run_called_task)
        try:
            func = flower.get_handler()
            result = func(ctx, *data["args"], **data["kwargs"])
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
            _current_flow_runner.reset(runner_token)
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
