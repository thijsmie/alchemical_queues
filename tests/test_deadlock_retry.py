"""Deterministic tests for the MySQL/MariaDB deadlock-retry decorators
(`main._retry_on_deadlock`, `aio._retry_on_deadlock`). The backend test
suites exercise these against a real deadlock on mariadb, but that doesn't
guarantee one actually occurs on a given run -- these tests drive the
decorators directly with a fake deadlock-shaped `OperationalError`, so a
regression in the retry count, backoff, or exception filtering can't slip
through undetected.
"""

import pytest
from sqlalchemy.exc import OperationalError

from alchemical_queues.main import _MAX_DEADLOCK_RETRIES, _retry_on_deadlock


def _deadlock_error() -> OperationalError:
    return OperationalError(
        "SELECT 1",
        {},
        Exception("Deadlock found when trying to get lock; try restarting transaction"),
    )


def _other_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("Connection refused"))


def test_retries_and_eventually_succeeds():
    calls = []

    @_retry_on_deadlock
    def flaky():
        calls.append(None)
        if len(calls) < 3:
            raise _deadlock_error()
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3


def test_exhausting_retries_raises():
    calls = []

    @_retry_on_deadlock
    def always_deadlocks():
        calls.append(None)
        raise _deadlock_error()

    with pytest.raises(OperationalError):
        always_deadlocks()

    assert len(calls) == _MAX_DEADLOCK_RETRIES


def test_non_deadlock_error_propagates_immediately():
    calls = []

    @_retry_on_deadlock
    def raises_unrelated_error():
        calls.append(None)
        raise _other_error()

    with pytest.raises(OperationalError):
        raises_unrelated_error()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_retries_and_eventually_succeeds():
    pytest.importorskip("pytest_asyncio")
    from alchemical_queues.aio import _retry_on_deadlock as _async_retry_on_deadlock

    calls = []

    @_async_retry_on_deadlock
    async def flaky():
        calls.append(None)
        if len(calls) < 3:
            raise _deadlock_error()
        return "ok"

    assert await flaky() == "ok"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_async_exhausting_retries_raises():
    pytest.importorskip("pytest_asyncio")
    from alchemical_queues.aio import _retry_on_deadlock as _async_retry_on_deadlock

    calls = []

    @_async_retry_on_deadlock
    async def always_deadlocks():
        calls.append(None)
        raise _deadlock_error()

    with pytest.raises(OperationalError):
        await always_deadlocks()

    assert len(calls) == _MAX_DEADLOCK_RETRIES


@pytest.mark.asyncio
async def test_async_non_deadlock_error_propagates_immediately():
    pytest.importorskip("pytest_asyncio")
    from alchemical_queues.aio import _retry_on_deadlock as _async_retry_on_deadlock

    calls = []

    @_async_retry_on_deadlock
    async def raises_unrelated_error():
        calls.append(None)
        raise _other_error()

    with pytest.raises(OperationalError):
        await raises_unrelated_error()

    assert len(calls) == 1
