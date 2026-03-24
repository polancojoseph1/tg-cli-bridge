from instance_manager import InstanceManager

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
