"""Implementation of Alchemical Queues"""

import pickle
from datetime import datetime
from typing import Callable, Dict, Any, Union, Tuple, Type, cast
from dataclasses import dataclass

from sqlalchemy import or_, event, DateTime, Integer, Text, Column, LargeBinary
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.engine import Engine
from sqlalchemy.orm import registry
from sqlalchemy.orm.decl_api import DeclarativeMeta


@dataclass
class TaskBase:
    """Typing helper, for correctly typed attributes of SQLAlchemy type. Not actually used"""

    enqueued_at: datetime
    expected_at: Union[datetime, None]
    schedule_at: Union[datetime, None]
    queue_name: str
    data: bytes
    entry_id: Union[int, None] = None
    dequeued_at: Union[datetime, None] = None


def _generate_models(tablename: str) -> Tuple[DeclarativeMeta, Type[TaskBase]]:
    """Generate SQLAlchemy task queue model

    Parameters
    ----------
    tablename : str
        The User-Defined tablename to use for the task queue

    Returns
    -------
    (baseclass, model)
        The base to register to the SQLAlchemy engine, the model to use in queries.
    """

    mapper_registry = registry()

    class Base(metaclass=DeclarativeMeta):
        """SQLAlchemy model base class"""

        __abstract__ = True

        registry = mapper_registry
        metadata = mapper_registry.metadata

        __init__ = mapper_registry.constructor

    class Task(Base):
        """SQLAlchemy model for a task."""

        __tablename__: str = tablename

        entry_id = Column(Integer, primary_key=True, nullable=False, autoincrement=True)
        enqueued_at = Column(DateTime(timezone=True), nullable=False)
        dequeued_at = Column(DateTime(timezone=True), nullable=True)
        expected_at = Column(DateTime(timezone=True), nullable=True)
        schedule_at = Column(DateTime(timezone=True), nullable=True)
        queue_name = Column(Text, nullable=False)
        data = Column(LargeBinary)

    return Base, Task  # type: ignore


class AlchemicalQueues:
    """The core entrypoint to Alchemical Queues."""

    def __init__(
        self, engine: Union[Engine, None] = None, tablename: str = "AlchemicalQueue"
    ) -> None:
        self._engine = engine
        self._get_prepped = False
        self._base, self._model = _generate_models(tablename)
        self._queues: Dict[str, "AlchemicalQueue"] = {}

    def set_engine(self, engine: Engine) -> None:
        """Set the SQLAlchemy engine post-initialization

        Parameters
        ----------
        engine : Engine
            The SQLAlchemy engine you want to use.

        Raises
        ------
        Exception
            This function will raise a generic Exception when the engine was already set.
        """

        if self._engine is not None:
            raise Exception(
                "Cannot set the engine on Alchemical Queues more than once!"
            )

        self._engine = engine

    def create(self) -> None:
        """Create the needed SQLAlchemy table. You would normally call this
        when you are also creating your own tables, e.g. db.create_all()."""
        self._base.metadata.create_all(self._engine)

    def clear(self) -> None:
        """Clear all entries from all queues. Might fail-silent an update call."""

        with Session(self._engine) as session:
            session.query(self._model).delete()
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

    def __getitem__(self, key: str) -> "AlchemicalQueue":
        self._prep_engine_for_get_transaction()
        assert self._engine

        if key not in self._queues:
            self._queues[key] = AlchemicalQueue(self._engine, self._model, key)

        return self._queues[key]


