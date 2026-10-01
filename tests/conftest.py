import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from alchemical_queues import AlchemicalQueues

try:
    import pytest_asyncio
    from sqlalchemy.ext.asyncio import create_async_engine

    from alchemical_queues.aio import AsyncAlchemicalQueues

    _HAS_ASYNC_TEST_DEPS = True
except ImportError:
    _HAS_ASYNC_TEST_DEPS = False

# Supporting modules
sys.path.insert(0, str(Path(__file__).parent / "test_tasker"))


def pytest_addoption(parser):
    parser.addoption(
        "-E",
        "--engine",
        action="store",
        type=str,
        help="Define the sqlite database engine URL. When not specified use a temporary SQLite file",
    )
    parser.addoption(
        "--async-engine",
        action="store",
        type=str,
        help=(
            "Define the async database engine URL (e.g. postgresql+asyncpg://... "
            "or postgresql+psycopg://...). When not specified use a temporary "
            "SQLite file via aiosqlite."
        ),
    )


@pytest.fixture
def engine(pytestconfig, tmpdir):
    if pytestconfig.getoption("engine") is not None:
        return create_engine(pytestconfig.getoption("engine"))
    else:
        path = Path(str(tmpdir)).absolute() / "test.db"
        return create_engine(f"sqlite:///{path}")


@pytest.fixture
def queue(engine):
    q = AlchemicalQueues(engine=engine)
    q.create_all()
    return q


@pytest.fixture
def engine_factory(pytestconfig, tmpdir):
    def factory():
        if pytestconfig.getoption("engine") is not None:
            return create_engine(pytestconfig.getoption("engine"))
        else:
            path = Path(str(tmpdir)).absolute() / "test.db"
            return create_engine(f"sqlite:///{path}")

    return factory


@pytest.fixture
def queue_factory(engine_factory):
    def factory():
        q = AlchemicalQueues(engine=engine_factory())
        q.create_all()
        return q

    return factory


@pytest.fixture(autouse=True)
def run_around_tests(queue):
    yield
    queue.clear()


if _HAS_ASYNC_TEST_DEPS:

    @pytest.fixture
    def async_engine_factory(pytestconfig, tmp_path):
        def factory():
            if pytestconfig.getoption("async_engine") is not None:
                return create_async_engine(pytestconfig.getoption("async_engine"))
            else:
                path = tmp_path.absolute() / "test_async.db"
                return create_async_engine(f"sqlite+aiosqlite:///{path}")

        return factory

    @pytest_asyncio.fixture
    async def async_queue(async_engine_factory):
        q = AsyncAlchemicalQueues(engine=async_engine_factory())
        await q.create_all()
        yield q
        # Needed when --async-engine points at a shared database (e.g.
        # Postgres in CI): each test should start from an empty table, same
        # as the sync `run_around_tests` does for `--engine`.
        await q.clear()
