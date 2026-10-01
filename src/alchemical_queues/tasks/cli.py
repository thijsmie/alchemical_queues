"""The command line interface `alchemical_worker`."""

import sys
import argparse
import importlib
from datetime import timedelta
from sqlalchemy.engine import create_engine
from alchemical_queues import AlchemicalQueues
from alchemical_queues.tasks import Worker

parser = argparse.ArgumentParser()
parser.add_argument(
    "engine",
    type=str,
    nargs="?",
    default=None,
    help=(
        "SQLAlchemy engine URL to connect to. This constructs a plain "
        "AlchemicalQueues() with default table names and no shared declarative "
        "base. If your app customizes queue_tablename/response_tablename or "
        "passes base=, use --import instead. Mutually exclusive with --import."
    ),
)
parser.add_argument("queue_name", type=str, help="The name of the queue to work on.")
parser.add_argument(
    "-p",
    "--poll-every",
    type=float,
    help="How often to poll for new tasks.",
    default=1.0,
)
parser.add_argument(
    "-i",
    "--import",
    dest="import_path",
    type=str,
    default=None,
    metavar="MODULE:ATTRIBUTE",
    help=(
        "Import an existing AlchemicalQueues instance instead of constructing "
        "a default one, as 'module.submodule:attribute' (e.g. 'myapp.queues:queues', "
        "similar to how uvicorn takes 'myapp.main:app'). The current directory is "
        "added to the import path, like `python -m`. Mutually exclusive with "
        "the positional ENGINE argument."
    ),
)


def _import_queues(import_path: str) -> AlchemicalQueues:
    module_path, sep, attr = import_path.partition(":")

    if not sep or not attr:
        raise SystemExit(
            f"--import value {import_path!r} must be of the form 'module:attribute'"
        )

    sys.path.insert(0, "")

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise SystemExit(f"Could not import module {module_path!r}: {exc}") from exc

    try:
        queues = getattr(module, attr)
    except AttributeError as exc:
        raise SystemExit(f"Module {module_path!r} has no attribute {attr!r}") from exc

    if not isinstance(queues, AlchemicalQueues):
        raise SystemExit(
            f"{import_path!r} is a {type(queues).__name__}, not an AlchemicalQueues"
        )

    return queues


def _resolve_queues(namespace: argparse.Namespace) -> AlchemicalQueues:
    if namespace.import_path and namespace.engine:
        parser.error("Pass either ENGINE or --import, not both.")

    if namespace.import_path:
        return _import_queues(namespace.import_path)

    if namespace.engine:
        return AlchemicalQueues(create_engine(namespace.engine))

    parser.error("Pass either ENGINE or --import.")
    raise AssertionError("unreachable")  # parser.error() always raises SystemExit


def cli():
    """The command line tool `alchemical_worker` runs this function."""
    namespace = parser.parse_args()
    queues = _resolve_queues(namespace)

    queues.create_all()
    queue = queues.get_task_queue(namespace.queue_name)
    Worker(queue, timedelta(seconds=namespace.poll_every)).work()
