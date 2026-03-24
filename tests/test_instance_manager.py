import pytest
from instance_manager import Instance

@pytest.mark.asyncio
async def test_clear_queue():
    """Test that clear_queue successfully empties the queue and returns the correct count."""
    instance = Instance(id=1, title="Test Instance")

    # Check that it's empty initially
    assert instance.queue.empty()
    assert instance.clear_queue() == 0

    # Enqueue fake items using put_nowait instead of await
    instance.queue.put_nowait("item 1")
    instance.queue.put_nowait("item 2")
    instance.queue.put_nowait("item 3")

    # Queue should not be empty
    assert not instance.queue.empty()
    assert instance.queue.qsize() == 3

    # Clear the queue
    cleared_count = instance.clear_queue()

    # Assertions
    assert cleared_count == 3
    assert instance.queue.empty()
    assert instance.queue.qsize() == 0

    # Calling clear_queue again should return 0
    assert instance.clear_queue() == 0
