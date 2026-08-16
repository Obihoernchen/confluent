"""How a manager address is read into a host and a port.

hardwaremanagement.manager is free text a user types, and both hardware
management plugins turn it into somewhere to connect. Getting that wrong is
quiet: the node simply cannot be reached, and the error names a timeout rather
than the address that caused it.

The two plugins are checked together and against the same table, because the
whole point of the ipmi one having been changed is that an address should mean
the same thing whichever method a node uses. Only the default port differs.

This is a pure function, so it needs no device and belongs in the unit tier.
"""

import pytest

from confluent.plugins.hardwaremanagement import ipmi
from confluent.plugins.hardwaremanagement import redfish


# address, expected host, and whether a port was named. The expected port is
# not written out, because it differs per plugin: what is asserted is that a
# named port is honoured and an unnamed or unusable one falls back.
ADDRESSES = (
    ('192.0.2.10', '192.0.2.10', None),
    ('192.0.2.10:8443', '192.0.2.10', 8443),
    ('  192.0.2.10:8443  ', '192.0.2.10', 8443),
    ('192.0.2.10/24', '192.0.2.10', None),
    # A bare IPv6 literal must keep its last group. Reading it as a port is the
    # mistake the bracket handling exists to prevent.
    ('2001:db8::1', '2001:db8::1', None),
    # Bracketed IPv6 is the one form the two plugins read differently, so it is
    # not in this table. See below.
    # Not a port: fall back rather than raise or connect somewhere arbitrary.
    ('192.0.2.10:notaport', '192.0.2.10', None),
    ('192.0.2.10:0', '192.0.2.10', None),
    ('192.0.2.10:65536', '192.0.2.10', None),
)

PLUGINS = (
    pytest.param(ipmi, 623, id='ipmi'),
    pytest.param(redfish, 443, id='redfish'),
)


def _params(plugin, address):
    return plugin.get_conn_params(
        'n1', {'hardwaremanagement.manager': {'value': address}})


@pytest.mark.parametrize('plugin,default', PLUGINS)
@pytest.mark.parametrize('address,host,port', ADDRESSES)
def test_address_is_split_into_a_host_and_a_port(plugin, default, address,
                                                 host, port):
    params = _params(plugin, address)

    assert params['bmc'] == host, address
    assert params['port'] == (default if port is None else port), address


@pytest.mark.parametrize('address,port', (
    ('[2001:db8::1]', None),
    ('[2001:db8::1]:8443', 8443),
))
def test_brackets_come_off_only_where_the_address_is_resolved(address, port):
    """The one place the two plugins deliberately disagree.

    The ipmi plugin hands its address to socket.getaddrinfo, which rejects a
    bracketed literal outright, so the brackets have to come off or every
    operation fails on resolution. The redfish plugin puts its address in a
    URL, where an IPv6 literal has to be bracketed, so they have to stay.

    Asserted together so the divergence stays deliberate. Making either plugin
    match the other looks like a tidy-up and breaks one of them.
    """
    ipmiparams = _params(ipmi, address)
    assert ipmiparams['bmc'] == '2001:db8::1', address
    assert ipmiparams['port'] == (623 if port is None else port), address

    redfishparams = _params(redfish, address)
    assert redfishparams['bmc'] == '[2001:db8::1]', address
    assert redfishparams['port'] == (443 if port is None else port), address


@pytest.mark.parametrize('plugin,default', PLUGINS)
def test_the_node_name_is_used_when_no_manager_is_set(plugin, default):
    """A node with no manager attribute is reached at its own name."""
    params = plugin.get_conn_params('n1', {})

    assert params['bmc'] == 'n1'
    assert params['port'] == default


@pytest.mark.parametrize('plugin,default', PLUGINS)
def test_credentials_have_defaults_rather_than_being_absent(plugin, default):
    """Both plugins guess rather than omit, and callers rely on the keys."""
    params = _params(plugin, '192.0.2.10')

    assert params['username']
    assert params['passphrase']
