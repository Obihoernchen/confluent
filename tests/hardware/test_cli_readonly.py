"""The CLI tools against a real device, through a real confluent service.

The deepest tests in the suite. Each one runs the actual command a user runs,
which reaches the device through argument parsing, the socket protocol, the
service, resource dispatch, a hardware management plugin and the protocol
library. Everything below is exercised as a side effect of asserting on what
the user is shown.

That is the point. The layers between the protocol library and the terminal
are where a perfectly good answer turns into "Unexpected Error", and no test
below this one can see it happen.

These assert on output shape rather than values, since power state, firmware
versions and inventory differ per machine and over time. What is being pinned
is that the command succeeds, names the node, and says something.

Everything here is read-only. A command that changes the device belongs in a
file of its own with the matching marker.
"""

import re

import pytest

pytestmark = [pytest.mark.hardware, pytest.mark.readonly]


def _node_lines(output, node):
    """The output lines that report about a given node."""
    return [line for line in output.splitlines()
            if line.startswith('{0}:'.format(node))]


def test_service_lists_the_configured_nodes(run_cli, service_node):
    returncode, output = run_cli('nodelist')

    assert returncode == 0, output
    assert service_node in output.splitlines()


# The third field is whether a non-zero exit can still carry an answer.
# nodehealth is the one that can: it reports what it read through the exit
# code as well as the output, so "not ok" is a successful read of a bad state.
# Everywhere else a non-zero exit means the read did not happen, and treating
# the two alike would let a real refusal pass for an answer.
@pytest.mark.parametrize('tool,pattern,exit_carries_the_answer', [
    ('nodepower', r'^\S+: (on|off)$', False),
    ('nodehealth', r'^\S+: \w+', True),
    ('nodeidentify', r'^\S+: (on|off|blink)$', False),
    # Reading the boot override, not setting one. Passing no device is a read:
    # simple_noderange_command sends an update only when it has an input, and
    # with none it fetches /boot/nextdevice instead. Confirmed against a device
    # as well as in the source, by reading the override either side of the call
    # and checking it did not move. Never give this entry an argument.
    ('nodesetboot', r'^\S+: (default|cd|network|http|setup|hd|usb|floppy)\b',
     False),
])
def test_state_commands_report_a_usable_value(run_cli, service_node, tool,
                                              pattern,
                                              exit_carries_the_answer):
    """The answer has to be one a caller can act on, not merely non-empty.

    A device is allowed not to offer a given state, as long as it says so.
    Not every BMC has an identify light. What is not allowed is failing
    without an explanation, which is the same contract the protocol-level
    sweep applies one layer down.

    An answer is not the same as a zero exit, which is why the table carries
    the third field. nodehealth exits non-zero for a node that is unhealthy,
    so reading a machine in warning state used to skip here as though the
    command had been refused, and the shape of what it printed went unchecked
    against every device that was not perfectly well.
    """
    returncode, output = run_cli(tool, service_node)
    lines = _node_lines(output, service_node)
    answered = bool(lines) and bool(re.match(pattern, lines[0]))

    # Checked on every path, not only the failing one. These patterns are
    # deliberately loose, and "n1: Unexpected Error: 'PCIeDevices'" satisfies
    # r'^\S+: \w+' perfectly, so leaving this inside the refusal branch let an
    # unexplained error count as an answer the moment a non-zero exit was
    # allowed to carry one. A tool exiting zero while printing one would be
    # worth catching here too.
    assert 'Unexpected' not in output, \
        '{0} reported an unexplained error: {1}'.format(tool, output)

    if returncode != 0 and not (exit_carries_the_answer and answered):
        assert output.strip(), '{0} failed and said nothing'.format(tool)
        pytest.skip('device refused {0}: {1}'.format(tool, output))

    assert lines, 'no line reported for {0}:\n{1}'.format(service_node, output)
    assert re.match(pattern, lines[0]), lines[0]


