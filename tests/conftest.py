"""Shared fixtures for the confluent test suite.

Everything here exists so that individual test modules do not have to
re-implement path bootstrapping, datastore isolation or plugin stubbing. See
tests/README.md for the conventions.
"""

import asyncio
import configparser
import contextlib
import functools
import gc
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

import pytest
import pytest_asyncio


# The hardware tier is enabled by --run-hardware and nothing else, so that no
# environment left over in a shell can turn a plain pytest into a run against
# real machines. The collection hook below is the coarse gate; per-target
# precision comes from the target fixtures, so naming one BMC does not enable
# tests for equipment that is not there.
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

# The well-known IPMI RMCP port. Unlike Redfish, where a BMC behind a tunnel is
# the usual reason to see a port, an IPMI target commonly carries one because a
# simulator cannot have 623: it is privileged, and nothing here runs as root.
_IPMI_PORT = 623

# OpenIPMI's simulated BMC, which speaks RMCP+ from a described machine rather
# than from hardware. An inventory entry opts in with simulator: true and the
# port in its address is where it is asked to listen. tests/support/ipmisim.py
# writes its configuration and says what it is and is not evidence of.
_SIMULATOR = 'ipmi_sim'

# What it prints once the channel is bound. Waiting for this rather than
# sleeping is the difference between a tier that is slow and one that is flaky.
_SIMULATOR_READY = 'Opened UDP port'

# The Redfish counterpart of the simulator: a captured service replayed by
# DMTF's mockup server. An inventory entry opts in with mockup: <name>, and the
# port in its address is where that capture is served. Unlike ipmi_sim this is
# a container, because the server is published as one and nothing about it is
# worth installing on a developer's machine.
_MOCKUP_IMAGE = 'docker.io/dmtf/redfish-mockup-server:latest'
_MOCKUP_DIRECTORY = pathlib.Path(__file__).parent / 'support' / 'mockups'

# How long to wait for a replayed service to answer its root. Generous next to
# how long it takes, and still well inside the per-test timeout in addopts.
_MOCKUP_READY_TIMEOUT = 30


def pytest_addoption(parser):
    parser.addoption(
        '--run-hardware', action='store_true', default=False,
        help='Let the hardware tier run. An inventory alone does not: it says '
             'what the devices are, this says they may be touched.')
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


def _device_known_failures(item):
    """The known_failures mapping of the device this test was parametrized on.

    A simulator implements less than real firmware, and a given machine may
    have a defect someone has already looked at. Either way the test should
    still run and report, rather than being deleted or silently skipped, so
    these become xfail rather than skip: a listed test that starts passing is
    reported as an unexpected pass and the entry can go.
    """
    callspec = getattr(item, 'callspec', None)
    if callspec is None:
        return {}
    for value in callspec.params.values():
        if isinstance(value, dict) and 'address' in value:
            return value.get('known_failures') or {}
    return {}


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
    # Two keys, and the inventory is not one of them. Naming devices says what
    # they are; --run-hardware says they may be touched. Exporting the
    # inventory variable once, which is the natural thing to do, otherwise
    # turns every later plain pytest in that shell into a run against real
    # machines, and that is not something to find out afterwards.
    hardware_ready = config.getoption('run_hardware')
    lab_ready = any(os.environ.get(name) for name in _LAB_ENV)
    skip_hardware = pytest.mark.skip(
        reason='needs --run-hardware, and a device in the inventory named by '
               + _INVENTORY_ENV)
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

    # Refused here rather than left to be discovered, because the failure is
    # silent: _service_nodes keys by name, so a second device with the same one
    # replaces the first and stops being tested through the CLI, while the run
    # still reports a full pass. Listing one machine under two methods is a
    # reasonable thing to want; it just needs two names.
    duplicates = _duplicate_target_names()
    if duplicates:
        raise pytest.UsageError(
            'each inventory name must identify one device, and these do '
            'not:\n  {0}'.format('\n  '.join(
                '{0}, listed under {1}'.format(name, ' and '.join(kinds))
                for name, kinds in sorted(duplicates.items()))))

    run_ceiling = config.getoption('hw_level')
    for item in items:
        if 'hardware' in item.keywords and not hardware_ready:
            item.add_marker(skip_hardware)
        if 'lab' in item.keywords and not lab_ready:
            item.add_marker(skip_lab)
        if 'hardware' not in item.keywords:
            continue

        for pattern, reason in _device_known_failures(item).items():
            if pattern in item.nodeid:
                item.add_marker(pytest.mark.xfail(reason=reason,
                                                  strict=False))

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
def _asyncio_debug(request):
    """Put each test's loop in debug mode, whichever loop that is.

    Sync, and it asks for the async fixture below only when that applies.
    Requesting an async function-scoped fixture is what builds a
    function-scoped loop, and a test running on the session loop must not have
    one: aiohmi registers its socket readers against whichever loop is current
    when it opens a session, so a second loop appearing around the test costs
    every IPMI read a timeout instead of an answer.

    That was found the slow way. Replacing the fixture body with pass left the
    tier hanging exactly as before, which is what identified the loop itself,
    rather than anything the fixture did, as the problem.
    """
    marker = request.node.get_closest_marker('asyncio')
    if marker and marker.kwargs.get('loop_scope') == 'session':
        # _asyncio_debug_session has already done it for that loop.
        return
    request.getfixturevalue('_asyncio_debug_function')


