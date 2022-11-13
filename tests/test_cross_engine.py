from datetime import datetime
from typing import Callable
from alchemical_queues import AlchemicalQueues


def test_put_get_data(queue_factory: Callable[[], AlchemicalQueues]) -> None:
    queue1: AlchemicalQueues = queue_factory()
    queue2: AlchemicalQueues = queue_factory()

    assert queue1 is not queue2

    q1 = queue1.get("test")
    q2 = queue2.get("test")

    assert q1 is not q2

    q1.put(1)
    entry = q2.get()
    assert entry and entry.data == 1

