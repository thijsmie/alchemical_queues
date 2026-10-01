"""Async counterparts of [AlchemicalQueues][alchemical_queues.AlchemicalQueues]
and friends, built on `sqlalchemy.ext.asyncio` (`AsyncEngine`/`AsyncSession`)
instead of their sync equivalents.

The table models (via `_generate_models`), `AlchemicalEntry`,
`AlchemicalResponse`, and `ClaimExpired` are backend-agnostic and shared
unchanged with the sync implementation in `.main` -- only the session
lifecycle and the methods that use it are duplicated here. The statements
each method executes mirror `.main` exactly (same filters, same ordering,
same atomic UPDATE/DELETE ... RETURNING pattern); only `await`/`AsyncSession`
differ, see `.main` for why each query is shaped the way it is.
"""

import asyncio
import functools
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Generic, List, Type, TypeVar, Union, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from .main import (
    _MAX_DEADLOCK_RETRIES,
    _SKIP_LOCKED_DIALECTS,
    AlchemicalEntry,
    AlchemicalResponse,
    ClaimExpired,
    _deadlock_backoff,
    _generate_models,
    _is_deadlock,
    _new_claim_token,
    _supports_returning,
    _validate_queue_name,
)
from .serializers import PickleSerializer, Serializer

DEFAULT_VISIBILITY_TIMEOUT = timedelta(minutes=5)
DEFAULT_SERIALIZER: Serializer[Any] = PickleSerializer()

T = TypeVar("T")
_F = TypeVar("_F", bound=Callable[..., Any])


def _retry_on_deadlock(fn: _F) -> _F:
    # Async counterpart of `.main._retry_on_deadlock` -- same rationale,
    # just awaiting the wrapped coroutine function instead of calling it.
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(_MAX_DEADLOCK_RETRIES):
            try:
                return await fn(*args, **kwargs)
            except OperationalError as exc:
                if attempt == _MAX_DEADLOCK_RETRIES - 1 or not _is_deadlock(exc):
                    raise
                await asyncio.sleep(_deadlock_backoff(attempt))

    return cast(_F, wrapper)


R = TypeVar("R")


