"""Every read-only operation confluent performs against a BMC.

The broad sweep, over whichever transports the inventory describes. It does not
assert what a BMC answers, because that varies legitimately by model and
vendor. It asserts how it answers: either a result, or a refusal that says why.
What must never happen is a crash inside confluent, which reaches a user as
"Unexpected Error" with a traceback in the server log and tells them nothing.

The list below is the same for Redfish and IPMI. Which of it a given client
implements is discovered rather than declared, so this file needs nothing added
when a device speaks the other protocol, and a read that only one transport
offers is a skip on the other rather than a failure. That is why the file is
named for the role and not for a protocol.

That distinction is the whole point. A BMC without a licence service, or with
no media support, is entitled to refuse. A read that raises AttributeError
because a plugin called a method the client does not define is a defect, and
looks the same to a user.

Each operation is a separate test, so a run reports one line per read per
device and the pass and refuse pattern across a fleet is readable directly
from the summary. Deselect a noisy one with -k, and the per-test timeout in
addopts bounds each read on its own rather than the sweep as a whole.
"""

import asyncio
import inspect

import pytest

import aiohmi.exceptions as exc

# loop_scope='session' because bmc_command may hand back an IPMI client,
# which only answers on the loop that opened it. See the fixture.
pytestmark = [pytest.mark.hardware, pytest.mark.readonly,
              pytest.mark.asyncio(loop_scope='session')]


# Every read confluent makes against a BMC. Whether a given client implements
# one is discovered rather than declared: those it does not are skipped, which
# is how the same list serves more than one transport.
READS = (
    'get_power',
    'get_health',
    'get_sensor_descriptions',
    'get_sensor_data',
    'get_inventory_descriptions',
    'get_inventory',
    'get_firmware',
    'get_event_log',
    'get_bootdev',
    'get_identify',
    'get_leds',
    'get_bmc_configuration',
    'get_description',
    'get_hostname',
    'get_mci',
    'get_domain_name',
    'get_net_configuration',
    'get_ntp_enabled',
    'get_ntp_servers',
    'get_users',
    'list_media',
    'get_licenses',
    'get_update_status',
)

# Shapes that mean the read broke inside confluent rather than being refused
# by the device.
BUG_SHAPES = (AttributeError, TypeError, NotImplementedError, KeyError,
              IndexError, StopIteration, StopAsyncIteration,
              asyncio.TimeoutError)

# What a device is allowed to answer with instead of a result: aiohmi's own
# exception hierarchy, and nothing else. PyghmiException is its base, so
# naming it covers the specific ones and the bare base a handler sometimes
# raises directly; TemporaryError sits beside the hierarchy rather than under
# it, hence the second entry.
#
# The point of drawing the line at the library's own exceptions is what falls
# outside it. A RuntimeError, an OSError from a socket, a ValueError out of a
# parser: none of those is a device declining anything, they are the library
# or confluent coming apart, and each reaches a user as "Unexpected Error".
# This used to accept any exception at all that carried a non-empty message,
# which made the sweep report green for exactly the class of defect it exists
# to find.
EXPECTED_REFUSALS = (exc.PyghmiException, exc.TemporaryError)

# A few reads take an argument before they will do anything useful.
READ_ARGUMENTS = {
    'get_event_log': (False,),
}


async def _perform(method, arguments):
    """Call a read, draining it first if it yields rather than returns."""
    if inspect.isasyncgenfunction(method):
        return [entry async for entry in method(*arguments)]
    return await method(*arguments)


@pytest.mark.parametrize('operation', READS)
async def test_read_answers_or_refuses_with_a_reason(bmc_command,
                                                     operation):
    method = getattr(bmc_command, operation, None)
    if method is None:
        pytest.skip('client does not implement {0}'.format(operation))

    try:
        await _perform(method, READ_ARGUMENTS.get(operation, ()))
    except BUG_SHAPES as caught:
        pytest.fail('{0} broke inside confluent rather than being refused by '
                    'the device: {1}: {2}'.format(
                        operation, type(caught).__name__, caught))
    except EXPECTED_REFUSALS as caught:
        # A device is entitled to refuse, as long as it says something.
        assert str(caught).strip(), (
            '{0} was refused with an empty message, so a user is told '
            'nothing'.format(operation))
    except Exception as caught:  # the classification is what this test is for
        # Anything outside the refusals above is a failure, message or not.
        # This used to pass whatever carried a non-empty string, on the
        # argument that a reason tells a user something. It does not tell them
        # enough: a RuntimeError, a socket error or an unexpected error out of
        # a library all reach them as "Unexpected Error" just the same, and
        # accepting them here meant the sweep reported green for exactly the
        # class of defect it exists to find. A device that means to refuse has
        # a way to say so.
        pytest.fail('{0} raised {1}, which is neither a result nor a refusal '
                    'this device is entitled to make: {2}'.format(
                        operation, type(caught).__name__, caught or '(no message)'))


async def test_client_implements_what_confluent_calls(bmc_command):
    """At least the reads with no plausible reason to be absent are present.

    Plugins have called methods the client did not define, and each one
    answered "Unexpected Error" with an AttributeError in the server log.
    """
    universal = ('get_power', 'get_health', 'get_bootdev', 'get_inventory',
                 'get_firmware', 'get_sensor_data')
    missing = [name for name in universal
               if getattr(bmc_command, name, None) is None]
    assert missing == [], 'client is missing: {0}'.format(', '.join(missing))


async def test_identify_reports_a_state_a_caller_can_act_on(bmc_command):
    """Either one of the documented states, or a stated refusal.

    A client without the read at all is a skip rather than a failure: IPMI has
    a command to set the identify light and none to ask what it is doing, so
    aiohmi's IPMI client offers no get_identify to call.
    """
    read = getattr(bmc_command, 'get_identify', None)
    if read is None:
        pytest.skip('client does not implement get_identify')

    try:
        state = await read()
    except BUG_SHAPES as caught:
        pytest.fail('get_identify broke inside confluent: {0}: {1}'.format(
            type(caught).__name__, caught))
    except EXPECTED_REFUSALS:
        pytest.skip('device does not offer an identify state')

    assert 'identifystate' in state, state
    assert state['identifystate'] in ('on', 'off', 'blink'), state
