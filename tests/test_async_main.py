"""Tests for AsyncAlchemicalQueues/AsyncAlchemicalQueue/AsyncAlchemicalTaskQueue
-- the async counterparts of AlchemicalQueues/AlchemicalQueue/AlchemicalTaskQueue
built on sqlalchemy.ext.asyncio, mirroring test_basic.py/test_claims.py for
the sync classes.
"""

import pytest

pytest_asyncio = pytest.importorskip("pytest_asyncio")
pytest.importorskip("aiosqlite")

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from alchemical_queues import ClaimExpired  # noqa: E402
from alchemical_queues.aio import AsyncAlchemicalQueues  # noqa: E402


@pytest.fixture
def async_engine_factory(tmp_path):
    def factory():
        return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'async.db'}")

    return factory


@pytest_asyncio.fixture
async def async_queue(async_engine_factory):
    q = AsyncAlchemicalQueues(engine=async_engine_factory())
    await q.create_all()
    return q


@pytest.mark.asyncio
async def test_create_queue(async_engine_factory):
    aq = AsyncAlchemicalQueues(engine=async_engine_factory())
    await aq.create_all()


@pytest.mark.asyncio
async def test_create_queue_late_init(async_engine_factory):
    aq = AsyncAlchemicalQueues()
    aq.set_engine(async_engine_factory())
    await aq.create_all()


@pytest.mark.asyncio
async def test_no_multiset(async_engine_factory):
    aq = AsyncAlchemicalQueues(engine=async_engine_factory())

    with pytest.raises(Exception):  # noqa: B017
        aq.set_engine(async_engine_factory())


@pytest.mark.asyncio
async def test_no_uninitialized():
    aq = AsyncAlchemicalQueues()

    with pytest.raises(Exception):  # noqa: B017
        aq.get("test")


@pytest.mark.asyncio
async def test_queue_name(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get("test")
    assert q.name == "test"


@pytest.mark.asyncio
async def test_put_get_data(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get("test")
    await q.put(1)
    job = await q.get()

    assert job
    assert job.data == 1


@pytest.mark.asyncio
async def test_put_get_data_typed(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get_typed("test", dict)
    await q.put({"1": 1})
    job = await q.get()

    assert job
    assert job.data["1"] == 1


@pytest.mark.asyncio
async def test_put_get_data_ordered(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get("test")
    await q.put(1)
    await q.put(2)

    e1 = await q.get()
    e2 = await q.get()

    assert e1 and e1.data == 1
    assert e2 and e2.data == 2


@pytest.mark.asyncio
async def test_multi_instance(async_queue: AsyncAlchemicalQueues):
    q1 = async_queue.get("test")
    q2 = async_queue.get("test")
    assert q1 is q2


@pytest.mark.asyncio
async def test_queue_size_empty(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get("test")

    assert await q.empty()
    assert await q.qsize() == 0

    await q.put(1)

    assert not await q.empty()
    assert await q.qsize() == 1

    entry = await q.get()
    assert entry is not None

    assert await q.empty()
    assert await q.qsize() == 0


@pytest.mark.asyncio
async def test_type_errors(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get_task_queue("test")

    with pytest.raises(TypeError):
        await q.respond("a", "b")

    with pytest.raises(TypeError):
        await q.responses("a")


# --- Task queue: claim/release/discard/extend, mirroring test_claims.py ---


@pytest.mark.asyncio
async def test_get_claims_does_not_delete(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get_task_queue("test")
    await q.put(1)

    entry = await q.get()
    assert entry is not None
    assert entry.claim_token is not None

    assert await q.get() is None
    assert await q.qsize() == 1


@pytest.mark.asyncio
async def test_release_frees_a_claimed_entry_for_immediate_reclaim(
    async_queue: AsyncAlchemicalQueues,
):
    q = async_queue.get_task_queue("test")
    await q.put("important")

    entry = await q.get()
    assert entry is not None

    await q.release(entry.entry_id, entry.claim_token)
    assert await q.qsize() == 1

    reclaimed = await q.get()
    assert reclaimed is not None
    assert reclaimed.entry_id == entry.entry_id
    assert reclaimed.claim_token != entry.claim_token


@pytest.mark.asyncio
async def test_release_with_wrong_claim_token_raises(
    async_queue: AsyncAlchemicalQueues,
):
    q = async_queue.get_task_queue("test")
    await q.put(1)
    entry = await q.get()
    assert entry is not None

    with pytest.raises(ClaimExpired):
        await q.release(entry.entry_id, entry.claim_token + 1)

    assert await q.qsize() == 1


@pytest.mark.asyncio
async def test_discard_removes_a_claimed_entry_entirely(
    async_queue: AsyncAlchemicalQueues,
):
    q = async_queue.get_task_queue("test")
    await q.put(1)

    entry = await q.get()
    assert entry is not None

    await q.discard(entry.entry_id, entry.claim_token)

    assert await q.qsize() == 0
    assert await q.get() is None


@pytest.mark.asyncio
async def test_discard_on_unclaimed_entry_raises(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get_task_queue("test")
    with pytest.raises(ClaimExpired):
        await q.discard(12345, 999)


@pytest.mark.asyncio
async def test_extend_keeps_a_claim_alive(async_queue: AsyncAlchemicalQueues):
    from datetime import timedelta

    q = async_queue.get_task_queue("test", visibility_timeout=timedelta(seconds=5))
    await q.put(1)
    entry = await q.get()
    assert entry is not None

    await q.extend(entry.entry_id, entry.claim_token)
    assert await q.get() is None  # still claimed
    await q.discard(entry.entry_id, entry.claim_token)


@pytest.mark.asyncio
async def test_extend_on_an_expired_claim_raises(async_engine_factory):
    import asyncio
    from datetime import timedelta

    aq = AsyncAlchemicalQueues(engine=async_engine_factory())
    await aq.create_all()
    q = aq.get_task_queue("test", visibility_timeout=timedelta(milliseconds=30))

    await q.put(1)
    entry = await q.get()
    assert entry is not None

    await asyncio.sleep(0.05)
    reclaimed = await q.get()
    assert reclaimed is not None

    with pytest.raises(ClaimExpired):
        await q.extend(entry.entry_id, entry.claim_token)


@pytest.mark.asyncio
async def test_respond_does_not_touch_the_entry(async_queue: AsyncAlchemicalQueues):
    q = async_queue.get_task_queue("test")
    await q.put(1)

    entry = await q.get()
    assert entry is not None
    await q.respond(entry.entry_id, "answer")

    assert await q.qsize() == 1  # still claimed
    assert (await q.responses(entry.entry_id))[0].data == "answer"

    await q.discard(entry.entry_id, entry.claim_token)
    assert await q.qsize() == 0