@pytest.mark.parametrize('tool,arguments', [
    ('nodesensors', ()),
    # Never 'clear'. That is the one argument this file must not pass.
    ('nodeeventlog', ()),
    ('nodelicense', ('list',)),
    # Reading the bmc configuration. An assignment would be a write, so this
    # entry must stay argument-free: nodeconfig only updates when it is given
    # something with an '=' in it.
    ('nodeconfig', ()),
    # Each of these three is one subcommand of a tool whose others write. The
    # subcommand is the whole safety argument, so do not generalise these
    # entries: upload, attach and detachall write media, create and delete
    # write storage, and installbmccacert, removebmccacert and signbmccert
    # write certificates.
    ('nodemedia', ('list',)),
    ('nodestorage', ('show',)),
    ('nodecertutil', ('listbmccacerts',)),
])
def test_read_commands_answer_or_refuse_with_a_reason(run_cli, service_node,
                                                      tool, arguments):
    """The CLI counterpart of the protocol sweep in test_bmc_reads.py.

    These three have no single output shape worth pinning: sensors vary per
    machine, an event log is legitimately empty, and a device without a licence
    service says so. What does hold for all of them is the contract the sweep
    applies one layer down, plus one the sweep cannot see.

    A command that fails must say why and must not say "Unexpected Error". And
    a command that reports success must not have printed an error while doing
    it: a zero exit with an error in the output is worse than a failure,
    because `nodesensors n1 && next-step` then runs next-step.
    """
    returncode, output = run_cli(tool, service_node, *arguments)

    if returncode != 0:
        assert output.strip(), '{0} failed and said nothing'.format(tool)
        assert 'Unexpected' not in output, \
            '{0} failed with an unexplained error: {1}'.format(tool, output)
        pytest.skip('device refused {0}: {1}'.format(tool, output))

    errors = [line for line in output.splitlines() if 'Error:' in line]
    assert errors == [], (
        '{0} exited 0 after reporting {1}, so a caller checking the exit code '
        'is told it worked'.format(tool, '; '.join(errors)))


def test_inventory_reports_hardware_detail(run_cli, service_node):
    returncode, output = run_cli('nodeinventory', service_node, 'system')

    assert returncode == 0, output
    assert _node_lines(output, service_node), output


def test_firmware_reports_a_version(run_cli, service_node):
    returncode, output = run_cli('nodefirmware', service_node)

    assert returncode == 0, output
    assert _node_lines(output, service_node), output


def test_attributes_round_trip_through_the_service(run_cli, service_node):
    """An attribute set in the configuration comes back through the API.

    This is the mapping layer: the value the service was configured with has
    to survive dispatch and serialization to reach the caller.
    """
    returncode, output = run_cli('nodeattrib', service_node,
                                 'hardwaremanagement.method')

    assert returncode == 0, output
    assert 'hardwaremanagement.method' in output
    assert re.search(r'hardwaremanagement\.method:\s*\S+', output), output


def test_secrets_are_not_echoed_back(run_cli, bmc_target, service_node):
    """Reading attributes must not print the stored BMC password.

    Worth pinning explicitly: these tests run with a real credential in the
    configuration, and an attribute listing is the likeliest place for one to
    escape into a terminal or a log. The assertion is on the credential
    itself, not on the presence of asterisks, so it cannot be satisfied by
    some other field happening to be redacted.

    Not a vacuous check: the attribute is listed, as a redacted value.
    """
    password = bmc_target.get('password')
    if not password:
        pytest.skip('device has no password configured to leak')

    returncode, output = run_cli('nodeattrib', service_node)

    assert returncode == 0, output
    # Compare against the reported values only. A substring search over the
    # whole output gives a false positive when the credential happens to be a
    # common word: one device here uses "password", which appears inside the
    # attribute name secret.hardwaremanagementpassword.
    values = [line.rsplit(': ', 1)[-1].strip()
              for line in output.splitlines()]
    leaked = [value for value in values if password in value]
    assert leaked == [], \
        'the attribute listing exposed the stored BMC password'


def test_unknown_node_is_refused_with_an_explanation(run_cli):
    """The failure path users actually hit, and where a bare exception would
    surface as "Unexpected Error" with nothing to act on."""
    returncode, output = run_cli('nodepower', 'nosuchnode')

    assert returncode != 0
    assert output.strip(), 'a failure told the user nothing'
    assert 'nosuchnode' in output, output
    assert 'Unexpected' not in output, output


def test_unknown_attribute_is_refused_with_an_explanation(run_cli,
                                                          service_node):
    returncode, output = run_cli('nodeattrib', service_node,
                                 'nosuchattribute')

    assert returncode != 0
    assert 'nosuchattribute' in output, output
    assert 'Unexpected' not in output, output
