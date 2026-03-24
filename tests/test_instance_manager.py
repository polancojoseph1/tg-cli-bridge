import pytest
from instance_manager import InstanceManager, Instance

@pytest.fixture
def manager():
    return InstanceManager()

class TestInstanceManagerCreate:
    def test_create_default_args(self, manager):
        """Test creating an instance with default owner_id and switch_active."""
        # Initial state after manager creation: there is already a default instance with id=1
        assert manager.count == 1
        assert manager.active_id == 1

        # Create new instance
        inst = manager.create(title="My Test Instance")

        assert isinstance(inst, Instance)
        assert inst.title == "My Test Instance"
        assert inst.id == 2
        assert manager.count == 2

        # Verify ownership
        assert manager._instance_owner[inst.id] == 0

        # Verify active instance switched to the newly created instance
        assert manager.active_id == 2
        assert manager.active == inst

    def test_create_switch_active_false(self, manager):
        """Test creating an instance with switch_active=False."""
        assert manager.active_id == 1

        inst = manager.create(title="Background Task", switch_active=False)

        assert inst.title == "Background Task"
        assert inst.id == 2
        assert manager.count == 2

        # Verify active instance did NOT switch
        assert manager.active_id == 1
        assert manager.active != inst

    def test_create_different_owner(self, manager):
        """Test creating an instance for a different owner."""
        # Check global active ID before creation
        assert manager.active_id == 1

        inst = manager.create(title="User's Instance", owner_id=99)

        assert inst.title == "User's Instance"
        assert inst.id == 2

        # Verify ownership
        assert manager._instance_owner[inst.id] == 99

        # Verify that the global active instance did NOT change
        assert manager.active_id == 1

        # Verify that the new owner's active instance is the newly created instance
        assert manager.get_active_for(owner_id=99) == inst
        assert manager._user_active[99] == inst.id

    def test_consecutive_create_increments_id(self, manager):
        """Test that consecutive calls to create increment the _next_id correctly."""
        inst1 = manager.create(title="Inst 1")
        assert inst1.id == 2

        inst2 = manager.create(title="Inst 2")
        assert inst2.id == 3

        inst3 = manager.create(title="Inst 3")
        assert inst3.id == 4

        assert manager.count == 4
