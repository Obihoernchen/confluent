"""Shared fixtures for the confluent test suite.

Everything here exists so that individual test modules do not have to
re-implement path bootstrapping, datastore isolation or plugin stubbing. See
tests/README.md for the conventions.
"""

import asyncio
import configparser
import functools
import gc
import os

import pytest


# Environment variables that enable the correspondingly marked tier. The
# collection hook below is a coarse gate: it keeps a default run away from
# hardware entirely. Per-target precision comes from the redfish_bmc, ipmi_bmc
# and smm fixtures, so that naming one BMC does not enable tests for equipment
# that is not present.
_HARDWARE_ENV = ('CONFLUENT_TEST_REDFISH_BMC', 'CONFLUENT_TEST_IPMI_BMC',
                 'CONFLUENT_TEST_SMM', 'CONFLUENT_TEST_HARDWARE')
_LAB_ENV = ('CONFLUENT_TEST_LAB',)

# Path to a YAML inventory of test equipment, so more than one device can be
# exercised in a run and credentials stay off the command line, where ps would
# expose them. Top level is keyed by kind, which leaves room for ipmi, smm and
# pdu sections beside redfish:
#
#   redfish:
#     - name: xcc-lab1
#       address: 192.0.2.10       # :port permitted
#       user: admin
#       password: ...
#
# Keep the file outside the repository.
_INVENTORY_ENV = 'CONFLUENT_TEST_HARDWARE'

# What a test is allowed to do to a device, in increasing order of risk. Each
# level includes everything below it.
#
#   readonly     sends no writes at all
#   reversible   writes, but restores the prior state; no service interruption
#   disruptive   interrupts service, but the device recovers on its own
#   destructive  may need a human afterwards, or risks data loss
#
# The split between the last two is deliberate: a power cycle and a firmware
# flash are not the same event, and a shared machine may reasonably permit the
# first while forbidding the second.
_SAFETY_LEVELS = ('readonly', 'reversible', 'disruptive', 'destructive')

# Inventory sections are named after confluent's own hardwaremanagement.method
# values, so the file uses vocabulary that already exists rather than a second
# taxonomy. Fixtures aggregate them into roles: a test that works against any
# BMC asks for bmc_target, one that needs Redfish asks for redfish_target.
_TARGET_FIXTURES = {
    'redfish_target': ('redfish',),
    'ipmi_target': ('ipmi',),
    'bmc_target': ('redfish', 'ipmi'),
    'chassis_target': ('enclosure',),
    'cdu_target': ('cooltera',),
    'pdu_target': ('deltapdu', 'eatonpdu', 'enlogic', 'geist', 'raritan'),
    'switch_target': ('cnos', 'enos', 'nxos', 'srlinux'),
}

# Module-level state in configmanager that a test can reasonably disturb. The
# datastore is a module global rather than instance state, so fixtures have to
# snapshot and restore it or tests leak into each other.
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

# Of the above, the ones holding a mutable container. Saving a reference does
# not undo an in-place mutation, so these get a fresh empty container for the
# duration of the test and the original object back afterwards.
_CFM_MUTABLE_GLOBALS = ('_pendingchangesets',)

# Class attributes on ConfigManager with the same problem. Nothing in the
# current suite registers a watcher, but attribute-watch and node-collection
# notification tests would otherwise accumulate callbacks across tests.
_CFM_CLASS_MUTABLES = ('_attribwatchers', '_nodecollwatchers', '_notifierids')

_UNSET = object()

# Connected BMC clients, keyed by device name. See the redfish_command fixture
# for why these are reused rather than built per test.
_REDFISH_CLIENTS = {}


def pytest_addoption(parser):
    parser.addoption(
        '--hw-level', default='readonly', choices=_SAFETY_LEVELS,
        help='Most that any test may do to a device this run (default: '
             'readonly). A device also carries its own ceiling in the '
             'inventory, and the lower of the two applies.')


def _level_index(level):
    return _SAFETY_LEVELS.index(level)


