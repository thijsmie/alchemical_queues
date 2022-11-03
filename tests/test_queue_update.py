from datetime import datetime
from alchemical_queues import AlchemicalQueues


def test_put_get_data(queue: AlchemicalQueues):
    q = queue["test"]
    jid = q.put(1)

    j = q.get()
    assert j
    j.data = 2
    del j

    j = q.fetch(jid)
    assert j
    assert j.data == 2
