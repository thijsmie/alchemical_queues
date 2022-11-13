"""The command line interface `alchemical_worker`. """
import argparse
from datetime import timedelta
from sqlalchemy.engine import create_engine
from alchemical_queues import AlchemicalQueues
from alchemical_queues.tasks import Worker


parser = argparse.ArgumentParser()
parser.add_argument("engine", type=str, help="The name of the queue to work on.")
parser.add_argument("queue_name", type=str, help="The name of the queue to work on.")
parser.add_argument(
    "-p",
    "--poll-every",
    type=float,
    help="How often to poll for new tasks.",
    default=1.0,
)


def cli():
    """The command line tool `alchemical_worker` runs this function."""
    namespace = parser.parse_args()

    queues = AlchemicalQueues(create_engine(namespace.engine))
    queues.create_all()
    queue = queues.get(namespace.queue_name)
    Worker(queue, timedelta(seconds=namespace.poll_every)).work()
