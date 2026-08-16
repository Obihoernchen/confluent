"""System identity and state: the smallest useful checks against a real BMC.

The first tests in the hardware tier. They exist to catch the class of
regression that no amount of mocking finds: real firmware answering
differently from the fixture someone wrote from the specification.

Run them by naming a device in an inventory, or against a replayed capture,
which needs no hardware at all:

    CONFLUENT_TEST_HARDWARE=tests/support/inventory-dmtf.yaml \\
    python3 -m pytest -m hardware --run-hardware

An address accepts a :port for a BMC reached through a tunnel. Without
--run-hardware the tests are skipped whatever the inventory holds, so a
default run never touches hardware.

Everything here is strictly read-only. Nothing in this file may power a node,
set a boot device, change a setting or write firmware. A hardware test that
writes belongs in a separate file and must, per tests/README.md, capture the
prior state, prove it can restore it, and keep an explicit list of calls it
will never make.

Nothing here is vendor-specific. The assertions are deliberately about shape
and plausibility rather than specific values, so they hold across BMC families
and whatever power state the machine happens to be in. Verified against a
Lenovo XCC (ThinkSystem SD665-N V3), an AMI MegaRAC (MEGWARE EUREKA-LC-CN) and
an OpenBMC (Celestica Artemis U.2), which between them select the lenovo, ami
and generic OEM handlers.

Vendor-specific expectations belong in their own files, keyed off the detected
vendor rather than an assumption about what the address points at.
"""

import pytest

# Every hardware test must declare what it may do to the device, and the run
# refuses to collect if one does not. Nothing in this file sends a write.
pytestmark = [pytest.mark.hardware, pytest.mark.readonly]


async def test_power_state_is_reported(redfish_command):
    """The cheapest real round trip: connect, then read power state.

    Reaching this point already proves the service root parsed, the session
    opened, an OEM handler was selected and the default system URL resolved.
    """
    power = await redfish_command.get_power()

    assert 'powerstate' in power
    assert power['powerstate'] in ('on', 'off'), power


async def test_system_identity_is_reported(redfish_command):
    """Model and serial come from the system resource the OEM handler picked.

    A missing property here usually means the wrong system URL was chosen
    rather than a BMC problem, which is the kind of regression only real
    firmware surfaces.

    Model must carry something; a system that cannot name itself is broken.
    Note it may arrive padded, so compare stripped: the Celestica board
    reports 'Artemis U.2' followed by spaces.

    SerialNumber is only required to be present and a string. Plenty of ODM
    and whitebox boards ship without one, and the Celestica returns nothing
    but spaces. Asserting it is non-empty would fail those systems for a
    reason that is not a defect in confluent.
    """
    sysinfo = await redfish_command.sysinfo()

    assert isinstance(sysinfo.get('Model'), str), sysinfo.get('Model')
    assert sysinfo['Model'].strip(), repr(sysinfo['Model'])

    assert isinstance(sysinfo.get('SerialNumber'), str), sysinfo.get('SerialNumber')


async def test_boot_device_is_reported(redfish_command):
    """get_bootdev is read-only. Setting a boot device is not, and must not be
    added to this file."""
    bootdev = await redfish_command.get_bootdev()

    assert 'bootdev' in bootdev
    assert isinstance(bootdev.get('persistent'), bool), bootdev