class AsyncAlchemicalQueues:
    """The async entrypoint to Alchemical Queues, built on an `AsyncEngine`.
    See [AlchemicalQueues][alchemical_queues.AlchemicalQueues] for the sync
    equivalent -- the two have the same API, methods here are just
    coroutines.
    """

    def __init__(
        self,
        engine: Union[AsyncEngine, None] = None,
        queue_tablename: str = "AlchemicalQueue",
        response_tablename: str = "AlchemicalResult",
        base: Union[Type[DeclarativeBase], None] = None,
    ) -> None:
        """Create the async queue entrypoint object.

        Args:
            engine (AsyncEngine | None): The SQLAlchemy async engine you want
                to use (e.g. `create_async_engine("sqlite+aiosqlite:///...")`).
                May be left None and initialized later with `set_engine`.
            queue_tablename (str): see [AlchemicalQueues][alchemical_queues.AlchemicalQueues].
            response_tablename (str): see [AlchemicalQueues][alchemical_queues.AlchemicalQueues].
            base (Type[DeclarativeBase] | None, optional): see
                [AlchemicalQueues][alchemical_queues.AlchemicalQueues].
        """
        self._engine = engine
        self._base, self._qmodel, self._rmodel = _generate_models(
            queue_tablename, response_tablename, base
        )
        self._queues: Dict[str, "AsyncAlchemicalQueue"] = {}
        self._task_queues: Dict[str, "AsyncAlchemicalTaskQueue"] = {}

    def set_engine(self, engine: AsyncEngine) -> None:
        """Set the SQLAlchemy async engine post-initialization.

        Args:
            engine (AsyncEngine): The SQLAlchemy async engine you want to use.

        Raises:
            Exception: when the engine was already set.
        """
        if self._engine is not None:
            raise Exception(
                "Cannot set the engine on AsyncAlchemicalQueues more than once!"
            )

        self._engine = engine

    async def create_all(self) -> None:
        """Create the needed SQLAlchemy tables. You would normally await this
        when you are also creating your own tables."""
        if self._engine is None:
            raise Exception(
                "AsyncAlchemicalQueues SQLAlchemy engine was not initialized."
            )

        async with self._engine.begin() as conn:
            await conn.run_sync(self._base.metadata.create_all)

    @_retry_on_deadlock
    async def clear(self) -> None:
        """Clear all entries from all queues and task results. Might fail-silent an update call."""
        if self._engine is None:
            raise Exception(
                "AsyncAlchemicalQueues SQLAlchemy engine was not initialized."
            )

        async with AsyncSession(self._engine) as session:
            await session.execute(delete(self._qmodel))
            await session.execute(delete(self._rmodel))
            await session.commit()

    def get(
        self, key: str, *, serializer: Serializer[Any] = DEFAULT_SERIALIZER
    ) -> "AsyncAlchemicalQueue[Any]":
        """Get a plain async Queue instance. See
        [AlchemicalQueues.get][alchemical_queues.AlchemicalQueues.get] for the
        sync equivalent.

        Returns:
            AsyncAlchemicalQueue
        """
        if self._engine is None:
            raise Exception(
                "AsyncAlchemicalQueues SQLAlchemy engine was not initialized."
            )

        if key not in self._queues:
            _validate_queue_name(key)
            self._queues[key] = AsyncAlchemicalQueue(
                self._engine, self._qmodel, key, serializer=serializer
            )

        return self._queues[key]

    def get_typed(
        self,
        key: str,
        typeof: Type[T],
        *,
        serializer: Union[Serializer[T], None] = None,
    ) -> "AsyncAlchemicalQueue[T]":
        """Get a typed async Queue instance. See
        [AlchemicalQueues.get_typed][alchemical_queues.AlchemicalQueues.get_typed]
        for the sync equivalent.

        Returns:
            AsyncAlchemicalQueue[T]
        """
        # pylint: disable=unused-argument
        return cast(
            "AsyncAlchemicalQueue[T]",
            self.get(key, serializer=serializer or DEFAULT_SERIALIZER),
        )

    def get_serialized(
        self, key: str, serializer: Serializer[T]
    ) -> "AsyncAlchemicalQueue[T]":
        """Get an async Queue instance typed by its `serializer`. See
        [AlchemicalQueues.get_serialized][alchemical_queues.AlchemicalQueues.get_serialized]
        for the sync equivalent.

        Returns:
            AsyncAlchemicalQueue[T]
        """
        return cast("AsyncAlchemicalQueue[T]", self.get(key, serializer=serializer))

    def get_task_queue(
        self,
        key: str,
        *,
        visibility_timeout: timedelta = DEFAULT_VISIBILITY_TIMEOUT,
        serializer: Serializer[Any] = DEFAULT_SERIALIZER,
        response_serializer: Serializer[Any] = DEFAULT_SERIALIZER,
    ) -> "AsyncAlchemicalTaskQueue[Any, Any]":
        """Get an async task queue instance. See
        [AlchemicalQueues.get_task_queue][alchemical_queues.AlchemicalQueues.get_task_queue]
        for the sync equivalent.

        Returns:
            AsyncAlchemicalTaskQueue
        """
        if self._engine is None:
            raise Exception(
                "AsyncAlchemicalQueues SQLAlchemy engine was not initialized."
            )

        if key not in self._task_queues:
            _validate_queue_name(key)
            self._task_queues[key] = AsyncAlchemicalTaskQueue(
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
    ) -> "AsyncAlchemicalTaskQueue[T, Any]":
        """Get a typed async task queue instance. See
        [AlchemicalQueues.get_task_queue_typed][alchemical_queues.AlchemicalQueues.get_task_queue_typed]
        for the sync equivalent.

        Returns:
            AsyncAlchemicalTaskQueue[T, Any]
        """
        # pylint: disable=unused-argument
        return cast(
            "AsyncAlchemicalTaskQueue[T, Any]",
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
    ) -> "AsyncAlchemicalTaskQueue[T, R]":
        """Get an async task queue instance typed by its `serializer`/
        `response_serializer`. See
        [AlchemicalQueues.get_task_queue_serialized][alchemical_queues.AlchemicalQueues.get_task_queue_serialized]
        for the sync equivalent.

        Returns:
            AsyncAlchemicalTaskQueue[T, R]
        """
        return cast(
            "AsyncAlchemicalTaskQueue[T, R]",
            self.get_task_queue(
                key,
                visibility_timeout=visibility_timeout,
                serializer=serializer,
                response_serializer=response_serializer or DEFAULT_SERIALIZER,
            ),
        )


