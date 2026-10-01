"""Implementation of Alchemical Queues"""

import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Generic, List, Type, TypeVar, Union, cast

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    LargeBinary,
    String,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .serializers import PickleSerializer, Serializer

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

DEFAULT_VISIBILITY_TIMEOUT = timedelta(minutes=5)
DEFAULT_SERIALIZER: Serializer[Any] = PickleSerializer()


T = TypeVar("T")
R = TypeVar("R")

# Dialects whose SELECT ... FOR UPDATE supports SKIP LOCKED, used to make
# concurrent AlchemicalTaskQueue.get() calls avoid contending on the same rows.
_SKIP_LOCKED_DIALECTS = frozenset({"postgresql", "mysql", "mariadb", "oracle"})


def _supports_returning(engine: Union[Engine, "AsyncEngine"], kind: str) -> bool:
    """Whether this engine's dialect supports `<kind> ... RETURNING` (`kind`
    is "delete" or "update"). This is checked per statement kind rather than
    assumed from the dialect name: MySQL supports neither, and MariaDB
    supports `DELETE ... RETURNING` but not `UPDATE ... RETURNING`.
    """
    return bool(getattr(engine.dialect, f"{kind}_returning", False))


def _new_claim_token() -> int:
    # A fresh random value per claim (not the entry_id, not a counter): this
    # is what lets release()/discard()/extend() tell "I still hold today's
    # claim on this entry" apart from "I held a claim on this entry that's
    # since moved on to someone else", even though the entry_id is identical
    # in both cases.
    return secrets.randbits(63)


class ClaimExpired(Exception):
    """Raised by AlchemicalTaskQueue's release()/discard()/extend() when the
    entry_id/claim_token pair they were given no longer matches a live
    claim: it already timed out and was reclaimed by someone else, or was
    already responded to, released, or discarded. Whatever you were about
    to do with this claim, don't trust it -- someone else may already have,
    or may still.
    """

    def __init__(self, entry_id: int):
        super().__init__(
            f"entry_id={entry_id} is not currently claimed under the given claim_token "
            "(it may have timed out and been reclaimed, or already been responded to, "
            "released, or discarded)"
        )
        self.entry_id = entry_id


def _generate_models(
    queue_tablename: str,
    response_tablename: str,
    base: Union[Type[DeclarativeBase], None] = None,
):
    if base is None:
        # Without an explicit base, each call gets its own DeclarativeBase, which
        # implicitly creates its own registry and metadata, so separate
        # AlchemicalQueues instances never clash even if they happen to use the
        # same table names.
        class DefaultBase(DeclarativeBase):
            """SQLAlchemy model base class"""

        base = DefaultBase

    class Entry(base):  # type: ignore[misc,valid-type]
        """SQLAlchemy model for a Queue Entry."""

        __tablename__: str = queue_tablename
        # No-op outside SQLite: makes SQLite never reuse a deleted row's rowid,
        # so entry_id stays unique even after the queue empties out and refills.
        __table_args__ = {"sqlite_autoincrement": True}

        entry_id: Mapped[int] = mapped_column(
            Integer, primary_key=True, nullable=False, autoincrement=True
        )
        queue_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

        enqueued_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), nullable=False
        )
        schedule_at: Mapped[Union[datetime, None]] = mapped_column(
            DateTime(timezone=True), nullable=True
        )
        priority: Mapped[int] = mapped_column(Integer, nullable=False)
        data: Mapped[Union[bytes, None]] = mapped_column(LargeBinary)
        # Only ever set by AlchemicalTaskQueue.get(); stays NULL for entries
        # only ever touched through plain AlchemicalQueue. A claimed entry
        # past this timestamp is treated as abandoned and becomes claimable
        # again, so a worker that dies mid-task doesn't lose the entry
        # silently forever.
        claimed_until: Mapped[Union[datetime, None]] = mapped_column(
            DateTime(timezone=True), nullable=True
        )
        # A fresh random value set alongside claimed_until on every claim.
        # AlchemicalTaskQueue's release()/discard()/extend() require the
        # caller to present the value they were given, so a claim holder
        # that's been superseded by a later claim (same entry_id, new
        # claim_token) can't mistake itself for the current one -- see
        # ClaimExpired.
        claim_token: Mapped[Union[int, None]] = mapped_column(BigInteger, nullable=True)

    class Response(base):  # type: ignore[misc,valid-type]
        """SQLAlchemy model for a Task Result."""

        __tablename__: str = response_tablename
        __table_args__ = {"sqlite_autoincrement": True}

        response_id: Mapped[int] = mapped_column(
            Integer, primary_key=True, nullable=False, autoincrement=True
        )
        queue_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
        entry_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

        delivered_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), nullable=False
        )
        cleanup_at: Mapped[Union[datetime, None]] = mapped_column(
            DateTime(timezone=True), nullable=True
        )
        data: Mapped[Union[bytes, None]] = mapped_column(LargeBinary)

    return base, Entry, Response  # type: ignore


