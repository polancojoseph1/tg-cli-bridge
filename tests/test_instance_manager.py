import pytest
import asyncio
from instance_manager import Instance, MAX_INSTANCE_QUEUE

def test_clear_queue_empty():
    """Test clearing an already empty queue."""
    instance = Instance(id=1, title="Test Instance")

    assert instance.queue.empty()
    cleared = instance.clear_queue()

    assert cleared == 0
    assert instance.queue.empty()

def test_clear_queue_populated():
    """Test clearing a populated queue."""
    instance = Instance(id=2, title="Test Instance 2")

    # Populate the queue
    for i in range(5):
        instance.queue.put_nowait(f"Message {i}")

    assert not instance.queue.empty()
    assert instance.queue.qsize() == 5

    # Clear the queue
    cleared = instance.clear_queue()

    assert cleared == 5
    assert instance.queue.empty()
    assert instance.queue.qsize() == 0

    # Clearing again should return 0
    assert instance.clear_queue() == 0

def test_clear_queue_full():
    """Test clearing a queue that is at max capacity."""
    instance = Instance(id=3, title="Test Instance 3")

    for i in range(MAX_INSTANCE_QUEUE):
        instance.queue.put_nowait(f"Message {i}")

    assert instance.queue.qsize() == MAX_INSTANCE_QUEUE

    # Queue should be successfully cleared
    cleared = instance.clear_queue()

    assert cleared == MAX_INSTANCE_QUEUE
    assert instance.queue.empty()