class AsyncAlchemicalQueue(Generic[T]):
    """Async equivalent of [AlchemicalQueue][alchemical_queues.AlchemicalQueue].
    Not intended to be initialized by a user, go through
    [AsyncAlchemicalQueues.get][alchemical_queues.aio.AsyncAlchemicalQueues.get]
    instead.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        model,
        name: str,
        serializer: Serializer[T] = DEFAULT_SERIALIZER,
    ):
        self._engine = engine
        self._model = model
        self._name = name
        self._serializer = serializer
        self._session = async_sessionmaker(
            engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @property
    def name(self) -> str:
        """The name of the queue"""
        return self._name

    @_retry_on_deadlock
    async def put(
        self,
        item: T,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
    ) -> "AlchemicalEntry[T]":
        """Put an entry into the queue. See
        [AlchemicalQueue.put][alchemical_queues.AlchemicalQueue.put] for
        details.

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

        async with self._session() as session:
            session.add(entry)
            await session.commit()

            return AlchemicalEntry(entry, item)

    @_retry_on_deadlock
    async def get(self) -> Union["AlchemicalEntry[T]", None]:
        """Get the highest priority entry out from the queue, removing it.
        See [AlchemicalQueue.get][alchemical_queues.AlchemicalQueue.get] for
        details.

        Returns:
            (AlchemicalEntry | None)
        """
        timestamp = datetime.now()

        async with self._session() as session:
            query = (
                select(self._model)
                .filter(
                    self._model.queue_name == self._name,
                    or_(
                        self._model.schedule_at == None,  # noqa: E711
                        self._model.schedule_at <= timestamp,  # type: ignore
                    ),
                )
                .order_by(self._model.priority.desc(), self._model.entry_id.asc())  # type: ignore
                .limit(1)
            )

            if self._engine.dialect.name in _SKIP_LOCKED_DIALECTS:
                query = query.with_for_update(skip_locked=True)

            if _supports_returning(self._engine, "delete"):
                candidate = query.with_only_columns(self._model.entry_id)
                item = (
                    await session.execute(
                        delete(self._model)
                        .where(self._model.entry_id == candidate.scalar_subquery())
                        .returning(self._model)
                    )
                ).scalar_one_or_none()
            else:
                # MySQL has no RETURNING support at all: lock and fetch the
                # full candidate row first instead, then delete it by id in
                # the same transaction. The row stays locked throughout, so
                # the same atomicity guarantee holds.
                item = (await session.execute(query)).scalar_one_or_none()
                if item is not None:
                    await session.delete(item)

            if item is None:
                await session.rollback()
                return None

            entry = AlchemicalEntry(item, self._serializer.loads(item.data))
            await session.commit()

        return entry

    async def qsize(self) -> int:
        """Return the approximate size of this queue."""
        async with self._session() as session:
            return (
                await session.scalar(
                    select(func.count())  # pylint: disable=not-callable
                    .select_from(self._model)
                    .where(self._model.queue_name == self._name)
                )
                or 0
            )

    async def empty(self) -> bool:
        """Return `True` if the Queue is empty, `False` otherwise."""
        async with self._session() as session:
            return (
                await session.scalar(
                    select(self._model.entry_id)
                    .where(self._model.queue_name == self._name)
                    .limit(1)
                )
            ) is None

    @_retry_on_deadlock
    async def clear(self) -> None:
        """Clear all entries from this queue. Might fail-silent an update call."""
        async with self._session() as session:
            await session.execute(
                delete(self._model).where(self._model.queue_name == self._name)
            )
            await session.commit()


