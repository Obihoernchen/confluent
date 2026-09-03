"""nodebmcpassword, which nothing else in the suite reaches.

Two requests, not one: the tool first reads the BMC's user list to find the
uid the given name holds, then writes the password to that uid. Neither half
is exercised anywhere else, and the read half is the part that decides whether
the write goes to the right account.

The password written is the one the device already has. That is a real write,
taking the same path as any other, and it deliberately gives up the stronger
assertion of setting a new value and reading the effect. The reason is the
cost of a restore that does not happen: every other reversible test that fails
to put a setting back leaves a machine misconfigured, while this one would
leave confluent unable to authenticate to it at all. No other test here can
lock the suite out of its own device.

What replaces that assertion is the command afterwards: a second tool opening
a fresh session with the unchanged credential proves the account still works,
which is what a write that mangled it would break.

Calls this file will never make: any write to a user other than the one the
inventory already authenticates as, and any change to a password value.
"""

import pytest

pytestmark = [pytest.mark.hardware, pytest.mark.reversible]


def test_setting_the_current_password_keeps_the_account_usable(run_cli,
                                                               bmc_target,
                                                               service_node):
    """Restricted to a simulated device, and for two reasons.

    A password write against a machine somebody else is using is not something
    to do on the strength of a permissive allow:, which was written with
    settings like a boot override in mind. And the value has to be passed as
    an argument, which /proc publishes to every local user: the tool's other
    two ways in are an environment variable, no better here, and a prompt that
    needs a terminal a test does not have. A simulator's password is neither
    secret nor real, so on one of those there is nothing to expose.
    """
    if not bmc_target.get('simulator'):
        pytest.skip('a password write is limited to a simulated device')

    password = bmc_target.get('password')
    if not password:
        pytest.skip('device has no password configured to rewrite')

    returncode, output = run_cli('nodebmcpassword', service_node,
                                 bmc_target['user'], password)

    if returncode != 0:
        assert output.strip(), 'nodebmcpassword failed and said nothing'
        assert 'Unexpected' not in output, \
            'nodebmcpassword failed with an unexplained error: {0}'.format(
                output)
        pytest.skip('device refused nodebmcpassword: {0}'.format(output))

    assert service_node in output, output

    # A new process, so a new session negotiated with the credential that was
    # just written back. This is the assertion the file is built around: it
    # fails if the write reached the account and corrupted it.
    returncode, output = run_cli('nodepower', service_node)

    assert returncode == 0, \
        'the account stopped working after its password was rewritten: ' \
        '{0}'.format(output)


def test_an_unknown_user_is_refused_with_an_explanation(run_cli,
                                                        service_node):
    """The read half on its own, and safe on any device: a name no BMC has
    cannot resolve to a uid, so the write is never reached.

    Worth its own test because this is the path a typo takes, and a tool that
    cannot find the account has to say so rather than writing to whichever
    uid it happened to be left holding.
    """
    returncode, output = run_cli('nodebmcpassword', service_node,
                                 'nosuchbmcuser', 'unused')

    assert returncode != 0
    assert output.strip(), 'a failure told the user nothing'
    assert 'Unexpected' not in output, output
