"""Shared fixtures for the confluent test suite.

Everything here exists so that individual test modules do not have to
re-implement path bootstrapping, datastore isolation or plugin stubbing. See
tests/README.md for the conventions.
"""

import configparser
import gc
import os

import pytest


# Environment variables that enable the correspondingly marked tier. Tests
# marked ``hardware`` need at least one of the CONFLUENT_TEST_* variables that
# names the hardware they talk to; ``lab`` tests need the deployment lab.
_HARDWARE_ENV = ('CONFLUENT_TEST_REDFISH_BMC', 'CONFLUENT_TEST_IPMI_BMC',
                 'CONFLUENT_TEST_SMM')
_LAB_ENV = ('CONFLUENT_TEST_LAB',)

# Module-level state in configmanager that a test can reasonably disturb. The
# datastore itself is a module global rather than instance state, so fixtures
# have to snapshot and restore these or tests leak into each other.
_CFM_GLOBALS = (
    '_cfgstore',
    'statelessmode',
    '_masterkey',
    '_masterintegritykey',
    '_txcount',
    '_hasquorum',
    '_ready',
    '_pendingchangesets',
    'cfgleader',
)


def pytest_configure(config):
    # asyncio reads this when a loop is created, so setting it before any test
    # runs is enough. Debug mode reports slow callbacks, coroutines that were
    # never scheduled and exceptions never retrieved from a task, all of which
    # are realistic failure modes in the console and plugin dispatch paths.
    os.environ.setdefault('PYTHONASYNCIODEBUG', '1')

    # confluent.messages reads /etc/confluent/service.cfg at import time (it
    # calls cfgfile.get_option at module scope), so neutralize the config
    # before any test module imports it. A stray service.cfg on a developer
    # box or a lab server would otherwise silently change test behaviour.
    from confluent.config import conf
    conf._config = configparser.ConfigParser()


def pytest_collection_modifyitems(config, items):
    hardware_ready = any(os.environ.get(name) for name in _HARDWARE_ENV)
    lab_ready = any(os.environ.get(name) for name in _LAB_ENV)
    skip_hardware = pytest.mark.skip(
        reason='needs hardware, set one of: ' + ', '.join(_HARDWARE_ENV))
    skip_lab = pytest.mark.skip(
        reason='needs the deployment lab, set ' + _LAB_ENV[0])
    for item in items:
        if 'hardware' in item.keywords and not hardware_ready:
            item.add_marker(skip_hardware)
        if 'lab' in item.keywords and not lab_ready:
            item.add_marker(skip_lab)


@pytest.fixture(autouse=True)
def _collect_garbage():
    """Attribute unawaited-coroutine warnings to the test that caused them.

    "coroutine ... was never awaited" is raised when the coroutine object is
    collected, which without this would often be during some later test. The
    pytest.ini filterwarnings rules turn that warning into an error, so the
    blame needs to land in the right place.
    """
    yield
    gc.collect()


@pytest.fixture(autouse=True)
def _isolate_service_cfg():
    """Keep a real /etc/confluent/service.cfg out of the test run.

    pytest_configure does this once before collection; this restores it around
    each test in case something re-reads or replaces it.
    """
    from confluent.config import conf
    saved = conf._config
    conf._config = configparser.ConfigParser()
    yield
    conf._config = saved


@pytest.fixture
def confluent_cfgdir(tmp_path):
    """An isolated configmanager datastore directory.

    Points ConfigManager at a temp directory and puts the module in stateless
    mode, so _bg_sync_to_file and _sync_to_file both return early: no DBM file
    is opened and no background writer thread is started. Yields the path for
    tests that want to assert about it; most tests want the ``configmanager``
    fixture instead.
    """
    from confluent.config import configmanager as cfm

    cfgdir = tmp_path / 'cfg'
    cfgdir.mkdir()

    saved = {name: getattr(cfm, name) for name in _CFM_GLOBALS}
    saved_cfgdir = cfm.ConfigManager._cfgdir

    cfm.ConfigManager._cfgdir = str(cfgdir)
    cfm.statelessmode = True
    cfm._cfgstore = None
    cfm.init(stateless=True)
    try:
        yield cfgdir
    finally:
        cfm.ConfigManager._cfgdir = saved_cfgdir
        for name, value in saved.items():
            setattr(cfm, name, value)


@pytest.fixture
def configmanager(confluent_cfgdir):
    """A ConfigManager backed by an isolated in-memory datastore."""
    from confluent.config import configmanager as cfm

    return cfm.ConfigManager(None)


@pytest.fixture
def pluginmap():
    """Stub out core.pluginmap and rebuild the resource tree.

    core.load_plugins() cannot be used from tests: it mutates sys.path,
    imports every plugin by bare module name (dragging in pysnmp, asyncssh,
    aiohttp and friends), and ends by registering the affluent plugin. It is
    also not safe to call twice. Building the route tree directly and
    injecting stubs is both faster and hermetic.

    Yields the plugin map; assign into it, and add routes to
    ``core.noderesources`` as needed. Both are restored afterwards.
    """
    from confluent import core

    core._init_core()
    saved_plugins = dict(core.pluginmap)
    saved_noderesources = core.noderesources
    core.noderesources = dict(core.noderesources)
    core.pluginmap.clear()
    try:
        yield core.pluginmap
    finally:
        core.pluginmap.clear()
        core.pluginmap.update(saved_plugins)
        core.noderesources = saved_noderesources
