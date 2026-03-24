from instance_manager import InstanceManager, Instance

def test_ensure_pinned_creates_new():
    manager = InstanceManager()

    # Pre-condition check
    user_id = 123
    assert user_id not in manager._user_instance_map

    title = "Test Instance"

    # Save the original active ID to verify it doesn't change
    original_active_id = manager._active_id

    # Call ensure_pinned
    inst = manager.ensure_pinned(user_id, title)

    # Assertions
    assert isinstance(inst, Instance)
    assert inst.title == title
    assert inst.id in manager._instances
    assert manager._instances[inst.id] == inst

    # Verify owner
    assert manager._instance_owner[inst.id] == user_id

    # Verify maps
    assert manager._user_instance_map[user_id] == inst.id
    assert manager._user_active[user_id] == inst.id

    # Verify global active id didn't change
    assert manager._active_id == original_active_id


def test_ensure_pinned_returns_existing():
    manager = InstanceManager()
    user_id = 456
    title = "My Pinned Instance"

    # Create the first time
    inst1 = manager.ensure_pinned(user_id, title)

    # Call again with same user_id
    inst2 = manager.ensure_pinned(user_id, "Different Title Should Be Ignored")

    # Should return the exact same instance
    assert inst1 is inst2
    assert inst2.title == title  # Title should not be updated


def test_ensure_pinned_multiple_users():
    manager = InstanceManager()

    inst1 = manager.ensure_pinned(111, "User 111")
    inst2 = manager.ensure_pinned(222, "User 222")

    assert inst1.id != inst2.id
    assert manager._instance_owner[inst1.id] == 111
    assert manager._instance_owner[inst2.id] == 222
    assert manager._user_instance_map[111] == inst1.id
    assert manager._user_instance_map[222] == inst2.id