def _device_ceiling(item):
    """The allow: of whichever device this test was parametrized over."""
    callspec = getattr(item, 'callspec', None)
    if callspec is None:
        return None
    for value in callspec.params.values():
        if isinstance(value, dict) and 'address' in value:
            return value.get('allow', _SAFETY_LEVELS[0])
    return None


def _safety_level(item):
    """The one safety marker on a test, or None if that is not the case."""
    declared = [name for name in _SAFETY_LEVELS if name in item.keywords]
    if len(declared) != 1:
        return None
    return declared[0]


def pytest_configure(config):
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

    # A hardware test that does not say what it may do is refused outright
    # rather than given a default. The failure this guards against is a
    # destructive test with a forgotten marker inheriting something permissive
    # and running when it should not, which is not a thing to discover from
    # the state of the machine afterwards.
    undeclared = sorted({item.nodeid.split('[')[0] for item in items
                         if 'hardware' in item.keywords
                         and _safety_level(item) is None})
    if undeclared:
        raise pytest.UsageError(
            'hardware tests must carry exactly one of {0}:\n  {1}'.format(
                ', '.join(_SAFETY_LEVELS), '\n  '.join(undeclared)))

    run_ceiling = config.getoption('hw_level')
    for item in items:
        if 'hardware' in item.keywords and not hardware_ready:
            item.add_marker(skip_hardware)
        if 'lab' in item.keywords and not lab_ready:
            item.add_marker(skip_lab)
        if 'hardware' not in item.keywords:
            continue

        # The device ceiling and the run ceiling both apply, and the lower
        # wins. "In use right now" is a property of the machine, not of the
        # run, so a device must stay protected even when the command line asks
        # for more.
        level = _safety_level(item)
        device_ceiling = _device_ceiling(item)
        if _level_index(level) > _level_index(run_ceiling):
            item.add_marker(pytest.mark.skip(
                reason='{0} test, run allows up to {1}: raise '
                       '--hw-level'.format(level, run_ceiling)))
        elif (device_ceiling is not None
                and _level_index(level) > _level_index(device_ceiling)):
            item.add_marker(pytest.mark.skip(
                reason='{0} test, device allows up to {1}: raise allow: in '
                       'the inventory'.format(level, device_ceiling)))


@pytest.fixture(autouse=True)
async def _asyncio_debug():
    """Run every test's event loop in debug mode.

    Debug mode reports slow callbacks, coroutines that were never scheduled
    and exceptions never retrieved from a task, all realistic failure modes in
    the console and plugin dispatch paths. set_debug also turns on coroutine
    origin tracking while the loop is running, so warnings carry the source
    location of the coroutine rather than just its name.

    Set on the loop rather than through PYTHONASYNCIODEBUG, which is a
    process-wide flag that libraries read for their own purposes. aiohttp
    captures it at import as aiohttp.helpers.DEBUG and builds its HTTP
    response parser with lax=not DEBUG, and strict parsing rejects a repeated
    singleton header. Real BMCs send them: an OpenBMC tested here returns ETag
    twice on the Redfish service root, so the variable turned a response
    production accepts into "400, Duplicate 'Etag' header found." Nothing
    consults loop.get_debug() for parsing, so setting it here gets the
    diagnostics without changing how any library treats the wire.

    Exporting PYTHONASYNCIODEBUG yourself reintroduces that, and the hardware
    tier will fail against firmware that is fine in production.
    """
    asyncio.get_running_loop().set_debug(True)


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
def _reset_noderange_cache():
    """Keep noderange.lastnoderange from leaking between tests.

    Every NodeRange construction rebinds this module global to the range it
    just evaluated, and ReverseNodeRange.noderange consults it: if the cached
    node set matches the one being abbreviated, it returns the cached range
    string outright and skips the abbreviation logic. A range built by one
    test would therefore be able to decide another test's abbreviation result.

    Cleared before each test as well as restored after, so a test that
    abbreviates always starts from a known-empty cache.
    """
    from confluent import noderange

    saved = noderange.lastnoderange
    noderange.lastnoderange = None
    yield
    noderange.lastnoderange = saved


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


def _hardware_target(varname):
    value = os.environ.get(varname)
    if not value:
        pytest.skip('needs {0}'.format(varname))
    return value


