import pytest
import logging
import sys

from pathlib import Path
from sqlalchemy import create_engine
from alchemical_queues import AlchemicalQueues


# Supporting modules
sys.path.insert(0, str(Path(__file__).parent / 'test_tasker'))


def pytest_addoption(parser):
    parser.addoption("-E", "--engine", action="store", type=str, help="Define the sqlite database engine URL. When not specified use a temporary SQLite file")


@pytest.fixture
def engine(pytestconfig, tmpdir):
    if pytestconfig.getoption('engine') is not None:
        return create_engine(pytestconfig.getoption('engine'))
    else:
        path = Path(str(tmpdir)).absolute() / 'test.db'
        return create_engine(f"sqlite:///{path}")


@pytest.fixture
def queue(engine):
    q = AlchemicalQueues(engine=engine)
    q.create()
    return q


@pytest.fixture
def engine_factory(pytestconfig, tmpdir):
    def factory():
        if pytestconfig.getoption('engine') is not None:
            return create_engine(pytestconfig.getoption('engine'))
        else:
            path = Path(str(tmpdir)).absolute() / 'test.db'
            return create_engine(f"sqlite:///{path}")
    return factory


@pytest.fixture
def queue_factory(engine_factory):
    def factory():
        q = AlchemicalQueues(engine=engine_factory())
        q.create()
        return q
    return factory
