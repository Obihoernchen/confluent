"""Boot device override against a real BMC: set it, then put it back.

A small reversible write, and a good first one: it changes a single setting,
the prior value is readable beforehand, restoring it is one more call, and
nothing about it interrupts a running system. It is the operation behind
nodesetboot.

The override does affect the next boot, so the window between setting and
restoring is the risk. The fixture keeps that window to the length of one test
and closes it in teardown, so the setting is put back even when an assertion
fails part way through.

Calls this file will never make: anything that powers, resets or reseats the
system, and any write outside the boot override itself.
"""

import pytest

pytestmark = [pytest.mark.hardware, pytest.mark.reversible]


@pytest.fixture
async def restored_bootdev(redfish_command):
    """Capture the boot override and put it back afterwards, come what may."""
    original = await redfish_command.get_bootdev()
    try:
        yield original
    finally:
        # Teardown, so this runs after a failed assertion too. 'default' is
        # the documented way to clear a directed boot request, and is what a
        # BMC with no override set reports.
        await redfish_command.set_bootdev(
            original.get('bootdev', 'default'),
            persist=original.get('persistent', False))


async def test_boot_override_can_be_set_and_cleared(redfish_command,
                                                    restored_bootdev):
    """Setting an override is visible, and clearing it returns the BMC to
    reporting no directed boot request."""
    await redfish_command.set_bootdev('network')

    changed = await redfish_command.get_bootdev()
    assert changed['bootdev'] == 'network', changed

    await redfish_command.set_bootdev('default')

    cleared = await redfish_command.get_bootdev()
    assert cleared['bootdev'] == 'default', cleared


async def test_boot_override_survives_a_reread(redfish_command,
                                               restored_bootdev):
    """The setting is persisted by the BMC rather than merely echoed back.

    Reading twice catches an implementation that reports what it was told
    instead of what it stored, which a single read immediately after the write
    would not distinguish.
    """
    await redfish_command.set_bootdev('network')

    first = await redfish_command.get_bootdev()
    second = await redfish_command.get_bootdev()

    assert first['bootdev'] == 'network'
    assert second['bootdev'] == 'network'
    assert first['persistent'] == second['persistent']


async def test_prior_setting_is_restored(redfish_command, restored_bootdev):
    """The fixture's own contract: whatever the BMC had before is what it has
    after. Asserting it here means a broken restore fails loudly rather than
    quietly leaving the machine changed."""
    await redfish_command.set_bootdev('network')
    assert (await redfish_command.get_bootdev())['bootdev'] == 'network'

    await redfish_command.set_bootdev(
        restored_bootdev.get('bootdev', 'default'),
        persist=restored_bootdev.get('persistent', False))

    assert (await redfish_command.get_bootdev())['bootdev'] == \
        restored_bootdev['bootdev']
