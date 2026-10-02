"""Prototype: periodic ("cron-like") tasks, built entirely on top of the
existing `AlchemicalTaskQueue` / `task` API -- no changes to the core Queue
abstraction.

A [Beat][alchemical_queues.tasks.periodic.Beat] holds a small set of
[PeriodicTask][alchemical_queues.tasks.periodic.PeriodicTask] schedules and,
on each `tick()`, enqueues (via the normal `Task.schedule()` path) whichever
ones are due. "Due" is tracked in one tiny extra table keyed by schedule
name, so running several `Beat` processes at once (for redundancy) never
double-enqueues a run: the due check and the next_run advance happen in a
single conditional `UPDATE`, which only one concurrent transaction can win,
the same pattern `AlchemicalTaskQueue.get()` already relies on to hand each
entry to exactly one claimant.

This is intentionally minimal -- fixed-interval schedules only, no cron
expression syntax. A cron string could be layered on top later (parsed down
to "the next due datetime from here", same as `every` is now) without
changing `Beat` itself.
"""

import time
from datetime import datetime, timedelta
from typing import Callable, Dict, Generic, List, NoReturn

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
        args: tuple,
        kwargs: dict,
    ) -> None:
        self.name = name
        self.every = every
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
        self, tasker: "Tasker[Param, RValue]", name: str, every: timedelta
    ) -> None:
        self._tasker = tasker
        self.name = name
        self.every = every

    def __call__(self, *args, **kwargs) -> PeriodicTask[Param, RValue]:
        return PeriodicTask(self.name, self._tasker, self.every, args, kwargs)


def periodic(
    tasker: "Tasker[Param, RValue]", *, name: str, every: timedelta
) -> PeriodicTaskFactory[Param, RValue]:
    """Wrap a `@task`-decorated function as a fixed-interval schedule.

    Args:
        tasker (Tasker): a function already decorated with `@task`.
        name (str): a stable identifier for this schedule, used as the
            dedup key in `Beat`'s own table. Changing it starts a fresh
            schedule (a new row, next due immediately).
        every (timedelta): how often to enqueue this task.

    Returns:
        PeriodicTaskFactory: call it with the task's own arguments (exactly
            like calling `tasker` itself) to get a `PeriodicTask` to hand to
            `Beat`.
    """
    return PeriodicTaskFactory(tasker, name, every)


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

    This is a prototype -- not yet exported from `alchemical_queues.tasks`.
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
        that was. Schedules seen for the first time are treated as due
        immediately."""
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
                session.add(self._model(name=name, next_run=now))
            session.commit()

            for schedule in self.schedules.values():
                result = session.execute(
                    update(self._model)
                    .where(
                        self._model.name == schedule.name,
                        self._model.next_run <= now,
                    )
                    .values(next_run=now + schedule.every)
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
