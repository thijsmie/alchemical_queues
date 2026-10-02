"""Prototype: periodic ("cron-like") tasks, built entirely on top of the
existing `AlchemicalTaskQueue` / `task` API -- no changes to the core Queue
abstraction.

A [Beat][alchemical_queues.tasks.periodic.Beat] holds a small set of
[PeriodicTask][alchemical_queues.tasks.periodic.PeriodicTask] schedules and,
on each `tick()`, enqueues (via the normal `Task.schedule()` path) whichever
ones are due. "Due" is tracked in one tiny extra table keyed by schedule
name, so running several `Beat` processes at once (for redundancy) never
double-enqueues a run: advancing a schedule's `next_run` is a conditional
`UPDATE` keyed on the exact value just read, so only one concurrent
transaction can win -- the same compare-and-swap `AlchemicalTaskQueue.get()`
already relies on to hand each entry to exactly one claimant. Each advance
adds one `every` to the schedule's own previous `next_run` (never to the
current time), so run times stay on a fixed grid anchored at `start_at`
instead of drifting later with each run.

This is intentionally minimal -- fixed-interval schedules only, no cron
expression syntax. A cron string could be layered on top later (parsed down
to "the next due datetime from here", same as `every` is now) without
changing `Beat` itself.
"""

import time
from datetime import datetime, timedelta
from typing import Callable, Dict, Generic, List, NoReturn, Union

from sqlalchemy import String, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ..main import AlchemicalTaskQueue, _datetime_column, _retry_on_deadlock
from .main import Param, RValue, Task, Tasker


class PeriodicTask(Generic[Param, RValue]):
    """A task bound to a fixed-interval schedule. Not constructed directly --
    see [periodic][alchemical_queues.tasks.periodic.periodic]."""

    def __init__(
        self,
        name: str,
        tasker: "Tasker[Param, RValue]",
        every: timedelta,
        start_at: Union[datetime, None],
        args: tuple,
        kwargs: dict,
    ) -> None:
        self.name = name
        self.every = every
        self.start_at = start_at
        self._tasker = tasker
        self._args = args
        self._kwargs = kwargs

    def _build_task(self) -> "Task[Param, RValue]":
        return Task(self._tasker.get_handler(), *self._args, **self._kwargs)


class PeriodicTaskFactory(Generic[Param, RValue]):
    """Returned by `Tasker.periodic()`; call it with the task's own arguments
    to get a [PeriodicTask][alchemical_queues.tasks.periodic.PeriodicTask],
    the same way calling a `Tasker` gives you a `Task`."""

    def __init__(
        self,
        tasker: "Tasker[Param, RValue]",
        name: str,
        every: timedelta,
        start_at: Union[datetime, None],
    ) -> None:
        self._tasker = tasker
        self.name = name
        self.every = every
        self.start_at = start_at

    def __call__(self, *args, **kwargs) -> PeriodicTask[Param, RValue]:
        return PeriodicTask(
            self.name, self._tasker, self.every, self.start_at, args, kwargs
        )


def periodic(
    tasker: "Tasker[Param, RValue]",
    *,
    name: str,
    every: timedelta,
    start_at: Union[datetime, None] = None,
) -> PeriodicTaskFactory[Param, RValue]:
    """Wrap a `@task`-decorated function as a fixed-interval schedule.

    Args:
        tasker (Tasker): a function already decorated with `@task`.
        name (str): a stable identifier for this schedule, used as the
            dedup key in `Beat`'s own table. Changing it starts a fresh
            schedule (a new row, next due immediately).
        every (timedelta): how often to enqueue this task.
        start_at (datetime | None, optional): the first run isn't due before
            this timestamp. Only takes effect the first time `Beat` sees this
            schedule's `name` (it's where the row's initial `next_run` comes
            from); a later change to `start_at` has no effect on a schedule
            that already has a row. Defaults to `None`, meaning due
            immediately, same as a past `start_at` would be.

    Returns:
        PeriodicTaskFactory: call it with the task's own arguments (exactly
            like calling `tasker` itself) to get a `PeriodicTask` to hand to
            `Beat`.
    """
    return PeriodicTaskFactory(tasker, name, every, start_at)


