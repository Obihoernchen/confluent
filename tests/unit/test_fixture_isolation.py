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

import pytest

from confluent import core
from confluent import noderange
from confluent.config import configmanager as cfm


# These tests only mean anything together and in order: one dirties state and
# the rest check it was restored. Under -n the default distribution would put
# them on different workers, where nothing was ever dirtied and every check
# passes vacuously. The group keeps them on one worker.
pytestmark = pytest.mark.xdist_group('fixture-isolation')


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


# Set by the test below once it has actually scribbled. Every check in this
# file compares against a baseline, and a baseline matches a pristine process
# perfectly, so without knowing that something was dirtied first they all pass
# while proving nothing. The module comment above guards the xdist case; this
# guards the likelier ones, -k selecting the checks on their own and the
# dirtying test failing in setup.
_DIRTIED = False


def _require_dirt():
    if not _DIRTIED:
        pytest.fail(
            'test_fixtures_are_used_and_state_is_dirtied did not run, so this '
            'check has nothing to prove: it would pass against an untouched '
            'process. Run this file whole and in order.')


def test_fixtures_are_used_and_state_is_dirtied(pluginmap, configmanager):
    """Deliberately scribble on everything the fixtures are meant to protect.
    The next test checks none of it survived."""
    global _DIRTIED
    pluginmap['scribble'] = object()
    core.noderesources['_scribble'] = object()
    core.nodegroupresources['_scribble'] = object()
    cfm._pendingchangesets['scribble'] = 1
    cfm.ConfigManager._attribwatchers['scribble'] = 1
    cfm.ConfigManager._nodecollwatchers['scribble'] = 1
    cfm.ConfigManager._notifierids['scribble'] = 1
    assert cfm.ConfigManager._cfgdir != BASELINE['cfgdir']

    # Building any range populates a module-level cache that ReverseNodeRange
    # reads. Left in place it would let this test decide another test's
    # abbreviation result.
    noderange.NodeRange('n[1-2]')
    assert noderange.lastnoderange

    _DIRTIED = True


def test_core_resource_trees_are_restored():
    """Neither tree exists until the first _init_core() call, so "restored"
    can mean removing them again."""
    _require_dirt()
    assert hasattr(core, 'noderesources') is BASELINE['has_noderesources']
    assert hasattr(core, 'nodegroupresources') is BASELINE['has_nodegroupresources']


def test_pluginmap_is_restored():
    _require_dirt()
    assert core.pluginmap == BASELINE['pluginmap']


def test_configmanager_datastore_path_is_restored():
    _require_dirt()
    assert cfm.ConfigManager._cfgdir == BASELINE['cfgdir']


def test_mutable_globals_keep_their_identity_and_lose_the_mutations():
    """The original container objects come back, and nothing written during a
    test is still in them."""
    _require_dirt()
    assert id(cfm._pendingchangesets) == BASELINE['pendingchangesets_id']
    assert 'scribble' not in cfm._pendingchangesets


def test_noderange_cache_starts_clean():
    """The previous test built a range. Every test must still begin with an
    empty cache, or ReverseNodeRange short-circuits against whatever the last
    test happened to evaluate and abbreviation results become order-dependent.
    """
    _require_dirt()
    assert noderange.lastnoderange is None


def test_configmanager_class_watchers_are_restored():
    _require_dirt()
    watchers = (
        ('attribwatchers_id', cfm.ConfigManager._attribwatchers),
        ('nodecollwatchers_id', cfm.ConfigManager._nodecollwatchers),
        ('notifierids_id', cfm.ConfigManager._notifierids),
    )
    for key, container in watchers:
        assert id(container) == BASELINE[key]
        assert 'scribble' not in container
