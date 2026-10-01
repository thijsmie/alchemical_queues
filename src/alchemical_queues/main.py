"""Implementation of Alchemical Queues"""

import pickle
from datetime import datetime, timedelta
from typing import Dict, List, Any, Union, Type, cast, Generic, TypeVar

from sqlalchemy import (
    or_,
    delete,
    select,
    update,
    func,
    DateTime,
    Integer,
    Text,
    LargeBinary,
)
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase, Mapped, mapped_column
from sqlalchemy.engine import Engine

DEFAULT_VISIBILITY_TIMEOUT = timedelta(minutes=5)


T = TypeVar("T")

# Dialects whose SELECT ... FOR UPDATE supports SKIP LOCKED, used to make
# concurrent AlchemicalQueue.get() calls avoid contending on the same rows.
_SKIP_LOCKED_DIALECTS = frozenset({"postgresql", "mysql", "oracle"})


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
        queue_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)

        enqueued_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), nullable=False
        )
        schedule_at: Mapped[Union[datetime, None]] = mapped_column(
            DateTime(timezone=True), nullable=True
        )
        priority: Mapped[int] = mapped_column(Integer, nullable=False)
        data: Mapped[Union[bytes, None]] = mapped_column(LargeBinary)
        # Set by get() when an entry is claimed; cleared (deleted, really -- see
        # respond()/release()) once it's handled. A claimed entry past this
        # timestamp is treated as abandoned and becomes claimable again, so a
        # worker that dies mid-task doesn't lose the entry silently forever.
        claimed_until: Mapped[Union[datetime, None]] = mapped_column(
            DateTime(timezone=True), nullable=True
        )

    class Response(base):  # type: ignore[misc,valid-type]
        """SQLAlchemy model for a Task Result."""

        __tablename__: str = response_tablename
        __table_args__ = {"sqlite_autoincrement": True}

        response_id: Mapped[int] = mapped_column(
            Integer, primary_key=True, nullable=False, autoincrement=True
        )
        queue_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
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
        self,
        key: str,
        *,
        visibility_timeout: timedelta = DEFAULT_VISIBILITY_TIMEOUT,
    ) -> "AlchemicalQueue[Any]":
        """Get a Queue instance

        Args:
            key (str): The name of the queue you wish to access.
            visibility_timeout (timedelta, optional): how long an entry stays claimed
                after [AlchemicalQueue.get][alchemical_queues.AlchemicalQueue.get] before
                it becomes claimable again if nobody has responded to or released it.
                Only takes effect the first time this queue name is requested; later
                calls return the same cached AlchemicalQueue instance.

        Returns:
            AlchemicalQueue
        """
        if self._engine is None:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        if key not in self._queues:
            self._queues[key] = AlchemicalQueue(
                self._engine, self._qmodel, self._rmodel, key, visibility_timeout
            )

        return self._queues[key]

    def get_typed(
        self,
        key: str,
        typeof: Type[T],
        *,
        visibility_timeout: timedelta = DEFAULT_VISIBILITY_TIMEOUT,
    ) -> "AlchemicalQueue[T]":
        """Get a typed Queue instance

        Args:
            key (str): The name of the queue you wish to access.
            typeof (Type[T]): The type of the queue you wish to use
            visibility_timeout (timedelta, optional): see [get][alchemical_queues.AlchemicalQueues.get].

        Returns:
            AlchemicalQueue[T]
        """
        # pylint: disable=unused-argument
        return cast(
            AlchemicalQueue[T], self.get(key, visibility_timeout=visibility_timeout)
        )


