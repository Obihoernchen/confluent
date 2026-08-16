"""Firmware inventory invariants that a plain read does not check.

The sweep in test_bmc_reads.py establishes that firmware inventory can be
read at all. These are the properties that were actually wrong in practice and
that only real firmware exposes.
"""

import pytest

import aiohmi.exceptions as exc

pytestmark = [pytest.mark.hardware, pytest.mark.readonly]

CATEGORIES = ('core', 'adapters', 'disks', 'misc')


async def _firmware(command, category=None):
    if category is None:
        return [entry async for entry in command.get_firmware()]
    return [entry async for entry in command.get_firmware(category=category)]


@pytest.fixture
async def firmware_inventory(redfish_command):
    """Everything the device reports, or skip if it reports nothing."""
    try:
        everything = await _firmware(redfish_command)
    except (exc.UnsupportedFunctionality, exc.RedfishError) as caught:
        pytest.skip('firmware inventory unavailable: {0}'.format(caught))
    if not everything:
        pytest.skip('device reports no firmware inventory')
    return everything


@pytest.mark.parametrize('category', CATEGORIES)
async def test_category_filters_rather_than_returning_everything(
        redfish_command, firmware_inventory, category):
    """A category must be a subset of the whole inventory.

    A filter that quietly ignores its argument and answers with everything
    looks like it works until someone counts.
    """
    everything = {entry[0] for entry in firmware_inventory}
    try:
        selected = {entry[0] for entry in
                    await _firmware(redfish_command, category)}
    except (exc.UnsupportedFunctionality, exc.RedfishError) as caught:
        pytest.skip('{0} category unsupported: {1}'.format(category, caught))

    extra = sorted(selected - everything)
    assert extra == [], (
        '{0} reported entries that the full inventory did not: {1}'.format(
            category, extra))


async def test_versions_do_not_carry_a_status_bit_as_a_digit(
        firmware_inventory):
    """The top bit of an IPMI major revision is a flag, not part of the number.

    Counted as part of the version it turns 3.11 into 131.11, which is how
    this was noticed.
    """
    suspect = []
    for name, info in firmware_inventory:
        version = str((info or {}).get('version', ''))
        major = version.split('.')[0]
        if major.isdigit() and int(major) >= 128:
            suspect.append('{0}={1}'.format(name, version))

    assert suspect == [], (
        'these look like the device-not-ready bit counted as part of the '
        'revision: {0}'.format(', '.join(suspect)))


async def test_every_entry_is_named(firmware_inventory):
    """An unnamed entry cannot be referred to, so it cannot be updated."""
    unnamed = [repr(entry) for entry in firmware_inventory
               if not str(entry[0]).strip()]
    assert unnamed == [], 'firmware entries with no name: {0}'.format(unnamed)


async def test_categories_are_not_all_the_whole_inventory(redfish_command,
                                                          firmware_inventory):
    """A filter that returns everything for every category is not filtering.

    The subset check above cannot see this. Asking for one category and being
    handed the lot satisfies it perfectly, which is precisely the shape of a
    filter that ignores its argument, and that was the failure the file set out
    to catch. Telling them apart needs more than one category in view at once,
    so it needs its own test.

    A device whose firmware really is all one category is not a defect, so this
    only concludes anything where there is more than one entry to divide.
    """
    everything = {entry[0] for entry in firmware_inventory}
    if len(everything) < 2:
        pytest.skip('only {0} firmware entry, nothing to divide'.format(
            len(everything)))

    answers = {}
    for category in CATEGORIES:
        try:
            answers[category] = {entry[0] for entry in
                                 await _firmware(redfish_command, category)}
        except (exc.UnsupportedFunctionality, exc.RedfishError):
            continue
    if not answers:
        pytest.skip('device supports no firmware category')

    whole = [name for name, selected in answers.items()
             if selected == everything]
    assert whole != list(answers), (
        'every category answered with the whole inventory ({0} entries), so '
        'the category argument is being ignored: {1}'.format(
            len(everything), ', '.join(sorted(answers))))