@pytest.fixture
async def _asyncio_debug_function():
    """Run a function-scoped test loop in debug mode.

    Debug mode reports slow callbacks, coroutines that were never scheduled
    and exceptions never retrieved from a task, all realistic failure modes in
    the console and plugin dispatch paths. set_debug also turns on coroutine
    origin tracking while the loop is running, so warnings carry the source
    location of the coroutine rather than just its name.

    There are two of these, one per loop scope. Most of the suite gets a fresh
    loop per test; the IPMI tier shares one for the session, because an IPMI
    session cannot outlive the loop that opened it. Both need the same
    treatment, and a fixture can only ask for the loop of its own scope.

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


@pytest_asyncio.fixture(scope='session', loop_scope='session', autouse=True)
async def _asyncio_debug_session():
    """The same, for the loop the IPMI tier shares. See _asyncio_debug."""
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
        missing = [field for field in ('address', 'user')
                   if not merged.get(field)]
        if missing:
            # Refused rather than skipped. An entry with a typo used to be
            # dropped in silence, so the run reported a full pass over the
            # devices it did read and never mentioned the one it did not.
            raise pytest.UsageError(
                'inventory device {0!r} under {1} is missing {2}'.format(
                    merged.get('name', merged.get('address', '<unnamed>')),
                    kind, ' and '.join(missing)))
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


def _duplicate_target_names():
    """Names an inventory gives to more than one device, and where.

    A name is both the node the CLI tier defines and the id a failure is
    reported under, so it has to mean one device. Checked across every method
    rather than within one, since the same machine reached two ways is exactly
    when this is easy to write by accident.
    """
    kinds = sorted({kind for group in _TARGET_FIXTURES.values()
                    for kind in group})
    seen = {}
    duplicates = {}
    for kind in kinds:
        for target in _targets(kind):
            name = str(target.get('name', target['address']))
            if name in seen:
                duplicates.setdefault(name, [seen[name]]).append(kind)
            else:
                seen[name] = kind
    return duplicates


def _parametrize_targets(metafunc, kinds, argname):
    """Run the test once per configured device.

    Each device gets its own xdist_group, so with -n and --dist loadgroup all
    the tests for one machine land on one worker. That keeps concurrent load
    on a given controller to what a single client would produce, and it is the
    same mechanism that will give an OS deployment test exclusive use of a
    node.

    Parametrized at session scope so that a session-scoped fixture can be built
    per device. A function-scoped parameter cannot be requested by one, and the
    IPMI client has to be session-scoped: see the ipmi_command fixture.
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
                           _INVENTORY_ENV)))], scope='session')
        return
    metafunc.parametrize(argname, [
        pytest.param(target,
                     id=str(target.get('name', target['address'])),
                     marks=pytest.mark.xdist_group(
                         str(target.get('name', target['address']))))
        for target in targets], scope='session')


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