class AlchemicalQueue(Generic[T]):
    """An Alchemical Queue. It is not intended to be initialized by a user, go through
    [AlchemicalQueues][alchemical_queues.AlchemicalQueues] instead."""

    def __init__(
        self,
        engine: Engine,
        model,
        response_model,
        name: str,
        visibility_timeout: timedelta = DEFAULT_VISIBILITY_TIMEOUT,
    ):
        self._engine = engine
        self._model = model
        self._response_model = response_model
        self._name = name
        self._visibility_timeout = visibility_timeout
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
            item (Any): The item you wish to add to the queue. It must be pickle-able.
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
            data=pickle.dumps(item),
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
        [respond][alchemical_queues.AlchemicalQueue.respond] or
        [release][alchemical_queues.AlchemicalQueue.release] for it, a later
        `get()` call will eventually claim it again instead of the entry being
        lost. Call `respond()` (if you have a result) or `release()` (if you
        don't) as soon as you're done with an entry to free it up immediately,
        rather than waiting out the timeout.

        Args:
            visibility_timeout (timedelta | None, optional): how long this entry
                stays claimed before it becomes claimable again. Defaults to the
                visibility_timeout this queue was obtained with (see
                [AlchemicalQueues.get][alchemical_queues.AlchemicalQueues.get]).

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

        with self._session() as session:
            # Picking the candidate row and claiming it happen in one atomic
            # UPDATE ... RETURNING statement, so two concurrent get() calls can
            # never claim the same entry. This needs no special transaction
            # isolation (previously SQLite needed a global BEGIN EXCLUSIVE,
            # which serialized every transaction on the engine, including ones
            # from unrelated code sharing the same engine).
            candidate = (
                select(self._model.entry_id)
                .filter(
                    self._model.queue_name == self._name,
                    or_(
                        self._model.schedule_at == None,  # pylint: disable=C0121
                        self._model.schedule_at <= timestamp,  # type: ignore
                    ),
                    or_(
                        self._model.claimed_until == None,  # pylint: disable=C0121
                        self._model.claimed_until <= timestamp,  # type: ignore
                    ),
                )
                .order_by(self._model.priority.desc(), self._model.entry_id.asc())  # type: ignore
                .limit(1)
            )

            if self._engine.dialect.name in _SKIP_LOCKED_DIALECTS:
                candidate = candidate.with_for_update(skip_locked=True)

            item = session.execute(
                update(self._model)
                .where(self._model.entry_id == candidate.scalar_subquery())
                .values(claimed_until=timestamp + timeout)
                .returning(self._model)
            ).scalar_one_or_none()

            if item is None:
                session.rollback()
                return None

            entry = AlchemicalEntry(item, pickle.loads(item.data))
            session.commit()

        return entry

    def release(self, entry_id: int) -> None:
        """Release a claimed entry, without recording a response for it.

        Use this when you called [get][alchemical_queues.AlchemicalQueue.get],
        decided you have nothing to respond with, and want the entry removed
        right away instead of waiting for its claim to time out and become
        available for another `get()` to pick up. Calling this on an entry_id
        that isn't currently claimed (or doesn't exist) is a no-op.

        Args:
            entry_id (int): the entry_id you wish to release.
        """

        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        with self._session() as session:
            session.execute(delete(self._model).where(self._model.entry_id == entry_id))
            session.commit()

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
        self, entry_id: int, response: Any, cleanup_at: Union[datetime, None] = None
    ) -> "AlchemicalResponse":
        """Send a response to a queue entry, and release it if it is still
        claimed (as from a prior [get][alchemical_queues.AlchemicalQueue.get]).
        Used to implement task queues.

        Args:
            entry_id (int): The entry_id you wish to respond to.
            response (Any): The response data. Must be pickable.
            cleanup_at (datetime, optional): The optional cleanup timestamp. After this time the response will be removed.
                                             By default it is not automatically cleaned up.

        Returns:
            AlchemicalResponse: the response as sent.
        """

        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        entry = self._response_model(
            entry_id=entry_id,
            delivered_at=datetime.now(),
            cleanup_at=cleanup_at,
            queue_name=self._name,
            data=pickle.dumps(response),
        )

        with self._session() as session:
            session.add(entry)
            session.execute(delete(self._model).where(self._model.entry_id == entry_id))
            session.commit()

            return AlchemicalResponse(entry, response)

    def responses(self, entry_id: int) -> List["AlchemicalResponse"]:
        """Obtain the response(s) to a specific queue entry.

        Returns:
            List[AlchemicalResponse]: A list of responses
        """
        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        with self._session() as session:
            now = datetime.now()

            session.execute(
                delete(self._response_model).where(
                    self._response_model.cleanup_at != None,  # pylint: disable=C0121
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
            return [AlchemicalResponse(e, pickle.loads(e.data)) for e in entries]


class AlchemicalEntry(Generic[T]):
    """An entry in a queue.

    Attributes:
        entry_id (int): the identifier of the entry. Guaranteed unique per [AlchemicalQueues][alchemical_queues.AlchemicalQueues] instance.
        enqueued_at (datetime): when the entry was added to the queue.
        schedule_at (datetime | None): do not remove the entry from the queue before this time.
        priority (int): the priority of the entry.
        data (T): the data stored in this entry.
    """

    __slots__ = ("data", "entry_id", "enqueued_at", "schedule_at", "priority")

    def __init__(
        self,
        entry,
        data: T,
    ):
        assert isinstance(entry.entry_id, int)

        self.entry_id: int = entry.entry_id
        self.enqueued_at: datetime = entry.enqueued_at
        self.schedule_at: Union[datetime, None] = entry.schedule_at
        self.priority: int = entry.priority
        self.data: T = data

    def __repr__(self):
        return (
            f"<{self.__class__.__module__}.{self.__class__.__name__} "
            f"entry_id={self.entry_id} enqueued_at={self.enqueued_at} "
            f"schedule_at={self.schedule_at} priority={self.priority}>"
        )


class AlchemicalResponse:
    """An response to a queue item. While you can use this as a user, it is probably most useful for the tasks submodule.

    Attributes:
        response_id (int): the identifier of the response.
        entry_id (int): the identifier of the associated entry.
        delivered_at (datetime): when the response was submitted.
        cleanup_at (datetime | None): autoremove this response after this time.
        data (Any): Response data.
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
        data: T,
    ):
        self.response_id = response.response_id
        self.entry_id = response.entry_id
        self.delivered_at = response.delivered_at
        self.cleanup_at = response.cleanup_at
        self.data = data
