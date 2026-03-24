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