def _support(name):
    """Import a helper from tests/support, which is not a package.

    There are deliberately no __init__.py files under tests/, so nothing here
    can be imported by name. Loading by path keeps it that way rather than
    adding a third importable package beside the two confluent namespace
    packages.
    """
    path = pathlib.Path(__file__).parent / 'support' / '{0}.py'.format(name)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _port_is_taken(host, port):
    """Whether something already holds this UDP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


@pytest.fixture(scope='session')
def ipmi_simulator(tmp_path_factory):
    """Start a simulated BMC for any inventory entry that asks for one.

    Yields a callable taking a target. Entries without ``simulator: true`` pass
    through untouched, so one inventory can mix real BMCs with a simulated one
    and every fixture can call this without checking first.

    A simulator already listening on the port is left alone and used as it
    stands. That covers one started by hand, and it covers a second xdist
    worker arriving for the same device: only the process that started one ever
    stops it.

    Skips rather than fails when ipmi_sim is not installed, so that an
    inventory naming a simulator is still usable on a machine without it.
    """
    ipmisim = _support('ipmisim')
    started = []

    def ensure(target):
        if not (target or {}).get('simulator'):
            return
        if shutil.which(_SIMULATOR) is None:
            pytest.skip(
                'a simulated BMC needs {0}, from OpenIPMI\'s lanserv tools '
                '(package OpenIPMI-lanserv on Fedora and EL)'.format(
                    _SIMULATOR))

        host, port = _split_port(target['address'], _IPMI_PORT)
        if _port_is_taken(host, port):
            return

        directory = tmp_path_factory.mktemp('ipmisim')
        state = directory / 'state'
        state.mkdir()
        lanconf, emulation = ipmisim.write_configuration(
            directory, port, target['user'], target.get('password'))

        simulator = subprocess.Popen(
            [_SIMULATOR, '-c', str(lanconf), '-f', str(emulation),
             '-n', '-s', str(state)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        running = _Drained(simulator, _SIMULATOR)
        started.append(running)
        _wait_for_simulator(running, port)

    yield ensure

    for running in started:
        running.process.terminate()
        try:
            running.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            running.process.kill()
            running.process.wait(timeout=10)
        running.close()


@functools.lru_cache(maxsize=None)
def _container_runtime():
    """podman for preference, docker if that is what is installed."""
    for runtime in ('podman', 'docker'):
        if shutil.which(runtime):
            return runtime
    return None


def _mockup_capture(name):
    """Where a ``mockup:`` value resolves to on disk.

    A bare name is one of the bundles vendored in tests/support/mockups.
    Anything carrying a separator is taken as a path, so an inventory kept
    outside the repository can point at a capture of a real machine, which
    holds that machine's identity and does not belong in one.
    """
    path = pathlib.Path(name).expanduser()
    if len(path.parts) == 1:
        path = _MOCKUP_DIRECTORY / name
    return path


def _mockup_answers(port):
    """Whether a Redfish service root is already being served on this port."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(
                'https://127.0.0.1:{0}/redfish/v1'.format(port),
                context=context, timeout=2) as answer:
            return answer.status == 200
    except Exception:  # anything at all means not ready
        # Connection refused while the container comes up, a handshake caught
        # mid-flight, or something else entirely on the port. None of them are
        # worth telling apart here: the caller either waits or gives up.
        return False


