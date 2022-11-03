from datetime import datetime
from alchemical_queues import AlchemicalQueues


def test_put_get_data(queue_factory):
    queue1: AlchemicalQueues = queue_factory()
    queue2: AlchemicalQueues = queue_factory()

    assert queue1 is not queue2

    q1 = queue1["test"]
    q2 = queue2["test"]

    assert q1 is not q2

    q1.put(1)
    assert q2.get().data == 1

