"""System identity and state over IPMI: the counterpart of the Redfish file.

The same three checks, asked over the other transport. Keeping them as their
own file rather than folding them into a shared one is deliberate: the reads
they make are named differently and mean different things. Redfish answers
identity from a system resource an OEM handler selected, IPMI answers it from a
FRU inventory area, and a test that hid that behind a common name would be
testing the fixture rather than the BMC.

Run them by naming a device in an inventory, or against the simulator, which
needs no hardware at all:

    CONFLUENT_TEST_HARDWARE=tests/support/inventory-ipmisim.yaml \\
    python3 -m pytest -m hardware

Everything here is strictly read-only. A write over IPMI belongs in
test_ipmi_bootdev.py, under the reversible marker and with a fixture that puts
the prior state back.
"""

import pytest

# loop_scope='session' because an IPMI session only answers on the loop that
# opened it. See the ipmi_command fixture; without this the tests hang.
pytestmark = [pytest.mark.hardware, pytest.mark.readonly,
              pytest.mark.asyncio(loop_scope='session')]


async def test_power_state_is_reported(ipmi_command):
    """The cheapest real round trip: negotiate a session, then read power.

    Reaching this point already proves RMCP+ came up, which is where an IPMI
    conversation most often fails: the cipher suite, the RAKP exchange and the
    privilege level are all settled before a single command is sent.
    """
    power = await ipmi_command.get_power()

    assert 'powerstate' in power
    assert power['powerstate'] in ('on', 'off'), power


async def test_system_identity_is_reported(ipmi_command):
    """Identity comes from the FRU inventory area, not from a system resource.

    A BMC that answers the FRU device list but not the read behind it leaves
    confluent with a component it can name and nothing to say about it, which
    is worth telling apart from a BMC with no FRU at all.

    Only presence and type are asserted. Whitebox and ODM boards ship with FRU
    fields blank or padded, and requiring a value here would fail a machine for
    something that is not a confluent defect. That matches the Redfish file,
    which makes the same allowance for SerialNumber.
    """
    inventory = await ipmi_command.get_inventory_of_component('System')

    assert inventory is not None, 'the system FRU read returned nothing'
    assert isinstance(inventory, dict), inventory

    for field in ('Manufacturer', 'Product name', 'Serial Number'):
        if field in inventory and inventory[field] is not None:
            assert isinstance(inventory[field], str), inventory


async def test_boot_device_is_reported(ipmi_command):
    """get_bootdev is read-only. Setting a boot device is not, and must not be
    added to this file."""
    bootdev = await ipmi_command.get_bootdev()

    assert 'bootdev' in bootdev
    assert isinstance(bootdev.get('persistent'), bool), bootdev