def _generate_schedule_model(tablename: str):
    class ScheduleBase(DeclarativeBase):
        """SQLAlchemy model base class for Beat's own schedule table."""

    class Schedule(ScheduleBase):  # type: ignore[misc,valid-type]
        __tablename__: str = tablename

        name: Mapped[str] = mapped_column(String(255), primary_key=True)
        next_run: Mapped[datetime] = mapped_column(_datetime_column(), nullable=False)

    return Schedule


class Beat:
    """Enqueues each [PeriodicTask][alchemical_queues.tasks.periodic.PeriodicTask]
    it holds onto a queue when it's due. Safe to run as several concurrent
    processes (e.g. for redundancy): due-ness and the next run time live in
    one row per schedule, advanced by a conditional `UPDATE` that only one
    concurrent `tick()` can win.
    """

    def __init__(
        self,
        engine: Engine,
        queue: AlchemicalTaskQueue,
        schedules: List[PeriodicTask],
        *,
        tablename: str = "AlchemicalSchedule",
    ) -> None:
        self.queue = queue
        self.schedules: Dict[str, PeriodicTask] = {s.name: s for s in schedules}
        self._engine = engine
        self._model = _generate_schedule_model(tablename)
        self._session: Callable[[], Session] = sessionmaker(
            engine, expire_on_commit=False
        )

    def create_all(self) -> None:
        """Create Beat's own schedule table."""
        self._model.metadata.create_all(self._engine)  # type: ignore[attr-defined]

    @_retry_on_deadlock
    def tick(self) -> int:
        """Enqueue every schedule that is currently due, and return how many
        that was. Schedules seen for the first time are due at their
        `start_at` (or immediately, if `start_at` is `None` or already past).

        Each due schedule's `next_run` advances by exactly one `every` from
        its own previous `next_run` -- the run times form a fixed grid
        anchored at `start_at` (`start_at`, `start_at + every`,
        `start_at + 2 * every`, ...) regardless of how long a `tick()` call
        or the task itself takes, rather than drifting later with each run.
        If `tick()` wasn't called for a while (the process was down, say),
        a schedule that's behind catches up one grid step per `tick()` call
        rather than jumping straight to "now" -- call `tick()` again
        immediately (as `run()` effectively does once its sleep elapses) to
        catch all the way up.
        """
        now = datetime.now()
        enqueued = 0

        with self._session() as session:
            known: set = set(
                session.scalars(
                    select(self._model.name).where(
                        self._model.name.in_(self.schedules.keys())
                    )
                )
            )
            for name in self.schedules.keys() - known:
                schedule = self.schedules[name]
                first_due = schedule.start_at if schedule.start_at is not None else now
                session.add(self._model(name=name, next_run=first_due))
            session.commit()

            next_runs: Dict[str, datetime] = dict(
                session.execute(
                    select(self._model.name, self._model.next_run).where(
                        self._model.name.in_(self.schedules.keys())
                    )
                ).all()
            )

            for schedule in self.schedules.values():
                current_next_run = next_runs[schedule.name]
                if current_next_run > now:
                    continue

                # A conditional UPDATE keyed on the exact next_run value we
                # just read acts as a compare-and-swap: if another Beat
                # process already advanced this schedule since our read,
                # this matches zero rows instead of advancing (and firing)
                # it a second time for the same grid step.
                result = session.execute(
                    update(self._model)
                    .where(
                        self._model.name == schedule.name,
                        self._model.next_run == current_next_run,
                    )
                    .values(next_run=current_next_run + schedule.every)
                )
                session.commit()

                if result.rowcount:  # type: ignore[attr-defined]
                    schedule._build_task().schedule(self.queue)  # pylint: disable=protected-access
                    enqueued += 1

        return enqueued

    def run(self, check_every: timedelta = timedelta(seconds=1)) -> NoReturn:
        """Call `tick()` forever, sleeping `check_every` between calls."""
        while True:
            self.tick()
            time.sleep(check_every.total_seconds())
