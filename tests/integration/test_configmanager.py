"""Attribute inheritance and expression evaluation in the config manager.

These are the tests the "some unit tests worth implementing" comment block at
the end of configmanager.py has been asking for since the IBM days:

    set group attribute on lower priority group, result is that node should
    not change; after that point, then unset on the higher priority group,
    lower priority group should get it then; rinse and repeat for set on node
    versus set on group; clear group attribute and assure that it becomes
    unset on all nodes; set various expressions

They are marked integration rather than unit because they drive a real
ConfigManager, albeit one the configmanager fixture has pointed at a temp
directory and put into stateless mode, so nothing is written to disk.
"""

import pytest

pytestmark = pytest.mark.integration


async def _make_node_in_groups(configmanager, node, groups):
    """Groups are listed in priority order: the first one wins."""
    await configmanager.set_group_attributes(
        {group: {} for group in groups}, autocreate=True)
    await configmanager.set_node_attributes(
        {node: {'groups': list(groups)}}, autocreate=True)


async def test_lower_priority_group_does_not_override_higher(configmanager):
    await _make_node_in_groups(configmanager, 'n1', ['hi', 'lo'])
    await configmanager.set_group_attributes({'hi': {'console.method': 'redfish'}})

    await configmanager.set_group_attributes({'lo': {'console.method': 'ipmi'}})

    attrs = configmanager.get_node_attributes('n1', 'console.method')
    assert attrs['n1']['console.method']['value'] == 'redfish'
    assert attrs['n1']['console.method']['inheritedfrom'] == 'hi'


async def test_clearing_higher_priority_group_reveals_the_lower(configmanager):
    await _make_node_in_groups(configmanager, 'n1', ['hi', 'lo'])
    await configmanager.set_group_attributes({'lo': {'console.method': 'ipmi'}})
    await configmanager.set_group_attributes({'hi': {'console.method': 'redfish'}})

    await configmanager.clear_group_attributes(['hi'], ['console.method'])

    attrs = configmanager.get_node_attributes('n1', 'console.method')
    assert attrs['n1']['console.method']['value'] == 'ipmi'
    assert attrs['n1']['console.method']['inheritedfrom'] == 'lo'


async def test_node_attribute_overrides_every_group(configmanager):
    await _make_node_in_groups(configmanager, 'n1', ['hi', 'lo'])
    await configmanager.set_group_attributes({'hi': {'console.method': 'redfish'}})

    await configmanager.set_node_attributes({'n1': {'console.method': 'tsmsol'}})

    attrs = configmanager.get_node_attributes('n1', 'console.method')
    assert attrs['n1']['console.method']['value'] == 'tsmsol'
    assert 'inheritedfrom' not in attrs['n1']['console.method']


async def test_clearing_node_attribute_restores_group_inheritance(configmanager):
    await _make_node_in_groups(configmanager, 'n1', ['hi', 'lo'])
    await configmanager.set_group_attributes({'lo': {'console.method': 'ipmi'}})
    await configmanager.set_node_attributes({'n1': {'console.method': 'tsmsol'}})

    await configmanager.clear_node_attributes(['n1'], ['console.method'])

    attrs = configmanager.get_node_attributes('n1', 'console.method')
    assert attrs['n1']['console.method']['value'] == 'ipmi'
    assert attrs['n1']['console.method']['inheritedfrom'] == 'lo'


async def test_clearing_group_attribute_unsets_it_on_all_members(configmanager):
    await configmanager.set_group_attributes({'compute': {}}, autocreate=True)
    await configmanager.set_node_attributes({
        'n1': {'groups': ['compute']},
        'n2': {'groups': ['compute']},
    }, autocreate=True)
    await configmanager.set_group_attributes({'compute': {'console.method': 'ipmi'}})

    await configmanager.clear_group_attributes(['compute'], ['console.method'])

    attrs = configmanager.get_node_attributes(['n1', 'n2'], 'console.method')
    assert attrs['n1'] == {}
    assert attrs['n2'] == {}


async def test_expression_is_evaluated_per_node(configmanager):
    """An expression set once on a group yields a different value per node."""
    await configmanager.set_group_attributes({'compute': {}}, autocreate=True)
    await configmanager.set_node_attributes({
        'n1': {'groups': ['compute']},
        'n2': {'groups': ['compute']},
    }, autocreate=True)

    await configmanager.set_group_attributes({'compute': {
        'hardwaremanagement.manager': {'expression': '{nodename}-bmc'}}})

    attrs = configmanager.get_node_attributes(
        ['n1', 'n2'], 'hardwaremanagement.manager')
    assert attrs['n1']['hardwaremanagement.manager']['value'] == 'n1-bmc'
    assert attrs['n2']['hardwaremanagement.manager']['value'] == 'n2-bmc'
    # The expression itself is preserved alongside the evaluated value.
    assert attrs['n1']['hardwaremanagement.manager']['expression'] == '{nodename}-bmc'


async def test_expression_can_reference_another_attribute(configmanager):
    await configmanager.set_node_attributes({'n1': {'id.uuid': '1234'}},
                                            autocreate=True)

    await configmanager.set_node_attributes(
        {'n1': {'info.note': {'expression': 'uuid is {id.uuid}'}}})

    attrs = configmanager.get_node_attributes('n1', 'info.note')
    assert attrs['n1']['info.note']['value'] == 'uuid is 1234'


async def test_invalid_expression_is_rejected(configmanager):
    await configmanager.set_node_attributes({'n1': {}}, autocreate=True)

    with pytest.raises(ValueError, match="expected '}' before end of string"):
        await configmanager.set_node_attributes(
            {'n1': {'info.note': {'expression': 'unbalanced {brace'}}})


async def test_datastore_stays_in_memory(configmanager, confluent_cfgdir):
    """The fixture promises stateless mode, so exercising the manager must not
    write a DBM file. If this fails, every other test here is touching disk."""
    await configmanager.set_node_attributes({'n1': {'console.method': 'ipmi'}},
                                            autocreate=True)

    assert list(confluent_cfgdir.iterdir()) == []