@functools.lru_cache(maxsize=None)
def _mockup_image(runtime):
    """Make sure the replay server image is present, or skip.

    Pulled explicitly rather than left to ``run``, so that "no image and no
    network" is a skip while a container that fails to start afterwards stays a
    failure. The first pull is most of 200MB and can outlast the per-test
    timeout in addopts, so continuous integration wants the image pulled before
    the run rather than discovered here.
    """
    present = subprocess.run([runtime, 'image', 'inspect', _MOCKUP_IMAGE],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    if present.returncode == 0:
        return
    pulled = subprocess.run([runtime, 'pull', _MOCKUP_IMAGE],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    if pulled.returncode:
        pytest.skip('could not obtain {0}: {1}'.format(
            _MOCKUP_IMAGE, pulled.stdout.strip()))


def _mockup_certificate(directory):
    """A throwaway self signed pair for the replay servers to present.

    confluent speaks https only and this tier does not verify, so any pair
    does. Generated per run rather than committed: a private key in a
    repository is something a scanner finds and reports, however inert it is.
    """
    if shutil.which('openssl') is None:
        pytest.skip('a replayed Redfish service needs openssl to generate a '
                    'certificate for it')
    subprocess.run(
        ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
         '-keyout', str(directory / 'key.pem'),
         '-out', str(directory / 'cert.pem'),
         '-days', '365', '-subj', '/CN=localhost'],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return directory


def _start_mockup(runtime, container, capture, host, port, certificates):
    """Serve one capture, replacing anything stale under the same name."""
    arguments = ['-D', '/mockup', '-s', '-p', '8000',
                 # The server binds its own loopback by default, which a
                 # published port cannot reach from outside the container.
                 '-H', '0.0.0.0',
                 '--cert', '/certs/cert.pem', '--key', '/certs/key.pem']
    if (capture / 'index.json').exists():
        # Short form: the DMTF bundles leave the /redfish/v1 prefix out of
        # their layout and a capture taken from a live service does not. Which
        # one this is can be read off the tree, so it is, rather than being one
        # more thing an inventory has to get right.
        arguments.append('-S')

    subprocess.run([runtime, 'rm', '-f', container],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = subprocess.run(
        [runtime, 'run', '-d', '--name', container,
         '-p', '{0}:{1}:8000'.format(host, port),
         '--security-opt', 'label=disable',
         '-v', '{0}:/mockup:ro'.format(capture),
         '-v', '{0}:/certs:ro'.format(certificates),
         _MOCKUP_IMAGE] + arguments,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if started.returncode:
        pytest.fail('could not serve the {0} mockup:\n{1}'.format(
            capture.name, started.stdout.strip()))


def _wait_for_mockup(runtime, container, port):
    """Block until the replayed service answers its root, or say why not."""
    deadline = time.monotonic() + _MOCKUP_READY_TIMEOUT
    while time.monotonic() < deadline:
        if _mockup_answers(port):
            return
        time.sleep(0.25)
    logs = subprocess.run([runtime, 'logs', container], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    pytest.fail('the mockup on port {0} never answered:\n{1}'.format(
        port, logs.stdout.strip()))


@pytest.fixture(scope='session')
def redfish_mockups(tmp_path_factory):
    """Serve the captures any inventory entry asks for, and stop them after.

    The Redfish counterpart of ipmi_simulator, and the same contract: yields a
    callable taking a target, entries without a ``mockup:`` pass through
    untouched, and a service already answering on the port is used as it stands
    rather than replaced. That covers one started by hand and a second xdist
    worker arriving for the same device.

    Skips rather than fails where the machine cannot serve one at all, so an
    inventory naming a capture is still usable without a container runtime.
    """
    started = []
    certificates = []

    def ensure(target):
        capture_name = (target or {}).get('mockup')
        if not capture_name:
            return
        runtime = _container_runtime()
        if runtime is None:
            pytest.skip('a replayed Redfish service needs podman or docker')

        capture = _mockup_capture(capture_name)
        if not capture.is_dir():
            pytest.fail('mockup {0!r} is not a directory: {1}'.format(
                capture_name, capture))

        host, port = _split_port(target['address'])
        if _mockup_answers(port):
            return

        _mockup_image(runtime)
        if not certificates:
            certificates.append(
                _mockup_certificate(tmp_path_factory.mktemp('mockupcerts')))

        container = 'confluent-test-mockup-{0}'.format(port)
        _start_mockup(runtime, container, capture, host, port, certificates[0])
        started.append((runtime, container))
        _wait_for_mockup(runtime, container, port)

    yield ensure

    for runtime, container in started:
        subprocess.run([runtime, 'rm', '-f', container],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for_simulator(running, port):
    """Block until the simulator says it has the channel, or explain why not."""
    deadline = time.monotonic() + 30
    while not running.saw(_SIMULATOR_READY):
        if running.process.poll() is not None:
            pytest.fail('{0} exited during startup:\n{1}'.format(
                _SIMULATOR, running.output()))
        if time.monotonic() > deadline:
            pytest.fail('{0} never bound port {1}:\n{2}'.format(
                _SIMULATOR, port, running.output()))
        time.sleep(0.05)


async def _redfish_client(target):
    """A connected Redfish client, reused across the tests for one device."""
    from aiohmi.redfish.command import Command

    key = target.get('name', target['address'])
    if key not in _REDFISH_CLIENTS:
        host, port = _split_port(target['address'])
        _REDFISH_CLIENTS[key] = await Command.create(
            host, target['user'], target.get('password'),
            verifycallback=lambda cert: True, port=port)
    return _REDFISH_CLIENTS[key]


@contextlib.asynccontextmanager
async def _ipmi_client(target):
    """A connected IPMI client, closed again on the way out.

    Built fresh per test, unlike the Redfish one, and for the opposite reason.
    An IPMI session is a live UDP conversation with keepalives running on the
    event loop that opened it, and every test here gets its own loop, so a
    cached client would be talking on a loop that has since closed.

    What makes one per test affordable is that IPMI can hang up. Closing the
    session in teardown keeps a BMC's session table from filling the way it
    does over Redfish, where aiohmi has no logout at all and the Redfish
    fixture has to cache to compensate.
    """
    from aiohmi.ipmi.command import Command

    host, port = _split_port(target['address'], _IPMI_PORT)
    command = await Command.create(
        host, target['user'], target.get('password'), port=port)
    try:
        yield command
    finally:
        await command.ipmi_session.logout()


@pytest_asyncio.fixture(scope='session', loop_scope='session')
async def ipmi_command(ipmi_target, ipmi_simulator):
    """A connected aiohmi IPMI client, one per configured device.

    The IPMI counterpart of redfish_command. Reaching this point already proves
    an RMCP+ session was negotiated, which is most of what can go wrong before
    a command is ever sent.

    Built once for the session and on a loop that lasts as long, which the
    Redfish fixture arrives at from the other direction. There it is a choice,
    to avoid leaving sessions behind. Here it is forced: an IPMI session is a
    UDP conversation whose sockets and pending waiters live in the loop that
    opened it, and aiohmi holds them in module state shared by every session in
    the process. Used from a second loop it does not fail, it stops answering,
    which costs a per-test timeout each time. Verified by trying it.

    A test using this must therefore run on the same loop, which is what the
    asyncio(loop_scope='session') marker on the IPMI modules is for. Without it
    the tests hang rather than fail, so do not drop it from a new file.

    Credentials come from the inventory file or the environment, exactly as
    they do for Redfish, and the same warning applies: do not run this tier
    with --showlocals.
    """
    ipmi_simulator(ipmi_target)
    async with _ipmi_client(ipmi_target) as command:
        yield command


@pytest_asyncio.fixture(scope='session', loop_scope='session')
async def bmc_command(bmc_target, ipmi_simulator, redfish_mockups):
    """A connected client for whichever transport this device speaks.

    For tests that are about what confluent asks of a BMC rather than about how
    it asks. Both clients present the same read methods, so a test written
    against this runs over Redfish and IPMI without knowing which it has, and
    an inventory listing both kinds reports one result per device per test.

    Which methods a given client actually implements differs, and that is
    discovered rather than declared: a client without one is a skip, not a
    failure.

    Session-scoped for the reason given in ipmi_command, which applies whenever
    the device on the other end turns out to speak IPMI.
    """
    if bmc_target['method'] == 'ipmi':
        ipmi_simulator(bmc_target)
        async with _ipmi_client(bmc_target) as command:
            yield command
    else:
        redfish_mockups(bmc_target)
        yield await _redfish_client(bmc_target)


@pytest.fixture
async def redfish_command(redfish_target, redfish_mockups):
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
    redfish_mockups(redfish_target)
    return await _redfish_client(redfish_target)


# What a service or a tool may say that means it found something. Matched
# against everything either writes, and against the service's trace log, which
# is where sockapi and the plugins put a traceback they caught.
#
# Deliberately not here: asyncio's slow callback warning. Debug mode reports
# every callback over 100ms, and dispatch to a device legitimately takes longer
# than that, so it says nothing about correctness. It is still shown with the
# rest of the output when something else fails.
_COMPLAINTS = (
    'Traceback (most recent call last)',
    'Task exception was never retrieved',
    'was never awaited',
    'Future exception was never retrieved',
)


class _Drained:
    """A long-running process whose output a thread reads continuously.

    Nothing else reads these pipes once startup is over, and a process whose
    pipe fills stops dead at the write. A traceback is exactly what would fill
    one, so without this the failure mode is a hang arriving precisely when the
    process has most to report.
    """

    def __init__(self, process, what):
        self.process = process
        self._what = what
        self._said = []
        self._failure = None
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self):
        try:
            for line in self.process.stdout:
                self._said.append(line)
        except Exception as caught:
            # Recorded rather than raised: an exception in a thread is
            # discarded, and this one has to be answerable for. See check().
            self._failure = caught

    def check(self):
        """Refuse to carry on with a reader that has stopped.

        The reader ending while the process runs is not a small problem. It
        collects nothing further, so every check built on this output keeps
        reporting a clean process for the rest of the session: a guard that
        fails silently is worse than no guard, because the run still says yes.
        """
        if self.process.poll() is not None or self._reader.is_alive():
            return
        pytest.fail(
            'nothing is reading {0} any more, so nothing it says from here on '
            'would be seen: {1}'.format(
                self._what, self._failure or 'the reader ended by itself'))

    def saw(self, text):
        return any(text in line for line in self._said)

    def mark(self):
        return len(self._said)

    def since(self, mark):
        return ''.join(self._said[mark:])

    def output(self):
        return ''.join(self._said)

    def close(self):
        self._reader.join(timeout=5)
        self.process.stdout.close()


class _Service(_Drained):
    """A running test service: what it has said, and what it has logged."""

    def __init__(self, process, logdirectory):
        super().__init__(process, 'the confluent service')
        self.socketpath = None
        self.logdirectory = logdirectory

    def mark(self):
        """Where the output and the trace log have reached."""
        return super().mark(), self._tracelog()

    def since(self, marker):
        """Everything said, and logged, since a mark."""
        said = super().since(marker[0])
        logged = self._tracelog()[len(marker[1]):]
        return '\n'.join(part for part in (said, logged) if part.strip())

    def complaints(self, marker):
        """Whichever complaints turned up since a mark, if any."""
        self.check()
        fresh = self.since(marker)
        found = [text for text in _COMPLAINTS if text in fresh]
        # A trace log that grew at all counts, whatever it holds: nothing
        # writes to it but a handler reporting something it caught.
        if len(self._tracelog()) > len(marker[1]) and not found:
            found.append('an entry in the trace log')
        return found, fresh

    def _tracelog(self):
        try:
            return (self.logdirectory / 'trace').read_text(errors='replace')
        except OSError:  # not written to yet, which is the healthy case
            return ''


def _service_nodes():
    """The inventory as confluent node definitions, keyed by device name.

    An address may carry a port for either method: both plugins read one off
    hardwaremanagement.manager. The ipmi plugin only learned to as of "Read a
    port off the manager address over ipmi too", so on a base without that
    commit every IPMI device here is reached on 623 whatever its address says,
    and a device that is not there fails rather than skipping.
    """
    nodes = {}
    for kind in ('redfish', 'ipmi'):
        for target in _targets(kind):
            nodes[str(target.get('name', target['address']))] = {
                'hardwaremanagement.manager': target['address'],
                'hardwaremanagement.method': kind,
                'secret.hardwaremanagementuser': target['user'],
                'secret.hardwaremanagementpassword': target.get('password'),
            }
    return nodes


@pytest.fixture(scope='session')
def confluent_service(tmp_path_factory):
    """A confluent service on a temp socket, with the inventory as its nodes.

    Started as a subprocess rather than in this one. A service here would
    share configmanager's module globals with the fixtures that reset them
    between tests, and its socket would have to live in whichever event loop
    happened to be current. A separate process shares nothing, needs no loop
    scope juggling, and is closer to how confluent actually runs.

    No privileges are needed. The socket is trusted by peer uid: sockapi
    grants a connection from the uid the service runs as, which is the same
    user running the tests, so no password, PAM or certificate is involved.

    Yields a _Service, or skips when no device is configured.
    """
    nodes = _service_nodes()
    if not nodes:
        pytest.skip('no device configured: nothing for a service to manage')

    directory = tmp_path_factory.mktemp('service')
    socketpath = str(directory / 'api.sock')
    # The service puts its datastore and its logs under here, so both are
    # pytest's to clean up rather than leaving directories in /tmp per run.
    logdirectory = directory / 'log'
    support = pathlib.Path(__file__).parent / 'support' / 'confluentservice.py'
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        str(pathlib.Path(__file__).parents[1] / component)
        for component in ('confluent_server', 'confluent_client'))

    # The node definitions carry BMC passwords, so they go over stdin rather
    # than argv, which any local user can read out of ps or /proc for the life
    # of the process. Same reason the inventory itself is a file.
    service = subprocess.Popen(
        [sys.executable, str(support), socketpath, str(directory)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env, text=True)
    service.stdin.write(json.dumps(nodes))
    service.stdin.close()
    running = _Service(service, logdirectory)
    try:
        deadline = time.monotonic() + 60
        while not running.saw('READY'):
            if service.poll() is not None:
                pytest.fail('service exited during startup:\n{0}'.format(
                    running.output()))
            if time.monotonic() > deadline:
                pytest.fail('service never reported ready:\n{0}'.format(
                    running.output()))
            time.sleep(0.05)
        running.socketpath = socketpath
        yield running
    finally:
        service.terminate()
        try:
            service.wait(timeout=10)
        except subprocess.TimeoutExpired:
            service.kill()
            service.wait(timeout=10)
        running.close()


@pytest.fixture
def service_nodes(confluent_service, ipmi_simulator, redfish_mockups):
    """Every node the test service knows, for tests that need them together.

    Deliberately not parametrized per device: the point of asking for all of
    them at once is to drive one request across several, which is the only
    place in the suite that produces genuinely concurrent dispatch.

    That means every stand-in has to be up, not just the one device a test was
    parametrized on, so this starts them all.
    """
    for kind in ('redfish', 'ipmi'):
        for target in _targets(kind):
            ipmi_simulator(target)
            redfish_mockups(target)
    return sorted(_service_nodes())


@pytest.fixture
def service_node(bmc_target, ipmi_simulator, redfish_mockups):
    """The name the test service knows this device by.

    Derived from bmc_target so the CLI tests inherit the same parametrization,
    xdist grouping and per-device safety ceiling as everything else.

    Starts whatever stands in for a simulated device, so the CLI tier reaches
    one the same way it reaches a real BMC.
    """
    ipmi_simulator(bmc_target)
    redfish_mockups(bmc_target)
    return str(bmc_target.get('name', bmc_target['address']))


@pytest.fixture
def run_cli(confluent_service):
    """Run a confluent CLI tool against the test service.

    Returns (returncode, output). The tools are invoked as subprocesses from
    the source tree, so argument parsing, the socket protocol and output
    formatting are all the real ones rather than something reassembled.
    """
    root = pathlib.Path(__file__).parents[1]
    env = dict(os.environ)
    env['CONFLUENT_HOST'] = confluent_service.socketpath
    env['PYTHONPATH'] = os.pathsep.join(
        str(root / component)
        for component in ('confluent_server', 'confluent_client'))
    # Several of the tools run their own event loop through
    # confluent.asynclient, and without this they are the last thing in the
    # suite with no asyncio diagnostics at all. The variable is safe to set
    # here, where the in-process tests must not have it: aiohttp derives its
    # own DEBUG from it and parses the wire differently, and the tools do not
    # import aiohttp. Checked rather than assumed. It reaches only these
    # subprocesses, never the run itself, and "0" would not disable it since
    # asyncio tests bool() of the string.
    env['PYTHONASYNCIODEBUG'] = '1'

    # Where the service's output had got to before this test ran, so whatever
    # it says next is attributed here rather than to some later test.
    marker = confluent_service.mark()

    def invoke(tool, *arguments, timeout=120):
        completed = subprocess.run(
            [sys.executable, str(root / 'confluent_client' / 'bin' / tool)]
            + [str(argument) for argument in arguments],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, timeout=timeout)
        output = completed.stdout.strip()
        # Echoed to the test's own stdout so a run can be read as a transcript
        # of what the tools actually printed, which is the whole subject of
        # this tier. pytest captures it, so it costs a passing run nothing and
        # is shown on failure; -rA shows it for passing tests too, and -s live.
        print('$ {0} {1}\n{2}'.format(
            tool, ' '.join(str(argument) for argument in arguments),
            output or '(no output)'))
        # Refused here rather than left to each test. A tool that dies with a
        # traceback exits non-zero and prints something, which is exactly what
        # a device refusing an operation looks like from the outside, so a
        # caller checking the return code reads a crash as a polite decline
        # and skips. The tools are the subject of this tier: a traceback out
        # of one is never an acceptable answer, whatever the exit code says.
        # The rest of the list is what debug mode below makes visible.
        found = [text for text in _COMPLAINTS if text in output]
        if found:
            pytest.fail('{0} reported {1} rather than answering:\n{2}'.format(
                tool, ', '.join(found), output))
        return completed.returncode, output

    yield invoke

    # A service that logged a traceback found something, whether or not the
    # client was told. Plenty never reaches one: a failure in a background
    # task, or one after the response went out. Those are the defects this
    # tier exists to catch and the only place they surface is here.
    found, said = confluent_service.complaints(marker)
    if found:
        pytest.fail('the service reported {0} while this test ran:\n{1}'.format(
            ', '.join(found), said))


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
