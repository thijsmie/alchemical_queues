"""Tests for the alchemical_worker CLI's --import mode."""

import pytest
from alchemical_queues import AlchemicalQueues
from alchemical_queues.tasks.cli import cli, _import_queues


def test_cli_rejects_both_engine_and_import(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "alchemical_worker",
            "sqlite:///:memory:",
            "testqueue",
            "--import",
            "tests.test_cli:some_queues",
        ],
    )
    with pytest.raises(SystemExit):
        cli()


def test_cli_rejects_neither_engine_nor_import(monkeypatch):
    monkeypatch.setattr("sys.argv", ["alchemical_worker", "testqueue"])
    with pytest.raises(SystemExit):
        cli()


def test_import_path_requires_colon():
    with pytest.raises(SystemExit):
        _import_queues("tests.test_cli_without_colon")


def test_import_path_rejects_unknown_module():
    with pytest.raises(SystemExit):
        _import_queues("no.such.module:queues")


def test_import_path_rejects_unknown_attribute():
    with pytest.raises(SystemExit):
        _import_queues("tests.test_cli:no_such_attribute")


def test_import_path_rejects_non_alchemicalqueues_attribute():
    with pytest.raises(SystemExit):
        _import_queues("tests.test_cli:not_a_queues_instance")


def test_import_path_returns_the_instance(engine):
    global some_queues  # pylint: disable=global-statement
    some_queues = AlchemicalQueues(engine=engine)

    result = _import_queues("tests.test_cli:some_queues")
    assert result is some_queues


# Used by test_import_path_returns_the_instance above, and as a negative case.
not_a_queues_instance = object()