@functools.lru_cache(maxsize=None)
def _inventory():
    """The parsed equipment inventory, or an empty one."""
    path = os.environ.get(_INVENTORY_ENV)
    if not path:
        return {}
    # Imported here rather than at module scope so the rest of the suite does
    # not gain a hard dependency on PyYAML just to collect.
    import yaml

    with open(path) as invfile:
        return yaml.safe_load(invfile) or {}


def _targets(kind):
    """Every configured device of one kind, inventory first.

    Entries inherit from a top-level defaults: mapping, and carry the method
    they were listed under so a fixture serving several methods can dispatch.
    The single-device CONFLUENT_TEST_* variables still work and are appended
    as one more target, so a quick one-off run needs no file.
    """
    inventory = _inventory()
    defaults = inventory.get('defaults') or {}
    targets = []
    for entry in inventory.get(kind) or []:
        merged = dict(defaults)
        merged.update(entry)
        merged['method'] = kind
        allow = merged.setdefault('allow', _SAFETY_LEVELS[0])
        if allow not in _SAFETY_LEVELS:
            raise pytest.UsageError(
                "inventory device {0!r} has allow: {1!r}, expected one of "
                "{2}".format(merged.get('name', merged.get('address')), allow,
                             ', '.join(_SAFETY_LEVELS)))
        if merged.get('address') and merged.get('user'):
            targets.append(merged)
    if kind == 'redfish':
        address = os.environ.get('CONFLUENT_TEST_REDFISH_BMC')
        user = os.environ.get('CONFLUENT_TEST_REDFISH_USER')
        password = os.environ.get('CONFLUENT_TEST_REDFISH_PASSWORD')
        if address and user:
            targets.append({'name': address, 'address': address,
                            'user': user, 'password': password,
                            'method': 'redfish',
                            'allow': os.environ.get(
                                'CONFLUENT_TEST_REDFISH_ALLOW',
                                _SAFETY_LEVELS[0])})
    return targets


def _parametrize_targets(metafunc, kinds, argname):
    """Run the test once per configured device.

    Each device gets its own xdist_group, so with -n and --dist loadgroup all
    the tests for one machine land on one worker. That keeps concurrent load
    on a given controller to what a single client would produce, and it is the
    same mechanism that will give an OS deployment test exclusive use of a
    node.
    """
    targets = []
    for kind in kinds:
        targets.extend(_targets(kind))
    if not targets:
        metafunc.parametrize(argname, [pytest.param(
            None, id='none-configured',
            marks=pytest.mark.skip(
                reason='no {0} device configured: add one under {1} in the '
                       'inventory named by {2}'.format(
                           ' or '.join(kinds), '/'.join(kinds),
                           _INVENTORY_ENV)))])
        return
    metafunc.parametrize(argname, [
        pytest.param(target,
                     id=str(target.get('name', target['address'])),
                     marks=pytest.mark.xdist_group(
                         str(target.get('name', target['address']))))
        for target in targets])


def pytest_generate_tests(metafunc):
    for argname, kinds in _TARGET_FIXTURES.items():
        if argname in metafunc.fixturenames:
            _parametrize_targets(metafunc, kinds, argname)


def _split_port(address, default=443):
    """Split an optional :port off a target address.

    BMCs reached through a tunnel or a virtual Redfish service often listen
    somewhere other than 443, so the address variables accept host:port.
    Bracketed IPv6 is honoured, and a bare IPv6 literal is left alone rather
    than having its last group mistaken for a port.
    """
    if address.startswith('['):
        host, _, rest = address.partition(']')
        if rest.startswith(':'):
            return host[1:], int(rest[1:])
        return host[1:], default
    host, sep, port = address.rpartition(':')
    if sep and port.isdigit() and ':' not in host:
        return host, int(port)
    return address, default


@pytest.fixture
def redfish_bmc():
    """Address of a Redfish BMC to test against, or skip."""
    return _hardware_target('CONFLUENT_TEST_REDFISH_BMC')


