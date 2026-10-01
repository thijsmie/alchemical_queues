"""Smoke test for the async_plain_python example, mirroring the sync
examples' tests in test_examples.py."""

import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytest_asyncio = pytest.importorskip("pytest_asyncio")
pytest.importorskip("aiosqlite")

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


@pytest.mark.asyncio
async def test_async_plain_python_example(tmp_path):
    sys.path.insert(0, str(EXAMPLES_DIR / "async_plain_python"))
    try:
        from producer import build_queues
        from tasks import add_numbers

        from alchemical_queues.tasks.aio import AsyncWorker

        queues = build_queues(f"sqlite+aiosqlite:///{tmp_path / 'async_plain.db'}")
        await queues.create_all()
        task_queue = queues.get_task_queue("tasks")

        task = await add_numbers(2, 3).schedule(task_queue)
        await AsyncWorker(task_queue, poll_every=timedelta(milliseconds=10)).work_one()

        assert await task.done()
        assert await task.result() == 5
    finally:
        sys.path.remove(str(EXAMPLES_DIR / "async_plain_python"))
        sys.modules.pop("tasks", None)
        sys.modules.pop("producer", None)