class AsyncAlchemicalTaskQueue(Generic[T, R]):
    """Async equivalent of
    [AlchemicalTaskQueue][alchemical_queues.AlchemicalTaskQueue]. Not
    intended to be initialized by a user, go through
    [AsyncAlchemicalQueues.get_task_queue][alchemical_queues.aio.AsyncAlchemicalQueues.get_task_queue]
    instead.
    """

    def __init__(
        self,
        engine: AsyncEngine,
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
        self._session = async_sessionmaker(
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

    @_retry_on_deadlock
    async def put(
        self,
        item: T,
        *,
        schedule_at: Union[datetime, None] = None,
        priority: int = 0,
    ) -> "AlchemicalEntry[T]":
        """Put an entry into the queue. See
        [AlchemicalTaskQueue.put][alchemical_queues.AlchemicalTaskQueue.put]
        for details.

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

        async with self._session() as session:
            session.add(entry)
            await session.commit()

            return AlchemicalEntry(entry, item)

    @_retry_on_deadlock
    async def get(
        self, *, visibility_timeout: Union[timedelta, None] = None
    ) -> Union["AlchemicalEntry[T]", None]:
        """Claim the highest priority entry from the queue. See
        [AlchemicalTaskQueue.get][alchemical_queues.AlchemicalTaskQueue.get]
        for details.

        Returns:
            (AlchemicalEntry | None)
        """
        timestamp = datetime.now()
        timeout = (
            visibility_timeout
            if visibility_timeout is not None
            else self._visibility_timeout
        )
        token = _new_claim_token()

        async with self._session() as session:
            query = (
                select(self._model)
                .filter(
                    self._model.queue_name == self._name,
                    or_(
                        self._model.schedule_at == None,  # noqa: E711
                        self._model.schedule_at <= timestamp,  # type: ignore
                    ),
                    or_(
                        self._model.claimed_until == None,  # noqa: E711
                        self._model.claimed_until <= timestamp,  # type: ignore
                    ),
                )
                .order_by(self._model.priority.desc(), self._model.entry_id.asc())  # type: ignore
                .limit(1)
            )

            if self._engine.dialect.name in _SKIP_LOCKED_DIALECTS:
                query = query.with_for_update(skip_locked=True)

            if _supports_returning(self._engine, "update"):
                candidate = query.with_only_columns(self._model.entry_id)
                item = (
                    await session.execute(
                        update(self._model)
                        .where(self._model.entry_id == candidate.scalar_subquery())
                        .values(claimed_until=timestamp + timeout, claim_token=token)
                        .returning(self._model)
                    )
                ).scalar_one_or_none()
            else:
                # MySQL has no RETURNING support at all, and MariaDB only
                # supports it for DELETE/INSERT, not UPDATE: lock and fetch
                # the full candidate row first instead, set the claim fields
                # on the already-loaded instance, and let the session flush
                # that as a plain UPDATE on commit. The row stays locked
                # throughout, so the same atomicity guarantee holds.
                item = (await session.execute(query)).scalar_one_or_none()
                if item is not None:
                    item.claimed_until = timestamp + timeout
                    item.claim_token = token

            if item is None:
                await session.rollback()
                return None

            entry = AlchemicalEntry(item, self._serializer.loads(item.data))
            await session.commit()

        return entry

    @_retry_on_deadlock
    async def release(self, entry_id: int, claim_token: int) -> None:
        """Release a claimed entry back to the queue. See
        [AlchemicalTaskQueue.release][alchemical_queues.AlchemicalTaskQueue.release]
        for details.

        Raises:
            ClaimExpired: if entry_id isn't currently claimed under claim_token.
        """
        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        async with self._session() as session:
            result = await session.execute(
                update(self._model)
                .where(
                    self._model.entry_id == entry_id,
                    self._model.claim_token == claim_token,
                )
                .values(claimed_until=None, claim_token=None)
            )
            await session.commit()

        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ClaimExpired(entry_id)

    @_retry_on_deadlock
    async def discard(self, entry_id: int, claim_token: int) -> None:
        """Remove a claimed entry from the queue entirely. See
        [AlchemicalTaskQueue.discard][alchemical_queues.AlchemicalTaskQueue.discard]
        for details.

        Raises:
            ClaimExpired: if entry_id isn't currently claimed under claim_token.
        """
        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        async with self._session() as session:
            result = await session.execute(
                delete(self._model).where(
                    self._model.entry_id == entry_id,
                    self._model.claim_token == claim_token,
                )
            )
            await session.commit()

        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ClaimExpired(entry_id)

    @_retry_on_deadlock
    async def extend(
        self,
        entry_id: int,
        claim_token: int,
        *,
        by: Union[timedelta, None] = None,
    ) -> None:
        """Extend a claim's visibility timeout. See
        [AlchemicalTaskQueue.extend][alchemical_queues.AlchemicalTaskQueue.extend]
        for details.

        Raises:
            ClaimExpired: if entry_id isn't currently claimed under claim_token.
        """
        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        timeout = by if by is not None else self._visibility_timeout
        new_claimed_until = datetime.now() + timeout

        async with self._session() as session:
            result = await session.execute(
                update(self._model)
                .where(
                    self._model.entry_id == entry_id,
                    self._model.claim_token == claim_token,
                )
                .values(claimed_until=new_claimed_until)
            )
            await session.commit()

        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ClaimExpired(entry_id)

    async def qsize(self) -> int:
        """Return the approximate size of this queue."""
        async with self._session() as session:
            return (
                await session.scalar(
                    select(func.count())  # pylint: disable=not-callable
                    .select_from(self._model)
                    .where(self._model.queue_name == self._name)
                )
                or 0
            )

    async def empty(self) -> bool:
        """Return `True` if the Queue is empty, `False` otherwise."""
        async with self._session() as session:
            return (
                await session.scalar(
                    select(self._model.entry_id)
                    .where(self._model.queue_name == self._name)
                    .limit(1)
                )
            ) is None

    @_retry_on_deadlock
    async def clear(self) -> None:
        """Clear all entries from this queue. Might fail-silent an update call."""
        async with self._session() as session:
            await session.execute(
                delete(self._model).where(self._model.queue_name == self._name)
            )
            await session.commit()

    @_retry_on_deadlock
    async def respond(
        self, entry_id: int, response: R, cleanup_at: Union[datetime, None] = None
    ) -> "AlchemicalResponse[R]":
        """Record a response for a queue entry. See
        [AlchemicalTaskQueue.respond][alchemical_queues.AlchemicalTaskQueue.respond]
        for details.

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

        async with self._session() as session:
            session.add(entry)
            await session.commit()

            return AlchemicalResponse(entry, response)

    @_retry_on_deadlock
    async def responses(self, entry_id: int) -> List["AlchemicalResponse[R]"]:
        """Obtain the response(s) to a specific queue entry.

        Returns:
            List[AlchemicalResponse[R]]: A list of responses
        """
        if not isinstance(entry_id, int):
            raise TypeError(f"entry_id={entry_id} should be integer")

        async with self._session() as session:
            now = datetime.now()

            await session.execute(
                delete(self._response_model).where(
                    self._response_model.cleanup_at != None,  # noqa: E711
                    self._response_model.cleanup_at < now,
                )
            )
            await session.commit()

            entries: Any = (
                await session.scalars(
                    select(self._response_model).where(
                        self._response_model.queue_name == self._name,
                        self._response_model.entry_id == entry_id,
                    )
                )
            ).all()
            return [
                AlchemicalResponse(e, self._response_serializer.loads(e.data))
                for e in entries
            ]
