"""Smoke tests: each example app under examples/ actually works end to end.

These import the example modules directly (examples/ is added to sys.path
below) rather than running them as subprocesses, so a broken example fails
fast with a normal traceback instead of a silent timeout.
"""

import sys
import time
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def _poll_until_done(get_status, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    status = get_status()
    while status.get("status") == "pending":
        if time.monotonic() > deadline:
            raise AssertionError(f"task did not complete in time: {status}")
        time.sleep(0.05)
        status = get_status()
    return status


def test_plain_python_example(tmp_path):
    sys.path.insert(0, str(EXAMPLES_DIR / "plain_python"))
    try:
        from datetime import timedelta

        from producer import build_queues
        from tasks import add_numbers

        from alchemical_queues.tasks import Worker

        queues = build_queues(f"sqlite:///{tmp_path / 'plain.db'}")
        task_queue = queues.get_task_queue("tasks")

        task = add_numbers(2, 3).schedule(task_queue)
        Worker(task_queue, poll_every=timedelta(milliseconds=10)).work_one()

        assert task.done
        assert task.result == 5
    finally:
        sys.path.remove(str(EXAMPLES_DIR / "plain_python"))
        sys.modules.pop("tasks", None)
        sys.modules.pop("producer", None)


def test_flask_example(tmp_path):
    flask = pytest.importorskip("flask")  # noqa: F841
    sys.path.insert(0, str(EXAMPLES_DIR / "flask_app"))
    try:
        from app import create_app

        app = create_app(f"sqlite:///{tmp_path / 'flask.db'}")
        client = app.test_client()

        response = client.post("/add", json={"a": 2, "b": 3})
        task_id = response.get_json()["task_id"]

        def get_status():
            return client.get(f"/result/{task_id}").get_json()

        status = _poll_until_done(get_status)
        assert status == {"status": "done", "result": 5}
    finally:
        sys.path.remove(str(EXAMPLES_DIR / "flask_app"))
        sys.modules.pop("app", None)


def test_fastapi_example(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("aiosqlite")
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(EXAMPLES_DIR / "fastapi_app"))
    try:
        from app import create_app

        # fastapi_app is now built on AsyncAlchemicalQueues, so it needs an
        # async driver URL -- see examples/fastapi_app/app.py.
        app = create_app(f"sqlite+aiosqlite:///{tmp_path / 'fastapi.db'}")
        with TestClient(app) as client:
            response = client.post("/add", json={"a": 2, "b": 3})
            task_id = response.json()["task_id"]

            def get_status():
                return client.get(f"/result/{task_id}").json()

            status = _poll_until_done(get_status)
            assert status == {"status": "done", "sum": 5}
    finally:
        sys.path.remove(str(EXAMPLES_DIR / "fastapi_app"))
        sys.modules.pop("app", None)


def test_starlette_example(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    sys.path.insert(0, str(EXAMPLES_DIR / "starlette_app"))
    try:
        from app import create_app

        app = create_app(f"sqlite:///{tmp_path / 'starlette.db'}")
        with TestClient(app) as client:
            response = client.post("/add", json={"a": 2, "b": 3})
            task_id = response.json()["task_id"]

            def get_status():
                return client.get(f"/result/{task_id}").json()

            status = _poll_until_done(get_status)
            assert status == {"status": "done", "result": 5}
    finally:
        sys.path.remove(str(EXAMPLES_DIR / "starlette_app"))
        sys.modules.pop("app", None)


def test_litestar_example(tmp_path):
    pytest.importorskip("litestar")
    from litestar.testing import TestClient

    sys.path.insert(0, str(EXAMPLES_DIR / "litestar_app"))
    try:
        from app import create_app

        app = create_app(f"sqlite:///{tmp_path / 'litestar.db'}")
        with TestClient(app) as client:
            response = client.post("/add", json={"a": 2, "b": 3})
            task_id = response.json()["task_id"]

            def get_status():
                return client.get(f"/result/{task_id}").json()

            status = _poll_until_done(get_status)
            assert status == {"status": "done", "result": 5}
    finally:
        sys.path.remove(str(EXAMPLES_DIR / "litestar_app"))
        sys.modules.pop("app", None)
