"""The boot override through nodesetboot, which is the write a user makes.

test_ipmi_bootdev.py and test_redfish_bootdev.py already set this override,
but they call the protocol library directly. Everything between the terminal
and that library goes untested by them: argument parsing, the update path
through simple_noderange_command, the socket protocol and plugin dispatch.
That layer is where a working call turns into a refusal nobody can act on, and
running the command is the only way to reach it.

So this is the counterpart of test_cli_readonly.py's sweep for a write, and
the same operation the library files cover one layer down.

A device that will not report its current override is skipped rather than
written to, because the restore afterwards depends on that read.

Calls this file will never make: anything that powers, resets or reseats the
system, and any write outside the boot override itself.
"""

import pytest

pytestmark = [pytest.mark.hardware, pytest.mark.reversible]


def _reported(output, node):
    """The value a node* tool printed for this node, or None."""
    prefix = '{0}: '.format(node)
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _distinct_from(original):
    """A boot device to write that is not the one already set.

    Chosen against the current setting rather than hardcoded, because a device
    already set to the value a test writes turns that test into an assertion
    that nothing changed, which holds whether the write worked or not. Both
    captures under tests/support ship an override of network, so a fixed
    'network' here would prove nothing on exactly the devices CI runs.

    The two values are the benign ones: 'default' withdraws a directed boot
    request, and 'network' is the one every device in the inventory accepts.
    """
    return 'default' if original == 'network' else 'network'


@pytest.fixture
def restored_bootdev(run_cli, service_node):
    """Capture the override with the CLI and put it back afterwards.

    The read doubles as the gate: a device that refuses it, as a simulator
    with no chassis boot options does, is skipped before anything is written,
    since there would be no way to know what to restore.
    """
    returncode, output = run_cli('nodesetboot', service_node)
    original = _reported(output, service_node)
    if returncode != 0 or not original:
        pytest.skip('device will not report its boot override: {0}'.format(
            output))
    try:
        yield original
    finally:
        # Teardown, so a failed assertion leaves the device as it was found.
        run_cli('nodesetboot', service_node, original)


def test_an_override_survives_the_command_that_set_it(run_cli, service_node,
                                                      restored_bootdev):
    """Set it with one command, read it with another.

    The second command is a new process opening its own connection, so what it
    reports came back from the device rather than from anything the first one
    remembered.
    """
    wanted = _distinct_from(restored_bootdev)

    returncode, output = run_cli('nodesetboot', service_node, wanted)

    assert returncode == 0, output
    assert _reported(output, service_node) == wanted, output

    returncode, output = run_cli('nodesetboot', service_node)

    assert returncode == 0, output
    assert _reported(output, service_node) == wanted, output


def test_clearing_the_override_is_read_back_as_default(run_cli, service_node,
                                                       restored_bootdev):
    """'default' is how a directed boot request is withdrawn, and a device
    with none set reports it, so this is the state the fixture restores to on
    a machine that started with no override."""
    returncode, output = run_cli('nodesetboot', service_node, 'network')

    assert returncode == 0, output
    assert _reported(output, service_node) == 'network', output

    returncode, output = run_cli('nodesetboot', service_node, 'default')

    assert returncode == 0, output

    returncode, output = run_cli('nodesetboot', service_node)

    assert returncode == 0, output
    assert _reported(output, service_node) == 'default', output


def test_an_unknown_device_is_refused_with_the_list_of_valid_ones(
        run_cli, service_node):
    """A mistyped boot device has to say which ones are accepted.

    Deliberately without the fixture above. The value is rejected before the
    device is reached, so there is nothing to restore and nothing to read
    back, and staying off the fixture is what lets this run against a device
    that cannot report an override at all.
    """
    returncode, output = run_cli('nodesetboot', service_node, 'nosuchdevice')

    assert returncode != 0
    assert 'nosuchdevice' in output, output
    assert 'network' in output, \
        'a rejected boot device did not say which are accepted: {0}'.format(
            output)
    assert 'Unexpected' not in output, output


def test_prior_setting_is_restored(run_cli, service_node, restored_bootdev):
    """The fixture's own contract, asserted so a broken restore fails loudly
    rather than quietly leaving the machine changed."""
    wanted = _distinct_from(restored_bootdev)

    returncode, output = run_cli('nodesetboot', service_node, wanted)

    assert returncode == 0, output
    assert _reported(output, service_node) == wanted, output

    returncode, output = run_cli('nodesetboot', service_node,
                                 restored_bootdev)

    assert returncode == 0, output
    assert _reported(output, service_node) == restored_bootdev, output
