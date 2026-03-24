import pytest
from unittest.mock import MagicMock
from instance_manager import InstanceManager, Instance


class TestEnsurePinned:
    def test_creates_new_instance(self):
        manager = InstanceManager()
        user_id = 123
        title = "Test Pinned"

        # Capture initial state
        initial_active_id = manager.active_id
        initial_next_id = manager._next_id

        # Call ensure_pinned
        inst = manager.ensure_pinned(user_id, title)

        # Assertions
        assert isinstance(inst, Instance)
        assert inst.title == title

        # Verify it updated dictionaries correctly
        assert manager._instances[inst.id] == inst
        assert manager._instance_owner[inst.id] == user_id
        assert manager._user_instance_map[user_id] == inst.id
        assert manager._user_active[user_id] == inst.id

        # Verify next_id incremented
        assert manager._next_id == initial_next_id + 1

        # Verify global active instance is unchanged
        assert manager.active_id == initial_active_id

    def test_returns_existing_instance(self):
        manager = InstanceManager()
        user_id = 123
        title = "Test Pinned"

        # First call creates the instance
        first_inst = manager.ensure_pinned(user_id, title)

        # Capture state after first creation
        next_id_after_first = manager._next_id

        # Second call should return the exact same instance
        second_inst = manager.ensure_pinned(user_id, title)

        # Assertions
        assert second_inst is first_inst
        assert second_inst.id == first_inst.id

        # Verify no new instance was created
        assert manager._next_id == next_id_after_first
        assert len(manager._instances) == 2  # Default + 1 pinned


def test_switch_by_display_number_success():
    manager = InstanceManager()
    second_instance = manager.create("second")

    # "1" is the default instance created on startup
    # "2" should be the "second" instance we just created
    switched_instance = manager.switch("2")

    assert switched_instance is not None
    assert switched_instance.id == second_instance.id
    assert switched_instance.title == "second"

    # Verify the active instance was actually updated
    active_instance = manager.get_active_for(0)
    assert active_instance.id == second_instance.id


def test_switch_by_display_number_out_of_bounds():
    manager = InstanceManager()

    # Switch to a non-existent display number
    switched_instance = manager.switch("99")

    assert switched_instance is None

    # Active instance should remain unchanged
    active_instance = manager.get_active_for(0)
    assert active_instance.title == "Default"


def test_switch_by_title_exact_match():
    manager = InstanceManager()
    second_instance = manager.create("Second")

    # Switch back to the Default instance first
    manager.switch("1")

    # Now switch to "Second" by title
    switched_instance = manager.switch("Second")

    assert switched_instance is not None
    assert switched_instance.id == second_instance.id

    active_instance = manager.get_active_for(0)
    assert active_instance.title == "Second"
    assert active_instance.id == second_instance.id


def test_switch_by_title_partial_match():
    manager = InstanceManager()

    # The first instance created on startup is "Default"
    default_instance = manager.get_active_for(0)

    # Create a second one and make it active
    manager.create("Another")

    # Switch back to the "Default" instance using partial, case-insensitive match
    switched_instance = manager.switch("fault")

    assert switched_instance is not None
    assert switched_instance.id == default_instance.id
    assert switched_instance.title == "Default"

    active_instance = manager.get_active_for(0)
    assert active_instance.id == default_instance.id


def test_switch_by_title_no_match():
    manager = InstanceManager()
    manager.create("Another")

    # Try to switch to a non-existent title
    switched_instance = manager.switch("Nonexistent")

    assert switched_instance is None

    # Active instance should remain unchanged ("Another")
    active_instance = manager.get_active_for(0)
    assert active_instance.title == "Another"


def test_switch_owner_isolation():
    manager = InstanceManager()

    # Global instance is active
    global_instance = manager.get_active_for(0)

    # Create an instance for user 123
    owner_id = 123
    user_instance = manager.create("user_inst", owner_id=owner_id)

    # Create another instance for user 123 so we have something to switch from/to
    manager.create("user_inst_2", owner_id=owner_id)

    # User 123 switches back to their first instance ("user_inst", display number 1)
    switched_instance = manager.switch("1", owner_id=owner_id)

    assert switched_instance is not None
    assert switched_instance.id == user_instance.id

    # Verify owner's active instance updated
    active_user_instance = manager.get_active_for(owner_id)
    assert active_user_instance.id == user_instance.id

    # Verify global active instance remained unchanged
    active_global_instance = manager.get_active_for(0)
    assert active_global_instance.id == global_instance.id


# --- clear_queue tests ---

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


# --- remove tests ---

@pytest.fixture
def manager():
    return InstanceManager()

def test_remove_nonexistent_instance(manager):
    assert manager.remove(999) is None

def test_remove_wrong_owner(manager):
    inst = manager.create("Other", owner_id=1)
    # Try to remove it using owner_id=0
    assert manager.remove(inst.id, owner_id=0) is None
    # Still exists
    assert manager.get(inst.id) is not None

def test_remove_last_instance(manager):
    # manager starts with 1 instance for owner_id=0
    assert len(manager.list_all(for_owner_id=0)) == 1
    assert manager.remove(1, owner_id=0) is None
    # Still exists
    assert manager.get(1) is not None

def test_remove_non_active_instance(manager):
    manager.create("Second", owner_id=0)  # Now active is 2
    assert manager.count == 2
    assert manager.active_id == 2

    # Remove the non-active one (id=1)
    removed = manager.remove(1, owner_id=0)
    assert removed is not None
    assert removed.id == 1
    assert manager.count == 1
    assert manager.active_id == 2  # active id shouldn't change

def test_remove_active_global_instance(manager):
    manager.get(1)
    manager.create("Second", owner_id=0)  # active becomes 2

    # Remove active
    removed = manager.remove(2, owner_id=0)
    assert removed is not None
    assert removed.id == 2
    assert manager.count == 1
    # active should be set to remaining inst1
    assert manager.active_id == 1

def test_remove_active_user_instance(manager):
    inst1 = manager.create("User1", owner_id=1)
    inst2 = manager.create("User2", owner_id=1)  # active for user 1 is now inst2.id

    assert manager._user_active[1] == inst2.id

    removed = manager.remove(inst2.id, owner_id=1)
    assert removed is not None
    assert removed.id == inst2.id

    # user 1 active should be set to inst1.id
    assert manager._user_active[1] == inst1.id

def test_remove_last_user_instance_not_allowed(manager):
    inst = manager.create("User", owner_id=1)
    assert manager.remove(inst.id, owner_id=1) is None
    assert manager.get(inst.id) is not None

def test_remove_cancels_tasks(manager):
    inst = manager.create("Second", owner_id=0)

    worker_task = MagicMock()
    worker_task.done.return_value = False
    inst.worker_task = worker_task

    current_task = MagicMock()
    current_task.done.return_value = False
    inst.current_task = current_task

    removed = manager.remove(inst.id, owner_id=0)
    assert removed is not None

    worker_task.cancel.assert_called_once()
    current_task.cancel.assert_called_once()

def test_remove_does_not_cancel_completed_tasks(manager):
    inst = manager.create("Second", owner_id=0)

    worker_task = MagicMock()
    worker_task.done.return_value = True
    inst.worker_task = worker_task

    current_task = MagicMock()
    current_task.done.return_value = True
    inst.current_task = current_task

    removed = manager.remove(inst.id, owner_id=0)
    assert removed is not None

    worker_task.cancel.assert_not_called()
    current_task.cancel.assert_not_called()