class AlchemicalQueue:
    """An Alchemical Queue. It is not intended to be initialized by a user, go through AlchemicalQueues instead."""

    def __init__(self, engine: Engine, model: Type[TaskBase], name: str):
        self._engine = engine
        self._model = model
        self._name = name
        self._session = sessionmaker(
            engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )

    @property
    def name(self) -> str:
        """The name of the queue"""
        return self._name

    def put(
        self,
        item: Any,
        *,
        schedule_at: Union[datetime, None] = None,
        expected_at: Union[datetime, None] = None,
    ) -> int:
        """Put an entry into the queue"""

        entry = self._model(
            enqueued_at=datetime.now(),
            schedule_at=schedule_at,
            expected_at=expected_at or datetime.min,
            queue_name=self._name,
            data=pickle.dumps(item),
        )

        with self._session() as session:
            session.add(entry)
            session.commit()

            return cast(int, entry.entry_id)

    def get(self) -> Union["AlchemicalEntry", None]:
        """Get the highest priority entry out from the queue, or None if no entries exist"""

        timestamp = datetime.now()

        with self._session() as session:
            item = (
                session.query(self._model)
                .with_for_update(of=self._model, skip_locked=True)
                .filter(
                    self._model.queue_name == self._name,
                    self._model.dequeued_at == None,  # pylint: disable=C0121
                    or_(
                        self._model.schedule_at == None,  # pylint: disable=C0121
                        self._model.schedule_at <= timestamp,  # type: ignore
                    ),
                )
                .order_by(self._model.expected_at.asc(), self._model.entry_id.asc())  # type: ignore
                .limit(1)
                .first()
            )

            if item is None:
                session.rollback()
                return None

            item.dequeued_at = timestamp
            session.commit()

        return AlchemicalEntry(
            item,
            pickle.loads(item.data),
            self.update,
        )

    def update(self, entry_id: int, data: Any) -> int:
        """Update entry data."""

        ser = pickle.dumps(data)

        with self._session() as session:
            session.query(self._model).where(self._model.entry_id == entry_id).update(
                {"data": ser}
            )
            session.commit()

        return len(ser)

    def clear(self) -> None:
        """Clear all entries from this queue. Might fail-silent an update call."""

        with self._session() as session:
            session.query(self._model).where(
                self._model.queue_name == self._name
            ).delete()
            session.commit()

    def fetch(
        self, entry_id: int, *, editable: bool = False
    ) -> Union["ReadOnlyAlchemicalEntry", "AlchemicalEntry", None]:
        """Fetch a previously inserted item from the queue that may or may not already be popped from the queue.

        Parameters
        ----------
        entry_id: int
            The id of the entry you wish to get. Can be the return value of a ``.put`` or ``.get().entry_id``.

        editable: bool = False
            Whether the data of this entry should be editable. Editing data can cause inconsistencies between distributed users.

        Returns
        -------
        ReadOnlyAlchemicalEntry, AlchemicalEntry, None
            Return an Entry, editable or not depending on the editable parameter, or return None when an entry with this id doesn't exist.
        """

        with self._session() as session:
            item = session.query(self._model).get(entry_id)

            if item is None:
                return None

            if editable:
                return AlchemicalEntry(
                    item,
                    pickle.loads(item.data),
                    self.update,
                )

            return ReadOnlyAlchemicalEntry(
                item,
                pickle.loads(item.data),
                None,
            )


class AlchemicalEntry:
    """An entry in a queue."""

    __slots__ = (
        "_data",
        "_size",
        "_update",
        "entry_id",
        "enqueued_at",
        "schedule_at",
        "expected_at",
    )

    def __init__(
        self,
        task: TaskBase,
        data: Any,
        update: Union[Callable[[int, Any], int], None],
    ):
        assert isinstance(task.entry_id, int)

        self.entry_id: int = task.entry_id
        self.enqueued_at: datetime = task.enqueued_at
        self.schedule_at: Union[datetime, None] = task.schedule_at
        self.expected_at: Union[datetime, None] = task.expected_at
        self._data: Any = data
        self._update: Union[Callable[[int, Any], int], None] = update

    def __repr__(self):
        return (
            f"<{self.__class__.__module__}.{self.__class__.__name__} "
            f"entry_id={self.entry_id} enqueued_at={self.enqueued_at} "
            f"schedule_at={self.schedule_at} expected_at={self.expected_at}>"
        )

    @property
    def data(self) -> Any:
        """Return the data stored in this entry of the queue."""
        return self._data

    @data.setter
    def data(self, value):
        """Set the data stored in this entry. The changes are pushed to the database."""
        self._update(self.entry_id, value)
        self._data = value


class ReadOnlyAlchemicalEntry(AlchemicalEntry):
    """A read-only entry in a queue"""

    @AlchemicalEntry.data.setter  # type: ignore
    def data(self, value):
        raise NotImplementedError
