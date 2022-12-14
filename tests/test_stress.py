"""Stresstest with multi-producer multi-consumer and check if no items get missed or duplicated"""

import pytest
from typing import Callable, List
from datetime import datetime, timedelta
from threading import Thread
from queue import Queue
from alchemical_queues import AlchemicalQueues


def producer(index: int, queue_factory: Callable[[], AlchemicalQueues], num_samples: int):
    queue: AlchemicalQueues = queue_factory()
    q = queue.get("test")

    for i in range(num_samples):
        q.put((index, i))


def consumer(queue: Queue, queue_factory: Callable[[], AlchemicalQueues]):
    aq: AlchemicalQueues = queue_factory()
    q = aq.get("test")
    t = datetime.now() + timedelta(seconds=2)
    out = []
    while datetime.now() < t:
        v = q.get()
        if v:
            out.append(v)
            t = datetime.now() + timedelta(seconds=2)

    queue.put(set(out))


numbers = [
    (1, 1, 1000),
    (2, 2, 100),
    (2, 10, 100),
    (10, 2, 50)
]


@pytest.mark.parametrize("num_producers,num_consumers,num_samples", numbers)
def test_stress(queue_factory: Callable[[], AlchemicalQueues], num_producers: int, num_consumers: int, num_samples: int):
    # Prime the queue
    queue: AlchemicalQueues = queue_factory()
    q = queue.get("test")
    q.put(0)

    producers = [Thread(target=producer, args=(i, queue_factory, num_samples)) for i in range(num_producers)]
    cqueues: List[Queue] = [Queue() for i in range(num_consumers)]
    consumers = [Thread(target=consumer, args=(cqueues[i], queue_factory)) for i in range(num_consumers)]

    for c in consumers:
        c.start()

    for p in producers:
        p.start()

    for p in producers:
        p.join()

    for c in consumers:
        c.join()

    data = [g.get() for g in cqueues]
    v = set().union(*data)

    assert len(v) == num_samples * num_producers + 1

    for i in range(num_consumers):
        for j in range(i+1, num_consumers):
            assert not data[i].intersection(data[j])
