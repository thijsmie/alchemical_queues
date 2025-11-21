"""Implementation of Alchemical Queues"""

import pickle
from datetime import datetime
from typing import Dict, List, Any, Type, cast, Generic, TypeVar, TYPE_CHECKING

from sqlalchemy import (
    delete,
    or_,
    func,
    DateTime,
    Integer,
    Text,
    Column,
    LargeBinary,
    select,
)
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase
from sqlalchemy.engine import Engine


T = TypeVar("T")


if TYPE_CHECKING:

    class _Entry(DeclarativeBase):
        entry_id = Column(Integer, primary_key=True, nullable=False, autoincrement=True)
        queue_name = Column(Text, nullable=False, index=True)

        enqueued_at = Column(DateTime(timezone=True), nullable=False)
        schedule_at = Column(DateTime(timezone=True), nullable=True)
        priority = Column(Integer, nullable=False)
        data = Column(LargeBinary)

    class _Response(DeclarativeBase):
        response_id = Column(
            Integer, primary_key=True, nullable=False, autoincrement=True
        )
        queue_name = Column(Text, nullable=False, index=True)
        entry_id = Column(Integer, index=True, nullable=False)

        delivered_at = Column(DateTime(timezone=True), nullable=False)
        cleanup_at = Column(DateTime(timezone=True), nullable=True)
        data = Column(LargeBinary)


def _generate_models(
    queue_tablename: str,
    response_tablename: str,
    base: Type[DeclarativeBase] | None = None,
) -> tuple[Type[DeclarativeBase], Type["_Entry"], Type["_Response"]]:
    if base is None:

        class base(DeclarativeBase):  # type: ignore[no-redef]
            pass

    class Entry(base):  # type: ignore[misc,valid-type]
        """SQLAlchemy model for a Queue Entry."""

        __tablename__: str = queue_tablename

        entry_id = Column(Integer, primary_key=True, nullable=False, autoincrement=True)
        queue_name = Column(Text, nullable=False, index=True)

        enqueued_at = Column(DateTime(timezone=True), nullable=False)
        schedule_at = Column(DateTime(timezone=True), nullable=True)
        priority = Column(Integer, nullable=False)
        data = Column(LargeBinary)

    class Response(base):  # type: ignore[misc,valid-type]
        """SQLAlchemy model for a Task Result."""

        __tablename__: str = response_tablename

        response_id = Column(
            Integer, primary_key=True, nullable=False, autoincrement=True
        )
        queue_name = Column(Text, nullable=False, index=True)
        entry_id = Column(Integer, index=True, nullable=False)

        delivered_at = Column(DateTime(timezone=True), nullable=False)
        cleanup_at = Column(DateTime(timezone=True), nullable=True)
        data = Column(LargeBinary)

    return base, Entry, Response  # type: ignore


class AlchemicalQueues:
    """The core entrypoint to Alchemical Queues."""

    def __init__(
        self,
        engine: Engine | None = None,
        queue_tablename: str = "AlchemicalQueue",
        response_tablename: str = "AlchemicalResult",
        base: Type[DeclarativeBase] | None = None,
    ) -> None:
        """Create the main queue entrypoint object.

        Args:
            engine (sqlalchemy.engine.Engine | None): The SQLAlchemy engine you want to use. May be left None and initialized later.
            queue_tablename (str): The name of the table AlchemicalQueues uses for queues.
            queue_tablename (str): The name of the table AlchemicalQueues uses for task results.
        """

        self._engine = engine
        self._get_prepped = False
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
        if not self._engine:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        self._base.metadata.create_all(self._engine)

    def clear(self) -> None:
        """Clear all entries from all queues and task results. Might fail-silent an update call."""
        if not self._engine:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        with Session(self._engine) as session:
            session.execute(delete(self._qmodel))
            session.execute(delete(self._rmodel))
            session.commit()

    def get(self, key: str) -> "AlchemicalQueue[Any]":
        """Get a Queue instance

        Args:
            key (str): The name of the queue you wish to access.

        Returns:
            AlchemicalQueue
        """
        if not self._engine:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        if key not in self._queues:
            self._queues[key] = AlchemicalQueue(
                self._engine, self._qmodel, self._rmodel, key
            )

        return self._queues[key]

    def get_typed(self, key: str, typeof: Type[T]) -> "AlchemicalQueue[T]":
        """Get a typed Queue instance

        Args:
            key (str): The name of the queue you wish to access.
            typeof (Type[T]): The type of the queue you wish to use

        Returns:
            AlchemicalQueue[T]
        """
        return cast(AlchemicalQueue[T], self.get(key))


