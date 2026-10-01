"""Tests for AsyncWorker and the async_task/AsyncTask/AsyncQueuedTask
scheduling API, mirroring test_task.py for the sync Worker."""

import time
from datetime import timedelta

import pytest

pytest_asyncio = pytest.importorskip("pytest_asyncio")
pytest.importorskip("aiosqlite")

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from alchemical_queues.asyncio import AsyncAlchemicalQueues  # noqa: E402
from alchemical_queues.tasks import TaskException  # noqa: E402
from alchemical_queues.tasks.asyncio import AsyncWorker  # noqa: E402

from .mocktasks_async import (  # noqa: E402
    fail_always,
    fail_once,
    increment,
    returns_none,
)


@pytest_asyncio.fixture
async def async_queue(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'async_task.db'}")
    q = AsyncAlchemicalQueues(engine=engine)
    await q.create_all()
    return q


@pytest.mark.asyncio
async def test_task(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get_task_queue("tasks")

    v = await increment(12).schedule(q)

    assert await v.done() is False
    assert await v.result() is None

    await AsyncWorker(q).work_one(False)

    assert await v.done() is True
    assert await v.result() == 13

    # The claim get() took to run the task should have been released, not
    # left sitting around until its visibility timeout.
    assert await q.qsize() == 0


@pytest.mark.asyncio
async def test_task_done_distinguishes_none_result_from_not_done(
    async_queue: AsyncAlchemicalQueues,
):
    q = async_queue.get_task_queue("tasks")

    v = await returns_none().schedule(q)

    assert await v.done() is False
    assert await v.result() is None  # not done yet

    await AsyncWorker(q).work_one(False)

    assert await v.done() is True
    assert await v.result() is None  # done, legitimately returned None


@pytest.mark.asyncio
async def test_task_retry(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get_task_queue("tasks")

    v = await fail_once(12).schedule(q, max_retries=1)

    await AsyncWorker(q).work_one(False)  # fail
    await AsyncWorker(q).work_one(False)  # success

    assert await v.result() == 12
    assert await q.qsize() == 0


@pytest.mark.asyncio
async def test_task_fail(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get_task_queue("tasks")

    v = await fail_always(12).schedule(
        q, max_retries=1, retry_in=timedelta(seconds=0.01)
    )

    assert await v.result() is None

    await AsyncWorker(q).work_one(False)  # fail
    time.sleep(0.1)
    await AsyncWorker(q).work_one(False)  # fail
    time.sleep(0.1)

    assert await v.done() is True
    result = await v.result()
    assert isinstance(result, TaskException)
    assert result.exception_type == "Exception"
    assert result.msg == "Always fails"

    assert await q.qsize() == 0


@pytest.mark.asyncio
async def test_task_namefail(async_queue: AsyncAlchemicalQueues):
    from alchemical_queues.tasks.asyncio import async_task

    q = async_queue.get_task_queue("tasks")

    class A:
        __module__ = "nothing"
        __qualname__ = "nothing"

    fn = async_task(A)
    v = await fn().schedule(q, max_retries=10)

    await AsyncWorker(q).work_one(False)  # fail

    result = await v.result()
    assert isinstance(result, TaskException)


@pytest.mark.asyncio
async def test_work_one_nonblocking_returns_when_empty(
    async_queue: AsyncAlchemicalQueues,
):
    q = async_queue.get_task_queue("tasks")
    # Should return immediately rather than hang, since block=False.
    await AsyncWorker(q, poll_every=timedelta(milliseconds=5)).work_one(False)


@pytest.mark.asyncio
async def test_keepalive_extends_the_claim_during_a_slow_handler(tmp_path):
    from .mocktasks_async import slow_task

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'keepalive.db'}")
    aq = AsyncAlchemicalQueues(engine=engine)
    await aq.create_all()
    q = aq.get_task_queue("tasks", visibility_timeout=timedelta(milliseconds=80))

    v = await slow_task(0.2).schedule(q)

    await AsyncWorker(
        q,
        poll_every=timedelta(milliseconds=10),
        keepalive_every=timedelta(milliseconds=20),
    ).work_one(False)

    result = await v.result()
    assert result == "done after 0.2s (retries=0)"
    assert await q.qsize() == 0
