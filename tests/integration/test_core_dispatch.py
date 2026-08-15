"""Plugin dispatch through core.handle_path.

This is the test that justifies pytest-asyncio over unittest's
IsolatedAsyncioTestCase: the whole dispatch path is built on async generators,
plugins yield their results, and consuming them requires `async for`. There is
no way to express the stub plugins below as an asyncSetUp.

The routing mechanisms covered here, as defined by PluginRoute in core.py:

- ``{'handler': name}``   fixed handler, resolved straight out of pluginmap
- ``{'pluginattrs': [..]}`` handler chosen per node from a config attribute,
  which is how the real hardwaremanagement plugins are selected

core.load_plugins() is never called: it mutates sys.path, imports every plugin
by bare module name (pysnmp, asyncssh, aiohttp, EfiCompressor) and is not safe
to call twice. The pluginmap fixture builds the route tree directly instead.
"""

import pytest

import confluent.messages as msg
from confluent import core

pytestmark = pytest.mark.integration


def _stub_plugin(label):
    """A plugin whose retrieve() is an async generator, like the real ones."""

    class StubPlugin(object):
        @staticmethod
        async def retrieve(nodes, element, configmanager, inputdata):
            for node in nodes:
                yield msg.KeyValueData({'backend': label}, name=node)

    return StubPlugin


async def _collect(response):
    return [rsp.kvpairs async for rsp in response]


async def test_fixed_handler_route(configmanager, pluginmap):
    pluginmap['teststub'] = _stub_plugin('stub')
    core.noderesources['_teststate'] = core.PluginRoute({'handler': 'teststub'})
    await configmanager.set_node_attributes({'n1': {}}, autocreate=True)

    response = await core.handle_path('/nodes/n1/_teststate', 'retrieve',
                                      configmanager)

    assert await _collect(response) == [{'backend': 'stub'}]


async def test_single_node_response_is_stripped_of_the_node_name(
        configmanager, pluginmap):
    """A /nodes/<name>/ request strips the node key; /noderange/ keeps it, so
    the caller can tell responses apart."""
    pluginmap['teststub'] = _stub_plugin('stub')
    core.noderesources['_teststate'] = core.PluginRoute({'handler': 'teststub'})
    await configmanager.set_node_attributes({'n1': {}, 'n2': {}}, autocreate=True)

    single = await core.handle_path('/nodes/n1/_teststate', 'retrieve',
                                    configmanager)
    ranged = await core.handle_path('/noderange/n1-n2/_teststate', 'retrieve',
                                    configmanager)

    assert await _collect(single) == [{'backend': 'stub'}]
    # A noderange resolves through a set, so the response order is not defined.
    assert sorted(await _collect(ranged), key=lambda kv: sorted(kv)[0]) == [
        {'n1': {'backend': 'stub'}},
        {'n2': {'backend': 'stub'}},
    ]


async def test_handler_is_chosen_per_node_from_an_attribute(configmanager,
                                                            pluginmap):
    """hardwaremanagement.method selects the backend, falling back to the
    route's default when the attribute is unset."""
    pluginmap['ipmi'] = _stub_plugin('ipmi')
    pluginmap['redfish'] = _stub_plugin('redfish')
    core.noderesources['_testbackend'] = core.PluginRoute({
        'pluginattrs': ['hardwaremanagement.method'],
        'default': 'ipmi',
    })
    await configmanager.set_node_attributes({
        'n1': {},
        'n2': {'hardwaremanagement.method': 'redfish'},
    }, autocreate=True)

    defaulted = await core.handle_path('/nodes/n1/_testbackend', 'retrieve',
                                       configmanager)
    explicit = await core.handle_path('/nodes/n2/_testbackend', 'retrieve',
                                      configmanager)

    assert await _collect(defaulted) == [{'backend': 'ipmi'}]
    assert await _collect(explicit) == [{'backend': 'redfish'}]


async def test_noderange_fans_out_across_different_handlers(configmanager,
                                                            pluginmap):
    """One noderange request, two backends, results merged into one stream."""
    pluginmap['ipmi'] = _stub_plugin('ipmi')
    pluginmap['redfish'] = _stub_plugin('redfish')
    core.noderesources['_testbackend'] = core.PluginRoute({
        'pluginattrs': ['hardwaremanagement.method'],
        'default': 'ipmi',
    })
    await configmanager.set_node_attributes({
        'n1': {},
        'n2': {'hardwaremanagement.method': 'redfish'},
    }, autocreate=True)

    response = await core.handle_path('/noderange/n1-n2/_testbackend',
                                      'retrieve', configmanager)

    results = sorted(await _collect(response), key=lambda kv: sorted(kv)[0])
    assert results == [
        {'n1': {'backend': 'ipmi'}},
        {'n2': {'backend': 'redfish'}},
    ]


async def test_unknown_node_is_rejected(configmanager, pluginmap):
    pluginmap['teststub'] = _stub_plugin('stub')
    core.noderesources['_teststate'] = core.PluginRoute({'handler': 'teststub'})

    with pytest.raises(Exception, match='Invalid Node'):
        await core.handle_path('/nodes/nosuchnode/_teststate', 'retrieve',
                               configmanager)


async def test_unknown_element_is_rejected(configmanager, pluginmap):
    await configmanager.set_node_attributes({'n1': {}}, autocreate=True)

    with pytest.raises(Exception, match='Invalid element requested'):
        await core.handle_path('/nodes/n1/_nosuchelement', 'retrieve',
                               configmanager)


async def test_pluginmap_fixture_restores_state(configmanager, pluginmap):
    """The fixture must leave core.pluginmap and core.noderesources as it found
    them, or tests would contaminate each other through module globals."""
    pluginmap['teststub'] = _stub_plugin('stub')
    core.noderesources['_teststate'] = core.PluginRoute({'handler': 'teststub'})

    assert 'teststub' in core.pluginmap
    assert '_teststate' in core.noderesources
    # Restoration itself is asserted by test_dispatch_isolation below, which
    # runs against a fresh fixture instance.


async def test_dispatch_isolation(configmanager, pluginmap):
    """Nothing from the preceding tests leaks into this one."""
    assert '_teststate' not in core.noderesources
    assert '_testbackend' not in core.noderesources
    assert core.pluginmap == {}
