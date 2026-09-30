"""Implementation of Alchemical Queues"""

import pickle
from datetime import datetime
from typing import Dict, List, Any, Union, Type, cast, Generic, TypeVar

from sqlalchemy import or_, event, DateTime, Integer, Text, LargeBinary
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase, Mapped, mapped_column
from sqlalchemy.engine import Engine


T = TypeVar("T")


def _generate_models(queue_tablename: str, response_tablename: str):
    # Each call gets its own DeclarativeBase, which implicitly creates its own
    # registry and metadata, so separate AlchemicalQueues instances never
    # clash even if they happen to use the same table names.
    class Base(DeclarativeBase):
        """SQLAlchemy model base class"""

    class Entry(Base):
        """SQLAlchemy model for a Queue Entry."""

        __tablename__: str = queue_tablename

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

    class Response(Base):
        """SQLAlchemy model for a Task Result."""

        __tablename__: str = response_tablename

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

    return Base, Entry, Response  # type: ignore


class AlchemicalQueues:
    """The core entrypoint to Alchemical Queues."""

    def __init__(
        self,
        engine: Union[Engine, None] = None,
        queue_tablename: str = "AlchemicalQueue",
        response_tablename: str = "AlchemicalResult",
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
            queue_tablename, response_tablename
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
        self._base.metadata.create_all(self._engine)

    def clear(self) -> None:
        """Clear all entries from all queues and task results. Might fail-silent an update call."""

        with Session(self._engine) as session:
            session.query(self._qmodel).delete()
            session.query(self._rmodel).delete()
            session.commit()

    def _prep_engine_for_get_transaction(self) -> None:
        if self._get_prepped:
            return

        if not self._engine:
            raise Exception("AlchemicalQueues SQLAlchemy engine was not initialized.")

        self._get_prepped = True

        if self._engine.driver == "pysqlite":

            @event.listens_for(self._engine, "begin")
            def do_begin(conn):
                conn.exec_driver_sql("BEGIN EXCLUSIVE")

    def get(self, key: str) -> "AlchemicalQueue[Any]":
        """Get a Queue instance

        Args:
            key (str): The name of the queue you wish to access.

        Returns:
            AlchemicalQueue
        """
        self._prep_engine_for_get_transaction()
        assert self._engine

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
        # pylint: disable=unused-argument
        return cast(AlchemicalQueue[T], self.get(key))


class AlchemicalQueue(Generic[T]):
    """An Alchemical Queue. It is not intended to be initialized by a user, go through
    [AlchemicalQueues][alchemical_queues.AlchemicalQueues] instead."""

    def __init__(self, engine: Engine, model, response_model, name: str):
        self._engine = engine
        self._model = model
        self._response_model = response_model
        self._name = name
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

    def get(self) -> Union["AlchemicalEntry[T]", None]:
        """Get the highest priority entry out from the queue

        Returns:
            (AlchemicalEntry | None): The popped entry, or None if the queue is empty (or nothing is scheduled yet)
        """

        timestamp = datetime.now()

        with self._session() as session:
            item = (
                session.query(self._model)
                .with_for_update(of=self._model, skip_locked=True)
                .filter(
                    self._model.queue_name == self._name,
                    or_(
                        self._model.schedule_at == None,  # pylint: disable=C0121
                        self._model.schedule_at <= timestamp,  # type: ignore
                    ),
                )
                .order_by(self._model.priority.desc(), self._model.entry_id.asc())  # type: ignore
                .limit(1)
                .first()
            )

            if item is None:
                session.rollback()
                return None

            entry = AlchemicalEntry(item, pickle.loads(item.data))
            session.delete(item)
            session.commit()

        return entry

    def qsize(self) -> int:
        """Return the approximate size of this queue.

        Returns:
            int: Queue size.
        """
        with self._session() as session:
            return (
                session.query(self._model)
                .where(self._model.queue_name == self._name)
                .count()
            )

    def empty(self) -> bool:
        """Return `True` if the Queue is emtpy, `False` otherwise. More efficient than
        `qsize() > 0`.

        Returns:
            bool: wether the Queue is empty.
        """
        with self._session() as session:
            return (
                session.query(self._model)
                .where(self._model.queue_name == self._name)
                .limit(1)
                .count()
                == 0
            )

    def clear(self) -> None:
        """Clear all entries from this queue. Might fail-silent an update call."""

        with self._session() as session:
            session.query(self._model).where(
                self._model.queue_name == self._name
            ).delete()
            session.commit()

    def respond(
        self, entry_id: int, response: Any, cleanup_at: Union[datetime, None] = None
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

            session.query(self._response_model).where(
                self._response_model.cleanup_at != None,  # pylint: disable=C0121
                self._response_model.cleanup_at < now,
            ).delete()
            entries = (
                session.query(self._response_model)
                .where(
                    self._response_model.queue_name == self._name,
                    self._response_model.entry_id == entry_id,
                )
                .all()
            )
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
