"""The fixtures must leave global state exactly as they found it.

Both the configmanager and the pluginmap fixtures work by patching module
globals, because that is how the code under test stores its state. That makes
restoration a correctness property of the suite rather than a nicety: a leak
here shows up as an unrelated test failing later, or worse, as a test passing
because of something a previous test left behind.

Restoring a reference is not enough for a mutable container, since an in-place
mutation survives it. The fixtures therefore swap in fresh containers and put
the originals back, and this file checks that they do.

The baseline is captured at import time, which pytest does for every test
module before running anything, so it reflects the state before any fixture
has run.

These checks are strongest in file order, where the test that dirties state
runs first. Run under a randomizing plugin they still pass but prove less, so
if test order is ever randomized, rewrite this using the pytester fixture to
run a nested session instead.
"""

from confluent import core
from confluent.config import configmanager as cfm


BASELINE = {
    'has_noderesources': hasattr(core, 'noderesources'),
    'has_nodegroupresources': hasattr(core, 'nodegroupresources'),
    'pluginmap': dict(core.pluginmap),
    'pendingchangesets_id': id(cfm._pendingchangesets),
    'attribwatchers_id': id(cfm.ConfigManager._attribwatchers),
    'nodecollwatchers_id': id(cfm.ConfigManager._nodecollwatchers),
    'notifierids_id': id(cfm.ConfigManager._notifierids),
    'cfgdir': cfm.ConfigManager._cfgdir,
}


def test_fixtures_are_used_and_state_is_dirtied(pluginmap, configmanager):
    """Deliberately scribble on everything the fixtures are meant to protect.
    The next test checks none of it survived."""
    pluginmap['scribble'] = object()
    core.noderesources['_scribble'] = object()
    core.nodegroupresources['_scribble'] = object()
    cfm._pendingchangesets['scribble'] = 1
    cfm.ConfigManager._attribwatchers['scribble'] = 1
    cfm.ConfigManager._nodecollwatchers['scribble'] = 1
    cfm.ConfigManager._notifierids['scribble'] = 1
    assert cfm.ConfigManager._cfgdir != BASELINE['cfgdir']


def test_core_resource_trees_are_restored():
    """Neither tree exists until the first _init_core() call, so "restored"
    can mean removing them again."""
    assert hasattr(core, 'noderesources') is BASELINE['has_noderesources']
    assert hasattr(core, 'nodegroupresources') is BASELINE['has_nodegroupresources']


def test_pluginmap_is_restored():
    assert core.pluginmap == BASELINE['pluginmap']


def test_configmanager_datastore_path_is_restored():
    assert cfm.ConfigManager._cfgdir == BASELINE['cfgdir']


def test_mutable_globals_keep_their_identity_and_lose_the_mutations():
    """The original container objects come back, and nothing written during a
    test is still in them."""
    assert id(cfm._pendingchangesets) == BASELINE['pendingchangesets_id']
    assert 'scribble' not in cfm._pendingchangesets


def test_configmanager_class_watchers_are_restored():
    watchers = (
        ('attribwatchers_id', cfm.ConfigManager._attribwatchers),
        ('nodecollwatchers_id', cfm.ConfigManager._nodecollwatchers),
        ('notifierids_id', cfm.ConfigManager._notifierids),
    )
    for key, container in watchers:
        assert id(container) == BASELINE[key]
        assert 'scribble' not in container