class AlchemicalQueue(Generic[T]):
    """An Alchemical Queue. It is not intended to be initialized by a user, go through
    [AlchemicalQueues][alchemical_queues.AlchemicalQueues] instead."""

    def __init__(
        self,
        engine: Engine,
        model: Type["_Entry"],
        response_model: Type["_Response"],
        name: str,
    ):
        self._engine = engine
        self._model = model
        self._response_model = response_model
        self._name = name
        self._session = sessionmaker(
            engine, autocommit=False, autoflush=False, expire_on_commit=True
        )

    @property
    def name(self) -> str:
        """The name of the queue"""
        return self._name

    def put(
        self,
        item: T,
        *,
        schedule_at: datetime | None = None,
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
            session.flush()  # Flush to get the entry_id assigned

            # Create the result before commit to access attributes while object is still in session
            result = AlchemicalEntry(entry, item)

            session.commit()

        return result

    def get(self) -> "AlchemicalEntry[T] | None":
        """Get the highest priority entry out from the queue

        Returns:
            (AlchemicalEntry | None): The popped entry, or None if the queue is empty (or nothing is scheduled yet)
        """

        timestamp = datetime.now()

        with self._session() as session:
            # Use different strategies based on database support for SKIP LOCKED
            dialect_name = (
                session.bind.dialect.name
                if session.bind and session.bind.dialect
                else ""
            )

            if dialect_name in ("postgresql", "mysql", "oracle"):
                # For databases that support SKIP LOCKED with DELETE, use it for better concurrency
                # Build the subquery with FOR UPDATE SKIP LOCKED
                subquery = (
                    select(self._model.entry_id)
                    .filter(
                        self._model.queue_name == self._name,
                        or_(
                            self._model.schedule_at.is_(None),
                            self._model.schedule_at <= timestamp,  # type: ignore
                        ),
                    )
                    .order_by(self._model.priority.desc(), self._model.entry_id.asc())  # type: ignore
                    .limit(1)
                    .with_for_update(skip_locked=True)
                    .scalar_subquery()
                )
            else:
                # For SQLite and other databases, use plain subquery (still atomic via DELETE)
                subquery = (
                    select(self._model.entry_id)
                    .filter(
                        self._model.queue_name == self._name,
                        or_(
                            self._model.schedule_at.is_(None),
                            self._model.schedule_at <= timestamp,  # type: ignore
                        ),
                    )
                    .order_by(self._model.priority.desc(), self._model.entry_id.asc())  # type: ignore
                    .limit(1)
                    .scalar_subquery()
                )

            # Atomically delete and return the row
            delete_stmt = (
                delete(self._model)
                .where(self._model.entry_id == subquery)
                .returning(self._model)
            )

            result = session.execute(delete_stmt)
            item = result.fetchone()

            if item is None:
                session.rollback()
                return None

            # item is a Row object, access the model via item[0]
            model_obj = item[0]
            entry = AlchemicalEntry(model_obj, pickle.loads(model_obj.data))
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
                    select(func.count())
                    .select_from(self._model)
                    .where(self._model.queue_name == self._name)
                )
                or 0
            )

    def empty(self) -> bool:
        """Return `True` if the Queue is emtpy, `False` otherwise.

        Returns:
            bool: wether the Queue is empty.
        """
        return self.qsize() == 0

    def clear(self) -> None:
        """Clear all entries from this queue. Might fail-silent an update call."""

        with self._session() as session:
            session.execute(
                delete(self._model).where(self._model.queue_name == self._name)
            )
            session.commit()

    def respond(
        self, entry_id: int, response: Any, cleanup_at: datetime | None = None
    ) -> "AlchemicalResponse":
        """Send a response to a queue entry. Used to implement task queues.

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
                    self._response_model.cleanup_at.is_not(None),
                    self._response_model.cleanup_at < now,
                )
            )
            session.commit()
            entries = session.scalars(
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
        self.schedule_at: datetime | None = entry.schedule_at
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
