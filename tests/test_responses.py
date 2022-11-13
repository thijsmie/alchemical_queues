from datetime import datetime
import pytest
from alchemical_queues import AlchemicalQueue, AlchemicalQueues


def test_respond(queue: AlchemicalQueues):
    q = queue.get("test")
    q.put(1)
    job = q.get()

    assert job

    q.respond(job.entry_id, "test")

    responses = q.responses(job.entry_id)
    assert len(responses) == 1
    response = responses[0]
    assert response.entry_id == job.entry_id
    assert response.data == "test"
    assert response.delivered_at > job.enqueued_at
    assert response.cleanup_at is None