class AlchemicalQueues:
    """The core entrypoint to Alchemical Queues."""

    def __init__(
        self,
        engine: Union[Engine, None] = None,
        queue_tablename: str = "AlchemicalQueue",
        response_tablename: str = "AlchemicalResult",
        base: Union[Type[DeclarativeBase], None] = None,
    ) -> None:
        """Create the main queue entrypoint object.

        Args:
            engine (sqlalchemy.engine.Engine | None): The SQLAlchemy engine you want to use. May be left None and initialized later.
            queue_tablename (str): The name of the table AlchemicalQueues uses for queues.
            response_tablename (str): The name of the table AlchemicalQueues uses for task results.
            base (Type[DeclarativeBase] | None, optional): An existing declarative base to attach
                the queue/response tables to, so they share a registry and metadata with tables
                defined elsewhere in your application (e.g. Flask-SQLAlchemy's `db.Model`). Leave
                `None` to let AlchemicalQueues create its own isolated base.
        """

        self._engine = engine
        self._base, self._qmodel, self._rmodel = _generate_models(
            queue_tablename, response_tablename, base
        )
        self._queues: Dict[str, "AlchemicalQueue"] = {}
        self._task_queues: Dict[str, "AlchemicalTaskQueue"] = {}

    def set_engine(self, engine: Engine) -> None:
        """Set the SQLAlchemy engine post-initialization

        Args:
            engine (sqlalchemy.engine.Engine): The SQLAlchemy engine you want to use.

        Raises:
            Exception: when the engine was already set.
        """

        if self._engine is not None:
            raise Exception(
                "Cannot set the engine on Alchemical Queues more than once!"
            )

        self._engine = engine

    def create_all(self) -> None:
        """Create the needed SQLAlchemy table. You would normally call this
        when you are also creating your own tables, e.g. db.create_all()."""
        if self._engine is None:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        self._base.metadata.create_all(self._engine)

    def clear(self) -> None:
        """Clear all entries from all queues and task results. Might fail-silent an update call."""

        if self._engine is None:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        with Session(self._engine) as session:
            session.execute(delete(self._qmodel))
            session.execute(delete(self._rmodel))
            session.commit()

    def get(
        self, key: str, *, serializer: Serializer[Any] = DEFAULT_SERIALIZER
    ) -> "AlchemicalQueue[Any]":
        """Get a plain Queue instance: `put()`/`get()`/`qsize()`/`empty()`/`clear()`,
        where `get()` removes the entry immediately. If your process dies
        between `get()` and finishing whatever you needed the entry for, the
        entry is simply gone -- there's no redelivery. For that (and for
        result-tracking via `respond()`/`responses()`), see
        [get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue].

        Args:
            key (str): The name of the queue you wish to access.
            serializer (Serializer, optional): how entries put into this queue
                are turned into bytes for storage and back. Defaults to
                [PickleSerializer][alchemical_queues.serializers.PickleSerializer],
                matching previous behavior. Only takes effect the first time
                this queue name is requested; later calls return the same
                cached instance. See
                [get_serialized][alchemical_queues.AlchemicalQueues.get_serialized]
                for a typed equivalent.

        Returns:
            AlchemicalQueue
        """
        if self._engine is None:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        if key not in self._queues:
            self._queues[key] = AlchemicalQueue(
                self._engine, self._qmodel, key, serializer=serializer
            )

        return self._queues[key]

    def get_typed(
        self,
        key: str,
        typeof: Type[T],
        *,
        serializer: Union[Serializer[T], None] = None,
    ) -> "AlchemicalQueue[T]":
        """Get a typed Queue instance

        Args:
            key (str): The name of the queue you wish to access.
            typeof (Type[T]): The type of the queue you wish to use
            serializer (Serializer[T] | None, optional): see
                [get][alchemical_queues.AlchemicalQueues.get]. Defaults to
                [PickleSerializer][alchemical_queues.serializers.PickleSerializer].

        Returns:
            AlchemicalQueue[T]
        """
        # pylint: disable=unused-argument
        return cast(
            AlchemicalQueue[T],
            self.get(key, serializer=serializer or DEFAULT_SERIALIZER),
        )

    def get_serialized(
        self, key: str, serializer: Serializer[T]
    ) -> "AlchemicalQueue[T]":
        """Get a Queue instance typed by its `serializer` instead of an
        explicit `typeof`, e.g. `queues.get_serialized("q", PydanticSerializer(MyModel))`
        gives you an `AlchemicalQueue[MyModel]` without repeating the type.

        Args:
            key (str): The name of the queue you wish to access.
            serializer (Serializer[T]): see [get][alchemical_queues.AlchemicalQueues.get].

        Returns:
            AlchemicalQueue[T]
        """
        return cast(AlchemicalQueue[T], self.get(key, serializer=serializer))

    def get_task_queue(
        self,
        key: str,
        *,
        visibility_timeout: timedelta = DEFAULT_VISIBILITY_TIMEOUT,
        serializer: Serializer[Any] = DEFAULT_SERIALIZER,
        response_serializer: Serializer[Any] = DEFAULT_SERIALIZER,
    ) -> "AlchemicalTaskQueue[Any, Any]":
        """Get an [AlchemicalTaskQueue][alchemical_queues.AlchemicalTaskQueue]
        instance: `get()` claims an entry rather than removing it, with
        `release()`/`discard()`/`extend()` to manage that claim and
        `respond()`/`responses()` to record and retrieve results. This is
        what [tasks.Worker][alchemical_queues.tasks.Worker] requires.

        Args:
            key (str): The name of the queue you wish to access.
            visibility_timeout (timedelta, optional): how long a claimed entry
                stays claimed before it becomes claimable again if nobody has
                responded to or released it. Only takes effect the first time
                this queue name is requested; later calls return the same
                cached AlchemicalTaskQueue instance.
            serializer (Serializer, optional): how entries put into this queue
                are turned into bytes for storage and back. Defaults to
                [PickleSerializer][alchemical_queues.serializers.PickleSerializer],
                matching previous behavior. Only takes effect the first time
                this queue name is requested. See
                [get_task_queue_serialized][alchemical_queues.AlchemicalQueues.get_task_queue_serialized]
                for a typed equivalent.
            response_serializer (Serializer, optional): how responses recorded
                via `respond()` are turned into bytes for storage and back.
                Independent of `serializer` -- a task's input and its result
                are usually different shapes. Defaults to
                [PickleSerializer][alchemical_queues.serializers.PickleSerializer].

        Returns:
            AlchemicalTaskQueue
        """
        if self._engine is None:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        if key not in self._task_queues:
            self._task_queues[key] = AlchemicalTaskQueue(
                self._engine,
                self._qmodel,
                self._rmodel,
                key,
                visibility_timeout,
                serializer=serializer,
                response_serializer=response_serializer,
            )

        return self._task_queues[key]

    def get_task_queue_typed(
        self,
        key: str,
        typeof: Type[T],
        *,
        visibility_timeout: timedelta = DEFAULT_VISIBILITY_TIMEOUT,
        serializer: Union[Serializer[T], None] = None,
    ) -> "AlchemicalTaskQueue[T, Any]":
        """Get a typed AlchemicalTaskQueue instance. Only the entry type `T`
        is given explicitly here -- responses stay `Any`
        (`PickleSerializer` by default). See
        [get_task_queue_serialized][alchemical_queues.AlchemicalQueues.get_task_queue_serialized]
        to also type (and independently serialize) responses.

        Args:
            key (str): The name of the queue you wish to access.
            typeof (Type[T]): The type of the queue you wish to use
            visibility_timeout (timedelta, optional): see
                [get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue].
            serializer (Serializer[T] | None, optional): see
                [get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue].
                Defaults to [PickleSerializer][alchemical_queues.serializers.PickleSerializer].

        Returns:
            AlchemicalTaskQueue[T, Any]
        """
        # pylint: disable=unused-argument
        return cast(
            AlchemicalTaskQueue[T, Any],
            self.get_task_queue(
                key,
                visibility_timeout=visibility_timeout,
                serializer=serializer or DEFAULT_SERIALIZER,
            ),
        )

    def get_task_queue_serialized(
        self,
        key: str,
        serializer: Serializer[T],
        *,
        response_serializer: Union[Serializer[R], None] = None,
        visibility_timeout: timedelta = DEFAULT_VISIBILITY_TIMEOUT,
    ) -> "AlchemicalTaskQueue[T, R]":
        """Get an AlchemicalTaskQueue instance typed by its `serializer`/
        `response_serializer` instead of explicit `typeof`s, e.g.
        `queues.get_task_queue_serialized("q", PydanticSerializer(Job), response_serializer=PydanticSerializer(JobResult))`
        gives you an `AlchemicalTaskQueue[Job, JobResult]` without repeating
        either type -- `entry.data` is a `Job`, `responses()[i].data` is a
        `JobResult`, independently serialized.

        Args:
            key (str): The name of the queue you wish to access.
            serializer (Serializer[T]): see
                [get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue].
            response_serializer (Serializer[R] | None, optional): see
                [get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue].
                Defaults to [PickleSerializer][alchemical_queues.serializers.PickleSerializer].
                For a queue `tasks.Worker` runs, consider
                [tasks.TaskResultSerializer][alchemical_queues.tasks.TaskResultSerializer],
                which keeps Worker's `{"result": ...}`/`{"error": ...}`
                envelope while serializing the success value with a
                serializer of your choice.
            visibility_timeout (timedelta, optional): see
                [get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue].

        Returns:
            AlchemicalTaskQueue[T, R]
        """
        return cast(
            AlchemicalTaskQueue[T, R],
            self.get_task_queue(
                key,
                visibility_timeout=visibility_timeout,
                serializer=serializer,
                response_serializer=response_serializer or DEFAULT_SERIALIZER,
            ),
        )