@pytest.fixture
async def redfish_command(redfish_target):
    """A connected aiohmi Redfish client, one per configured BMC.

    Tests requesting this run once per device in the inventory, so a failure
    names the machine it came from. Addresses may carry a :port for a BMC
    reached through a tunnel.

    Credentials come from the inventory file or the environment and are never
    stored in the repository. Do not run this tier with --showlocals, which
    would print the password into a failure report.

    Building the client is itself a substantial read-only check: it fetches
    the service root, opens a session, selects an OEM handler and resolves the
    default system and manager URLs.

    The certificate is accepted unverified, since BMCs ship self-signed certs.
    That means these tests confirm the BMC answers, not that it is the BMC you
    think it is. Anything relying on identity needs a pinned fingerprint, the
    way confluent itself does it.

    One client per device per process, not per test. aiohmi has no Redfish
    logout, so every client that gets built leaves a session behind until the
    BMC times it out. Building one per test exhausted a real XCC part way
    through a run: later reads that open a secondary connection failed with an
    AttributeError on a None web connection, which looks like a confluent bug
    and is not one.

    Caching is safe because the client holds no persistent aiohttp session.
    Each request opens and closes its own, so the client is not bound to the
    event loop it was created in and can be reused by tests that each get a
    fresh one. Under xdist the cache is per worker, which with one device per
    worker still comes to one session per device.
    """
    from aiohmi.redfish.command import Command

    key = redfish_target.get('name', redfish_target['address'])
    if key not in _REDFISH_CLIENTS:
        host, port = _split_port(redfish_target['address'])
        _REDFISH_CLIENTS[key] = await Command.create(
            host, redfish_target['user'], redfish_target.get('password'),
            verifycallback=lambda cert: True, port=port)
    return _REDFISH_CLIENTS[key]


@pytest.fixture
def ipmi_bmc():
    """Address of an IPMI BMC to test against, or skip."""
    return _hardware_target('CONFLUENT_TEST_IPMI_BMC')


@pytest.fixture
def smm():
    """Address of an SMM to test against, or skip."""
    return _hardware_target('CONFLUENT_TEST_SMM')


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
    saved_class = {name: getattr(cfm.ConfigManager, name)
                   for name in _CFM_CLASS_MUTABLES}
    saved_cfgdir = cfm.ConfigManager._cfgdir

    # Everything after the snapshot runs inside the try, so a failure during
    # setup still restores. Otherwise an exception here would leave every
    # later test in the session running against corrupted globals.
    try:
        for name in _CFM_MUTABLE_GLOBALS:
            setattr(cfm, name, {})
        for name in _CFM_CLASS_MUTABLES:
            setattr(cfm.ConfigManager, name, {})

        cfm.ConfigManager._cfgdir = str(cfgdir)
        cfm.statelessmode = True
        cfm._cfgstore = None
        cfm.init(stateless=True)
        yield cfgdir
    finally:
        cfm.ConfigManager._cfgdir = saved_cfgdir
        for name, value in saved.items():
            setattr(cfm, name, value)
        for name, value in saved_class.items():
            setattr(cfm.ConfigManager, name, value)


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
    ``core.noderesources`` as needed. The plugin map and both resource trees
    are restored afterwards.

    The snapshot is taken before _init_core() runs, or the "original" state
    would just be a freshly built tree. _init_core() rebinds noderesources and
    nodegroupresources to new objects rather than mutating them, so the saved
    references stay intact while the test mutates the fresh ones. Neither
    global exists until the first _init_core() call, hence the sentinel.
    """
    from confluent import core

    saved_plugins = dict(core.pluginmap)
    saved_resources = {name: getattr(core, name, _UNSET)
                       for name in ('noderesources', 'nodegroupresources')}

    # Inside the try for the same reason as confluent_cfgdir: _init_core()
    # imports confluent.shellserver, which can fail on an environment missing
    # a dependency, and an unrestored resource tree would corrupt the session.
    try:
        core._init_core()
        core.pluginmap.clear()
        yield core.pluginmap
    finally:
        core.pluginmap.clear()
        core.pluginmap.update(saved_plugins)
        for name, value in saved_resources.items():
            if value is _UNSET:
                delattr(core, name)
            else:
                setattr(core, name, value)
