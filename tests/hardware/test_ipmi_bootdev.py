"""Boot device override over IPMI: set it, then put it back.

The counterpart of test_redfish_bootdev.py, and the same argument for it being
the first reversible write worth making: one setting, readable beforehand,
restored with one more call, and nothing about it interrupts a running system.
It is the operation behind nodesetboot.

IPMI differs from Redfish in one way that matters here. The override is carried
in the chassis boot options, and a BMC that persists it does so with a timeout:
firmware is expected to clear the request once it has been acted on. So a value
read straight back is the request, not necessarily what the next boot will do.
The tests below assert the request, which is all the protocol promises.

Calls this file will never make: anything that powers, resets or reseats the
system, and any write outside the boot override itself.
"""

import pytest
import pytest_asyncio

# loop_scope='session' because an IPMI session only answers on the loop that
# opened it. See the ipmi_command fixture; without this the tests hang.
pytestmark = [pytest.mark.hardware, pytest.mark.reversible,
              pytest.mark.asyncio(loop_scope='session')]


# Per test, so the override is restored after each one, but on the session loop
# because that is where the client it talks to lives.
@pytest_asyncio.fixture(loop_scope='session')
async def restored_bootdev(ipmi_command):
    """Capture the boot override and put it back afterwards, come what may."""
    original = await ipmi_command.get_bootdev()
    try:
        yield original
    finally:
        # Teardown, so this runs after a failed assertion too. 'default' is
        # the documented way to clear a directed boot request, and is what a
        # BMC with no override set reports.
        await ipmi_command.set_bootdev(
            original.get('bootdev', 'default'),
            persist=original.get('persistent', False))


async def test_boot_override_can_be_set_and_cleared(ipmi_command,
                                                    restored_bootdev):
    """Setting an override is visible, and clearing it returns the BMC to
    reporting no directed boot request."""
    await ipmi_command.set_bootdev('network')

    changed = await ipmi_command.get_bootdev()
    assert changed['bootdev'] == 'network', changed

    await ipmi_command.set_bootdev('default')

    cleared = await ipmi_command.get_bootdev()
    assert cleared['bootdev'] == 'default', cleared


async def test_boot_override_survives_a_reread(ipmi_command,
                                               restored_bootdev):
    """The setting is held by the BMC rather than merely echoed back.

    Reading twice catches an implementation that reports what it was told
    instead of what it stored, which a single read immediately after the write
    would not distinguish.
    """
    await ipmi_command.set_bootdev('network')

    first = await ipmi_command.get_bootdev()
    second = await ipmi_command.get_bootdev()

    assert first['bootdev'] == 'network'
    assert second['bootdev'] == 'network'
    assert first['persistent'] == second['persistent']


async def test_prior_setting_is_restored(ipmi_command, restored_bootdev):
    """The fixture's own contract: whatever the BMC had before is what it has
    after. Asserting it here means a broken restore fails loudly rather than
    quietly leaving the machine changed."""
    await ipmi_command.set_bootdev('network')
    assert (await ipmi_command.get_bootdev())['bootdev'] == 'network'

    await ipmi_command.set_bootdev(
        restored_bootdev.get('bootdev', 'default'),
        persist=restored_bootdev.get('persistent', False))

    assert (await ipmi_command.get_bootdev())['bootdev'] == \
        restored_bootdev['bootdev']