class AlchemicalQueue(Generic[T]):
    """A plain Alchemical Queue: put an item in, get the highest-priority one
    back out. `get()` removes the entry immediately -- there's no claim to
    manage, nothing to acknowledge, and no redelivery if your process dies
    partway through handling what it returned. It is not intended to be
    initialized by a user, go through
    [AlchemicalQueues][alchemical_queues.AlchemicalQueues] instead.

    If you need crash-safe delivery (an entry comes back if the worker that
    claimed it dies before finishing), retries, or a way to record and poll
    for a result, use
    [AlchemicalQueues.get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue]
    instead.
    """

    def __init__(
        self,
        engine: Engine,
        model,
        name: str,
        serializer: Serializer[T] = DEFAULT_SERIALIZER,
    ):
        self._engine = engine
        self._model = model
        self._name = name
        self._serializer = serializer
        self._session = sessionmaker(
            engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @property
    def name(self) -> str:
        """The name of the queue"""
        return self._name

    def put(
        self,
        item: T,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
    ) -> "AlchemicalEntry[T]":
        """Put an entry into the AlchemicalQueue

        Args:
            item (T): The item you wish to add to the queue. Must be
                serializable by this queue's `serializer` (pickle-able, by
                default).
            schedule_at (datetime | None, optional): Earliest timestamp this entry may be popped of the queue.
            priority (int, optional): Entry priority. Entries are popped of first in order of priority and then
                                      in order of adding to the queue.

        Returns:
            AlchemicalEntry[T]: The resultant queue entry.
        """

        entry = self._model(
            enqueued_at=datetime.now(),
            schedule_at=schedule_at,
            priority=priority,
            queue_name=self._name,
            data=self._serializer.dumps(item),
        )

        with self._session() as session:
            session.add(entry)
            session.commit()

            return AlchemicalEntry(entry, item)

    def get(self) -> Union["AlchemicalEntry[T]", None]:
        """Get the highest priority entry out from the queue, removing it.

        Returns:
            (AlchemicalEntry | None): The popped entry, or None if the queue is empty (or nothing is scheduled yet)
        """

        timestamp = datetime.now()

        with self._session() as session:
            query = (
                select(self._model)
                .filter(
                    self._model.queue_name == self._name,
                    or_(
                        self._model.schedule_at == None,  # noqa: E711 (SQLAlchemy requires == None for IS NULL)
                        self._model.schedule_at <= timestamp,  # type: ignore
                    ),
                )
                .order_by(self._model.priority.desc(), self._model.entry_id.asc())  # type: ignore
                .limit(1)
            )

            if self._engine.dialect.name in _SKIP_LOCKED_DIALECTS:
                query = query.with_for_update(skip_locked=True)

            if _supports_returning(self._engine, "delete"):
                # Picking the candidate row and deleting it happen in one
                # atomic DELETE ... RETURNING statement, so two concurrent
                # get() calls can never pop the same entry, with no extra
                # transaction isolation needed for that guarantee to hold.
                candidate = query.with_only_columns(self._model.entry_id)
                item = session.execute(
                    delete(self._model)
                    .where(self._model.entry_id == candidate.scalar_subquery())
                    .returning(self._model)
                ).scalar_one_or_none()
            else:
                # MySQL has no RETURNING support at all: lock and fetch the
                # full candidate row first instead, then delete it by id in
                # the same transaction. The row stays locked throughout, so
                # the same atomicity guarantee holds.
                item = session.execute(query).scalar_one_or_none()
                if item is not None:
                    session.delete(item)

            if item is None:
                session.rollback()
                return None

            entry = AlchemicalEntry(item, self._serializer.loads(item.data))
            session.commit()

        return entry

    def qsize(self) -> int:
        """Return the approximate size of this queue.

        Returns:
            int: Queue size.
        """
        with self._session() as session:
            return (
                session.scalar(
                    select(func.count())  # pylint: disable=not-callable
                    .select_from(self._model)
                    .where(self._model.queue_name == self._name)
                )
                or 0
            )

    def empty(self) -> bool:
        """Return `True` if the Queue is emtpy, `False` otherwise. More efficient than
        `qsize() > 0`.

        Returns:
            bool: wether the Queue is empty.
        """
        with self._session() as session:
            return (
                session.scalar(
                    select(self._model.entry_id)
                    .where(self._model.queue_name == self._name)
                    .limit(1)
                )
                is None
            )

    def clear(self) -> None:
        """Clear all entries from this queue. Might fail-silent an update call."""

        with self._session() as session:
            session.execute(
                delete(self._model).where(self._model.queue_name == self._name)
            )
            session.commit()


class AlchemicalTaskQueue(Generic[T, R]):
    """An Alchemical Queue with crash-safe delivery: `get()` claims an entry
    rather than removing it, `release()`/`discard()`/`extend()` manage that
    claim, and `respond()`/`responses()` record and retrieve a result for a
    given entry. This is the queue [tasks.Worker][alchemical_queues.tasks.Worker]
    is built on. It is not intended to be initialized by a user, go through
    [AlchemicalQueues.get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue]
    instead.

    `T` is the type of entries (`put()`/`get()`); `R` is the type of
    responses (`respond()`/`responses()`) -- they're independent, each with
    their own `serializer`/`response_serializer`, since a task's input and
    its result are usually different shapes.

    If you just want a plain FIFO/priority queue and don't need redelivery,
    retries, or result tracking, use
    [AlchemicalQueues.get][alchemical_queues.AlchemicalQueues.get] /
    [AlchemicalQueue][alchemical_queues.AlchemicalQueue] instead -- it has a
    much smaller surface.
    """

    def __init__(
        self,
        engine: Engine,
        model,
        response_model,
        name: str,
        visibility_timeout: timedelta = DEFAULT_VISIBILITY_TIMEOUT,
        serializer: Serializer[T] = DEFAULT_SERIALIZER,
        response_serializer: Serializer[R] = DEFAULT_SERIALIZER,
    ):
        self._engine = engine
        self._model = model
        self._response_model = response_model
        self._name = name
        self._visibility_timeout = visibility_timeout
        self._response_serializer = response_serializer
        self._serializer = serializer
        self._session = sessionmaker(
            engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @property
    def name(self) -> str:
        """The name of the queue"""
        return self._name

    @property
    def visibility_timeout(self) -> timedelta:
        """The default visibility_timeout this queue's `get()` calls use."""
        return self._visibility_timeout

    def put(
        self,
        item: T,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
    ) -> "AlchemicalEntry[T]":
        """Put an entry into the queue

        Args:
            item (T): The item you wish to add to the queue. Must be
                serializable by this queue's `serializer` (pickle-able, by
                default).
            schedule_at (datetime | None, optional): Earliest timestamp this entry may be claimed.
            priority (int, optional): Entry priority. Entries are claimed first in order of priority and then
                                      in order of adding to the queue.

        Returns:
            AlchemicalEntry[T]: The resultant queue entry.
        """

        entry = self._model(
            enqueued_at=datetime.now(),
            schedule_at=schedule_at,
            priority=priority,
            queue_name=self._name,
            data=self._serializer.dumps(item),
        )

        with self._session() as session:
            session.add(entry)
            session.commit()

            return AlchemicalEntry(entry, item)

    def get(
        self, *, visibility_timeout: Union[timedelta, None] = None
    ) -> Union["AlchemicalEntry[T]", None]:
        """Claim the highest priority entry from the queue.

        The entry is not removed by this call: it is marked claimed until
        `visibility_timeout` elapses, so if your process dies before you call
        [release][alchemical_queues.AlchemicalTaskQueue.release] or
        [discard][alchemical_queues.AlchemicalTaskQueue.discard] for it, a
        later `get()` call will eventually claim it again instead of the
        entry being lost. Call `discard()` (after `respond()`, if you have a
        result) or `release()` (if you want it retried sooner than the
        timeout) as soon as you're done with an entry to free it up
        immediately, rather than waiting out the timeout. For work that can
        run longer than `visibility_timeout`, call
        [extend][alchemical_queues.AlchemicalTaskQueue.extend] periodically to
        push the deadline out while you're still on it.

        The returned entry's `claim_token` identifies *this* claim
        specifically -- pass it to `release()`/`discard()`/`extend()` so they
        only affect it, even if your claim has since timed out and been
        reclaimed by someone else (same entry_id, different claim_token).

        Args:
            visibility_timeout (timedelta | None, optional): how long this entry
                stays claimed before it becomes claimable again. Defaults to the
                visibility_timeout this queue was obtained with (see
                [AlchemicalQueues.get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue]).

        Returns:
            (AlchemicalEntry | None): The claimed entry, or None if the queue is empty
            (or nothing is claimable/scheduled yet)
        """

        timestamp = datetime.now()
        timeout = (
            visibility_timeout
            if visibility_timeout is not None
            else self._visibility_timeout
        )
        token = _new_claim_token()

        with self._session() as session:
            query = (
                select(self._model)
                .filter(
                    self._model.queue_name == self._name,
                    or_(
                        self._model.schedule_at == None,  # noqa: E711 (SQLAlchemy requires == None for IS NULL)
                        self._model.schedule_at <= timestamp,  # type: ignore
                    ),
                    or_(
                        self._model.claimed_until == None,  # noqa: E711 (SQLAlchemy requires == None for IS NULL)
                        self._model.claimed_until <= timestamp,  # type: ignore
                    ),
                )
                .order_by(self._model.priority.desc(), self._model.entry_id.asc())  # type: ignore
                .limit(1)
            )

            if self._engine.dialect.name in _SKIP_LOCKED_DIALECTS:
                query = query.with_for_update(skip_locked=True)

            if _supports_returning(self._engine, "update"):
                # Picking the candidate row and claiming it happen in one
                # atomic UPDATE ... RETURNING statement, so two concurrent
                # get() calls can never claim the same entry. This needs no
                # special transaction isolation (previously SQLite needed a
                # global BEGIN EXCLUSIVE, which serialized every transaction
                # on the engine, including ones from unrelated code sharing
                # the same engine).
                candidate = query.with_only_columns(self._model.entry_id)
                item = session.execute(
                    update(self._model)
                    .where(self._model.entry_id == candidate.scalar_subquery())
                    .values(claimed_until=timestamp + timeout, claim_token=token)
                    .returning(self._model)
                ).scalar_one_or_none()
            else:
                # MySQL has no RETURNING support at all, and MariaDB only
                # supports it for DELETE/INSERT, not UPDATE: lock and fetch
                # the full candidate row first instead, set the claim fields
                # on the already-loaded instance, and let the session flush
                # that as a plain UPDATE on commit. The row stays locked
                # throughout, so the same atomicity guarantee holds.
                item = session.execute(query).scalar_one_or_none()
                if item is not None:
                    item.claimed_until = timestamp + timeout
                    item.claim_token = token

            if item is None:
                session.rollback()
                return None

            entry = AlchemicalEntry(item, self._serializer.loads(item.data))
            session.commit()

        return entry

    def release(self, entry_id: int, claim_token: int) -> None:
        """Release a claimed entry back to the queue, without recording a
        response for it. The entry itself is kept -- it becomes claimable
        again immediately, same as if its visibility timeout had just
        elapsed, instead of whoever's turn it is next having to wait that
        out.

        Use this when you called [get][alchemical_queues.AlchemicalTaskQueue.get],
        decided you have nothing to respond with yet (you want it retried
        sooner than the timeout, or by someone else), and don't want to
        discard it. To remove an entry entirely without a response, see
        [discard][alchemical_queues.AlchemicalTaskQueue.discard].

        Args:
            entry_id (int): the entry_id you wish to release.
            claim_token (int): the claim_token from the `get()` call that
                claimed this entry (`AlchemicalEntry.claim_token`).

        Raises:
            ClaimExpired: if entry_id isn't currently claimed under claim_token.
        """

        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        with self._session() as session:
            result = session.execute(
                update(self._model)
                .where(
                    self._model.entry_id == entry_id,
                    self._model.claim_token == claim_token,
                )
                .values(claimed_until=None, claim_token=None)
            )
            session.commit()

        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ClaimExpired(entry_id)

    def discard(self, entry_id: int, claim_token: int) -> None:
        """Remove a claimed entry from the queue entirely, without recording
        a response for it. Unlike [release][alchemical_queues.AlchemicalTaskQueue.release],
        the entry does not become claimable again -- it's simply gone, the same
        as if [respond][alchemical_queues.AlchemicalTaskQueue.respond] had been
        called but without creating a response.

        Args:
            entry_id (int): the entry_id you wish to discard.
            claim_token (int): the claim_token from the `get()` call that
                claimed this entry (`AlchemicalEntry.claim_token`).

        Raises:
            ClaimExpired: if entry_id isn't currently claimed under claim_token.
        """

        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        with self._session() as session:
            result = session.execute(
                delete(self._model).where(
                    self._model.entry_id == entry_id,
                    self._model.claim_token == claim_token,
                )
            )
            session.commit()

        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ClaimExpired(entry_id)

    def extend(
        self,
        entry_id: int,
        claim_token: int,
        *,
        by: Union[timedelta, None] = None,
    ) -> None:
        """Extend a claim's visibility timeout, for work that can take longer
        than it. Call this periodically while you're still actively on an
        entry (a keepalive) to push its claim's expiry further out, so it
        isn't redelivered to another `get()` call while you're still working
        on it. [tasks.Worker][alchemical_queues.tasks.Worker] can do this for
        you automatically; see its `keepalive_every` argument.

        Args:
            entry_id (int): the entry_id whose claim you wish to extend.
            claim_token (int): the claim_token from the `get()` call that
                claimed this entry (`AlchemicalEntry.claim_token`).
            by (timedelta | None, optional): how much longer the claim should
                last from now. Defaults to this queue's configured visibility_timeout.

        Raises:
            ClaimExpired: if entry_id isn't currently claimed under claim_token
                -- it's already timed out (and possibly been redelivered and
                finished by someone else), so extending it further would be
                meaningless at best and misleading at worst. Stop working.
        """

        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        timeout = by if by is not None else self._visibility_timeout
        new_claimed_until = datetime.now() + timeout

        with self._session() as session:
            result = session.execute(
                update(self._model)
                .where(
                    self._model.entry_id == entry_id,
                    self._model.claim_token == claim_token,
                )
                .values(claimed_until=new_claimed_until)
            )
            session.commit()

        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ClaimExpired(entry_id)

    def qsize(self) -> int:
        """Return the approximate size of this queue.

        Returns:
            int: Queue size.
        """
        with self._session() as session:
            return (
                session.scalar(
                    select(func.count())  # pylint: disable=not-callable
                    .select_from(self._model)
                    .where(self._model.queue_name == self._name)
                )
                or 0
            )

    def empty(self) -> bool:
        """Return `True` if the Queue is emtpy, `False` otherwise. More efficient than
        `qsize() > 0`.

        Returns:
            bool: wether the Queue is empty.
        """
        with self._session() as session:
            return (
                session.scalar(
                    select(self._model.entry_id)
                    .where(self._model.queue_name == self._name)
                    .limit(1)
                )
                is None
            )

    def clear(self) -> None:
        """Clear all entries from this queue. Might fail-silent an update call."""

        with self._session() as session:
            session.execute(
                delete(self._model).where(self._model.queue_name == self._name)
            )
            session.commit()

    def respond(
        self, entry_id: int, response: R, cleanup_at: Union[datetime, None] = None
    ) -> "AlchemicalResponse[R]":
        """Record a response for a queue entry. Used to implement task queues.

        This only records the response -- it has no effect on a claim you may
        be holding on entry_id (there may not even be one: it's independent
        of [get][alchemical_queues.AlchemicalTaskQueue.get]/claim lifecycle
        entirely, just like `responses()` is). Call
        [discard][alchemical_queues.AlchemicalTaskQueue.discard] separately once
        you've responded, to free up the entry you claimed.

        If this entry_id belongs to a `tasks.task`-scheduled task, note that
        [QueuedTask.result][alchemical_queues.tasks.QueuedTask.result] only
        recognizes the `{"result": ...}`/`{"error": ..., "error_type": ...}`
        shape `tasks.Worker` itself responds with to tell success from
        failure -- call `respond()` directly (as here) with anything else
        and `QueuedTask.result` just hands that value back to you unparsed.

        Args:
            entry_id (int): The entry_id you wish to respond to.
            response (R): The response data. Must be serializable by this
                queue's `response_serializer` (pickle-able, by default).
            cleanup_at (datetime, optional): The optional cleanup timestamp. After this time the response will be removed.
                                             By default it is not automatically cleaned up.

        Returns:
            AlchemicalResponse[R]: the response as sent.
        """

        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        entry = self._response_model(
            entry_id=entry_id,
            delivered_at=datetime.now(),
            cleanup_at=cleanup_at,
            queue_name=self._name,
            data=self._response_serializer.dumps(response),
        )

        with self._session() as session:
            session.add(entry)
            session.commit()

            return AlchemicalResponse(entry, response)

    def responses(self, entry_id: int) -> List["AlchemicalResponse[R]"]:
        """Obtain the response(s) to a specific queue entry.

        Returns:
            List[AlchemicalResponse[R]]: A list of responses
        """
        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        with self._session() as session:
            now = datetime.now()

            session.execute(
                delete(self._response_model).where(
                    self._response_model.cleanup_at != None,  # noqa: E711 (SQLAlchemy requires == None for IS NULL)
                    self._response_model.cleanup_at < now,
                )
            )
            session.commit()

            entries: Any = session.scalars(
                select(self._response_model).where(
                    self._response_model.queue_name == self._name,
                    self._response_model.entry_id == entry_id,
                )
            ).all()
            return [
                AlchemicalResponse(e, self._response_serializer.loads(e.data))
                for e in entries
            ]


class AlchemicalEntry(Generic[T]):
    """An entry in a queue.

    Attributes:
        entry_id (int): the identifier of the entry. Guaranteed unique per [AlchemicalQueues][alchemical_queues.AlchemicalQueues] instance.
        enqueued_at (datetime): when the entry was added to the queue.
        schedule_at (datetime | None): do not remove the entry from the queue before this time.
        priority (int): the priority of the entry.
        data (T): the data stored in this entry.
        claim_token (int | None): identifies this specific claim, if this entry
            came from [AlchemicalTaskQueue.get][alchemical_queues.AlchemicalTaskQueue.get]
            (`None` for an entry from plain `AlchemicalQueue`, or from `put()`,
            neither of which claim anything). Pass it to
            `release()`/`discard()`/`extend()` to prove you still hold this
            particular claim and not a since-expired one.
    """

    __slots__ = (
        "data",
        "entry_id",
        "enqueued_at",
        "schedule_at",
        "priority",
        "claim_token",
    )

    def __init__(
        self,
        entry,
        data: T,
    ):
        assert isinstance(entry.entry_id, int)

        self.entry_id: int = entry.entry_id
        self.enqueued_at: datetime = entry.enqueued_at
        self.schedule_at: Union[datetime, None] = entry.schedule_at
        self.claim_token: Union[int, None] = entry.claim_token
        self.priority: int = entry.priority
        self.data: T = data

    def __repr__(self):
        return (
            f"<{self.__class__.__module__}.{self.__class__.__name__} "
            f"entry_id={self.entry_id} enqueued_at={self.enqueued_at} "
            f"schedule_at={self.schedule_at} priority={self.priority}>"
        )


class AlchemicalResponse(Generic[R]):
    """An response to a queue item. While you can use this as a user, it is probably most useful for the tasks submodule.

    Attributes:
        response_id (int): the identifier of the response.
        entry_id (int): the identifier of the associated entry.
        delivered_at (datetime): when the response was submitted.
        cleanup_at (datetime | None): autoremove this response after this time.
        data (R): Response data.
    """

    __slots__ = [
        "data",
        "entry_id",
        "response_id",
        "delivered_at",
        "cleanup_at",
    ]

    def __init__(
        self,
        response,
        data: R,
    ):
        self.response_id = response.response_id
        self.entry_id = response.entry_id
        self.delivered_at = response.delivered_at
        self.cleanup_at = response.cleanup_at
        self.data = data
