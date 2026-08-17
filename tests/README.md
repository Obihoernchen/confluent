# confluent tests

pytest, with `pytest-asyncio` in auto mode. Configuration lives in `pytest.ini` and `.coveragerc` at the repository
root, alongside the existing `ruff.toml` and `pyrefly.toml`.

## Running

```sh
python3 -m pytest
```

That runs the unit and integration tiers and never contacts hardware, needs root, or touches system configuration.

```sh
python3 -m pytest -m integration        # only the integration tier
python3 -m pytest -m "not integration"  # only fast unit tests
python3 -m pytest --cov --cov-branch    # with coverage
```

The suite is fast enough to run on every change. Coverage starts low, which is expected: this is a framework with
seed tests, not a coverage push. Record a baseline with the command above and work upward from it rather than
picking a target percentage.

Install the tooling with `pip install -r requirements-test.txt`. A full run additionally needs confluent's own
runtime dependencies importable: `pyparsing`, `cryptography`, `aiohttp`, `msgpack`, `pyyaml`, `lxml`, `pysnmp`,
`asyncssh`, `libarchive-c`, `python-dateutil`, `webauthn`, plus `legacycrypt` on Python 3.13 and newer (`import
crypt` is unconditional in `configmanager.py` and `selfservice.py`, and `crypt` left the stdlib in 3.13). The
guarded imports (`psutil`, `netifaces`, `EfiCompressor`, `libvirt`, `confluent.pam`) are optional.

There is no tox or nox yet. `pytest.ini` sets `pythonpath = confluent_server confluent_client`, so pytest runs
against the working tree with no install step.

## Layout

```
tests/
  conftest.py     shared fixtures, marker gating
  unit/           pure logic, no I/O
    server/       confluent_server/confluent
    client/       confluent_client/confluent
    aiohmi/       confluent_server/aiohmi
  integration/    real ConfigManager, plugin dispatch, sockets
  hardware/       live BMCs and the deployment lab, deselected by default
```

There are deliberately no `__init__.py` files here: adding them would create a third importable package name
alongside the two `confluent` namespace packages.

### Naming test files

`test_<transport>_<subsystem>.py`, for example `test_redfish_firmware.py`. The transport comes first because the
same subsystem is tested over more than one (`test_ipmi_system.py` beside `test_redfish_system.py`).

Where a test is genuinely the same question over either transport, the role name `bmc` takes the transport's
place: `test_bmc_reads.py` asks for `bmc_command` and runs against whatever the inventory holds. Reach for that
only when the reads involved mean the same thing on both. Identity does not, which is why the system files stay
apart: Redfish answers it from a system resource an OEM handler picked, IPMI from a FRU inventory area.

**Do not put the safety level in the filename.** It already lives in the marker, which is what actually gates the
test, and `-m readonly` selects on it. A filename that says `readonly` is a second copy of that fact which goes
stale the day the file gains a test at another level. Organise by subsystem instead: subsystems are stable, and
when a BMC misbehaves you want "the sensor tests", not "the read-only tests".

Two shapes are worth keeping distinct:

- **A sweep**, parametrized over many operations, asserting one property that holds for all of them.
  `test_bmc_reads.py` is this: every read either answers or refuses with a reason. Parametrizing gives one
  result line per operation per device, so the pass and refuse pattern across a fleet is readable straight from
  the summary, without a hand-written report.
- **Subsystem files** for invariants specific to one area, where the assertion is about meaning rather than shape.
  `test_redfish_firmware.py` is this: a category must filter rather than return everything, a version must not
  carry a status bit as a digit.

Reach for the sweep when the same question is being asked of many operations, and a subsystem file when a
particular answer has to be checked.

## Markers

**The hardware tier needs two keys, and an inventory is only one of them.** `CONFLUENT_TEST_HARDWARE` says what the
devices are; `--run-hardware` says they may be touched. Without the flag every hardware test skips, whatever the
environment holds. That is on purpose: exporting the inventory variable once in a shell is the natural thing to do,
and without a second key every later plain `pytest` in that shell would run against real machines.


| marker | what it means | how to enable |
|---|---|---|
| *(none)* | pure unit test, always runs | |
| `integration` | temp datastore, sockets, subprocesses | always runs |
| `hardware` | real BMC or running daemon | `--run-hardware`, plus a device in `CONFLUENT_TEST_HARDWARE` |
| `lab` | a provisioned deployment lab (real PXE/BMC infrastructure) | `--run-lab` |

Every `hardware` test additionally carries one **safety level**, described below.

Marked tests are collected and reported as skipped, not deselected, so a run always says what it did not do.
Use `-m hardware` to select only that tier, which does deselect everything else.

The marker plus `--run-hardware` is the coarse gate. Per-target precision comes from the `*_target` fixtures: a
test asking for `ipmi_target` is skipped when the inventory names no IPMI device, so configuring one BMC does not
enable tests for equipment that is not attached.

**Skipping is the right answer locally and the wrong one in CI.** A run with no device, a missing `ipmi_sim` or no
container runtime skips everything and exits 0, which reads as a pass. `--require-target=<method>`, repeatable,
fails the run unless a test actually reached a device of that method:

```sh
CONFLUENT_TEST_HARDWARE=tests/support/inventory-ipmisim.yaml \
python3 -m pytest -m hardware --run-hardware --require-target=ipmi
```

It counts test bodies that ran, not devices the inventory names, so a device configured but never reached fails
rather than skips. That is the difference between a CI job that proves something and one that is merely green.

A section name that is not a `hardwaremanagement.method` is refused at collection for the same reason: `redfih:`
instead of `redfish:` described no devices at all and ended in a green run of nothing but skips.

### Safety levels

Hardware may be in use, so what a test is allowed to do to a device is controlled explicitly. Each level includes
everything below it:

| level | writes? | interrupts service? | recovery | examples |
|---|---|---|---|---|
| `readonly` | no | no | n/a | power state, inventory, sensors, event log |
| `reversible` | yes | no | the test restores it | create then delete a user, set then restore a boot override |
| `disruptive` | yes | yes | automatic | power cycle, BMC reset, port flap, outlet cycle |
| `destructive` | yes | yes | may need a human | firmware flash, factory reset, RAID init, OS deployment |

The split between the last two is deliberate. A power cycle and a firmware flash are not the same event, and a
shared machine may reasonably permit the first while forbidding the second.

**Two ceilings apply, and the lower one wins:**

- the run, via `--hw-level=<level>`, defaulting to `readonly`
- the device, via `allow:` in its inventory entry, also defaulting to `readonly`

"In use right now" is a property of the machine rather than of the run, so a device stays protected even when the
command line asks for more. A skipped test says which of the two gates stopped it.

Every `hardware` test must carry **exactly one** safety marker. A test with none, or with more than one, aborts
collection rather than being given a default: the failure worth preventing is a destructive test with a forgotten
marker inheriting something permissive and running when it should not.

A `reversible` test owes the device a restore. Capture the prior state first, restore it in a fixture teardown so
it happens even when the test fails, and keep an explicit list of calls the test will never make.

### The inventory

Point `CONFLUENT_TEST_HARDWARE` at a YAML file, kept outside the repository and readable only by you:

```yaml
defaults:                      # inherited by every entry below
  user: admin
  allow: readonly

redfish:                       # sections are named after
  - name: xcc-lab1             # hardwaremanagement.method values
    address: 192.0.2.10        # a :port is allowed, for a BMC behind a tunnel
    password: ...
    allow: reversible

enclosure:                     # SMM
  - name: smm-rack4
    address: 192.0.2.40
    password: ...
    bays: [1, 2]               # per-kind: bays a test may touch

geist:                         # one of five PDU backends
  - name: pdu-rack4
    address: 192.0.2.60
    password: ...
    outlets: [7, 8]            # per-kind: outlets a test may cycle
    allow: disruptive

cnos:                          # one of four switch backends
  - name: sw-lab1
    address: 192.0.2.80
    password: ...
    ports: [Ethernet1/1]       # per-kind: ports a test may flap
```

Sections are named after confluent's own `hardwaremanagement.method` values, so the file uses vocabulary that
already exists rather than a second taxonomy. Recognised sections are `redfish`, `ipmi`, `enclosure`, `cooltera`,
the PDU backends (`deltapdu`, `eatonpdu`, `enlogic`, `geist`, `raritan`) and the switch backends (`cnos`, `enos`,
`nxos`, `srlinux`).

Per-kind keys such as `bays`, `outlets` and `ports` are both configuration and fencing: a test may only touch what
is listed.

Fixtures aggregate methods into roles, so a test asks for the broadest one it genuinely works against:

An entry may also carry `simulator: true`, which asks for a simulated device to be started on the port in its
address rather than a real one being contacted. Only `ipmi` supports it today. See "Testing without hardware".

| fixture | methods |
|---|---|
| `redfish_target`, `ipmi_target` | that one method |
| `bmc_target` | `redfish`, `ipmi` |
| `chassis_target` | `enclosure` |
| `cdu_target` | `cooltera` |
| `pdu_target` | the five PDU backends |
| `switch_target` | the four switch backends |

Tests run once per matching device and the test ID names the machine, so a failure reads
`test_boot_device_is_reported[openbmc-artemis]`. Credentials in a file also stay out of the command line, where
`ps` would expose them to any local user for the length of the run.

**The inventory is the only way to name a device**, and `CONFLUENT_TEST_HARDWARE` is the only variable the suite
reads. Single-device variables used to stand in for a small inventory, but they put a BMC password in the
environment, where any local user can read it out of `/proc` and every subprocess the tier starts inherits it. That
is the same exposure the service's node definitions were moved off the command line to avoid, and a file with mode
600 does the job without it.

### Testing through the CLI

`tests/hardware/test_cli_readonly.py` runs the actual `node*` commands against a real device. Each one reaches
the BMC through argument parsing, the socket protocol, a confluent service, resource dispatch, a hardware
management plugin and the protocol library, so everything below is exercised as a side effect of asserting on what
the user is shown. That is the layer where a good answer turns into "Unexpected Error", and no lower test sees it.

The `confluent_service` fixture starts a service on a temp socket with the inventory as its nodes, and `run_cli`
invokes the tools from the source tree against it. Two things make this cheap:

- **No privileges.** `sockapi` grants a connection whose peer uid matches the uid the service runs as, so no
  password, PAM or certificate is involved. That is also why this tier avoids the process pool the HTTP path needs
  for authentication.
- **A separate process.** A service in the test process would share configmanager's module globals with the
  fixtures that reset them between tests, and its socket would have to live in whichever event loop happened to be
  current. A subprocess shares nothing, needs no loop scope juggling, and is closer to how confluent runs.

`tests/support/confluentservice.py` is that subprocess. It is not a test module.

**A traceback logged by the service fails the test that caused it.** Its output is drained by a thread and its
trace log is read, and `run_cli` compares both against where they stood before the test ran. That matters because
plenty of what goes wrong in a service never reaches the client at all: a failure in a background task, or one
after the response has gone out. Those are the defects this tier exists to find, and nothing else in the suite is
watching for them. It arrives as an error rather than a failure, since the check runs in teardown, and the
service's own words are attached to it.

Asyncio's slow callback warning is deliberately not treated as a complaint: debug mode reports every callback over
100ms and dispatch to a device legitimately takes longer, so it says nothing about correctness. It is still shown
along with everything else when something does fail.

The service runs with `asyncio` debug on, for the same reasons the in-process tests do. It is the one component in
the suite that dispatches the way production does, so an unretrieved task exception or an unawaited coroutine there
is worth more than anywhere else, and both are silent without it.

Draining that output is also what stops the tier hanging. Nothing read the pipe once the service was up, and a
process whose pipe fills stops dead at the write, so the old failure mode was a hang arriving exactly when the
service had most to say. `ipmi_sim` is drained the same way and for the same reason.

**A traceback from a CLI tool fails the test whatever its exit code says.** A tool that dies exits non-zero and
prints something, which from the outside is indistinguishable from a device refusing an operation, so a test
checking the return code reads a crash as a polite decline and skips. `run_cli` refuses one centrally, since the
tools are the subject of this tier and no test should have to remember. The same check covers an unawaited
coroutine and an unretrieved task exception.

The tools run with `PYTHONASYNCIODEBUG` set, since several of them drive their own event loop through
`confluent.asynclient`. This is the one place that variable is safe: `tests/conftest.py` explains at length why the
in-process tests must not have it, and the reason is aiohttp, which the client tools do not import. It reaches
only those subprocesses and never the run itself.

**A reader thread that stops is itself a failure.** If one dies, it collects nothing further and every check built
on that output would keep reporting a clean process for the rest of the session. A guard that fails silently is
worse than none, because the run still says yes, so the checks confirm their own reader is alive before trusting
what it did not see.

`run_cli` echoes each invocation and its output to the test's own stdout, so a run reads as a transcript of what
the tools actually printed. pytest captures it: it costs a passing run nothing, appears automatically under
"Captured stdout call" when a test fails, and `-rA` shows it for passing tests too. `-s` shows it live.

Sensors, event logs and licences have no output shape worth pinning, so they are held to the contract the protocol
sweep uses one layer down, plus one it cannot see: a command that exits zero must not have printed an error while
doing it. A zero exit carrying an error is worse than a failure, because `nodesensors n1 && next-step` then runs
next-step. That check found `nodesensors` doing exactly this, recorded as a known failure rather than worked
around.

Assertions are about output shape rather than values, since power state, firmware versions and inventory differ
per machine. A device may refuse a command it does not support, provided it explains itself: an unsupported
identify light is a skip, an unexplained failure is a failure. That is the same contract the protocol-level sweep
applies one layer down.

### Testing without hardware

The tier does not care whether a device is real. Point `CONFLUENT_TEST_HARDWARE` at an inventory describing a
replayed one and the same tests run, which is what makes this usable where no hardware is attached, continuous
integration included. There is one committed inventory per transport:

```sh
CONFLUENT_TEST_HARDWARE=tests/support/inventory-ipmisim.yaml python3 -m pytest -m hardware --run-hardware
CONFLUENT_TEST_HARDWARE=tests/support/inventory-dmtf.yaml   python3 -m pytest -m hardware --run-hardware
```

Each command above is the whole setup: the fixtures start what a device stands in for and stop it afterwards.
The IPMI one needs `ipmi_sim`, from OpenIPMI's lanserv tools (`OpenIPMI-lanserv` on Fedora and EL). The Redfish
one needs podman or docker, and pulls DMTF's mockup server image on first use. Where neither is present the tests
skip rather than fail, so an inventory naming a stand-in stays usable on a machine that cannot run it.

Something already listening on the port is used as it stands and left running, so serving a mockup by hand per
`tests/support/mockups/README.md` still works, and a second xdist worker arriving for the same device does not
start a duplicate.

Neither is a substitute for the other, and the difference is worth keeping in mind when reading a green run:

- **A mockup replays what a real machine answered**, vendor extensions included, so it exercises the OEM handler
  that machine selects. That is why captures beat emulators, and why the vendor captures are worth keeping even
  though they cannot be published.
- **`ipmi_sim` answers from a model of a BMC that OpenIPMI wrote.** It proves the session comes up and every read
  path is reached, and it proves nothing about any vendor. Nothing here selects an OEM handler, and no test should
  be written that assumes one. `tests/support/ipmisim.py` describes the machine it presents.

What the simulator does not implement is recorded as `known_failures` in its inventory rather than worked around,
so those tests still run and would report an unexpected pass if a later `ipmi_sim` grew the feature. It has no
chassis boot options at all, and it refuses the FRU read behind `nodeinventory`.

Reaching the simulator through confluent, rather than only at the protocol layer, needs the commit "Read a port off
the manager address over ipmi too". Before it, the ipmi plugin connected to 623 whatever the address said, and the
simulator cannot have 623 because binding it takes privileges no test run should want.

`tests/support/capture-mockups.py` captures a device, running DMTF's Redfish-Mockup-Creator from their published
container so there is nothing to clone or install. It prunes the specification documents afterwards, since
confluent never requests them. Its docstring carries the details. Serve a capture with DMTF's mockup server:

```sh
podman run -d -p 8451:8000 --security-opt label=disable \
    -v <mockup>:/mockup:ro -v <certs>:/certs:ro \
    docker.io/dmtf/redfish-mockup-server:latest \
    -D /mockup -X -s --cert /certs/cert.pem --key /certs/key.pem
```

A replayed device answers reads and holds writes in memory, but has no state machine behind an action: a boot
override can be set and read back, while a reset is accepted and changes nothing. Read-only and reversible
property tests suit it; power and reset tests do not.

An entry missing `address` or `user` is refused at collection rather than skipped. It used to be dropped in
silence, so a run reported a full pass over the devices it did read and never mentioned the one it did not.

Entries may carry a `known_failures` mapping of nodeid substring to reason, for whatever a given device cannot
satisfy. Those tests still run and report as xfail rather than being skipped or deleted, so one that starts
passing shows up as an unexpected pass and the entry can go. Keep two kinds of entry apart: a gap in the replay is
one thing, and a confluent defect the replay exposed is another that should be fixed rather than accumulated.

### Running in parallel

Not yet, deliberately. `pytest-xdist` is not a dependency and no `--dist` option is set, because an option in
`addopts` that only parses when a plugin happens to be installed makes a fresh environment fail to start pytest at
all rather than fail a test. Reproduce the old behaviour with `-p no:xdist`.

The grouping this will need is already in place: each target carries an `xdist_group` so every test for one machine
would land on one worker, and `tests/unit/test_fixture_isolation.py` carries one because its checks depend on
running together and in order. The marker is registered in `pytest.ini` so it stays legal under `--strict-markers`
without the plugin. Adding xdist later therefore means installing it, setting `--dist loadgroup`, and solving the
one thing that grouping does not: a simulator or mockup started by one worker and merely used by another is torn
down when its owner finishes, so ownership needs an interprocess lock and a shared lifecycle. The noderange tests,
which want every device at once rather than one, make that sharper.

Measured before it was removed: three BMCs took 22.9s sequentially and 14.5s at `-n 3`, the slowest single device
rather than the sum. Worth having, once it is correct.

Long-running tests need their own `@pytest.mark.timeout(...)`: `addopts` sets a 60 second limit that an OS
deployment would blow straight through.

`--strict-markers` is on, so a typo in a marker name is an error rather than a silently skipped selection.

## Fixtures

All in `conftest.py`.

- **`configmanager`** gives a `ConfigManager` pointed at a temp directory with `statelessmode` on, so
  `_bg_sync_to_file` and `_sync_to_file` both return early: no DBM file is opened and no background writer thread
  starts. Module globals (`_cfgstore`, `statelessmode`, `_masterkey`, `_txcount`, `_ready` and friends) are
  snapshotted and restored, because the datastore is module state rather than instance state.
- **`confluent_cfgdir`** is the same isolation without the manager, for tests that want to assert about the
  directory itself.
- **`pluginmap`** stubs `core.pluginmap` and rebuilds the route tree via `core._init_core()`. Do not call
  `core.load_plugins()` from a test: it mutates `sys.path`, imports every plugin by bare module name (pysnmp,
  asyncssh, aiohttp, EfiCompressor), registers the affluent plugin, and is not safe to call twice.
- **`_isolate_service_cfg`** (autouse) keeps a real `/etc/confluent/service.cfg` out of the run.
  `confluent.messages` calls `cfgfile.get_option` at module scope, so this is also done once in `pytest_configure`
  before anything imports it.
- **`_collect_garbage`** (autouse) forces a collection after each test, so unawaited-coroutine warnings are blamed
  on the test that caused them rather than some later one.
- **`_reset_noderange_cache`** (autouse) clears `noderange.lastnoderange`. Building any range populates it, and
  `ReverseNodeRange` short-circuits against it, so without this an abbreviation result would depend on whether an
  earlier test happened to evaluate a matching range.
- **`ipmi_command`** returns a connected aiohmi IPMI client, and **`bmc_command`** returns whichever client the
  device speaks, for tests that work either way.

  Both are session-scoped and run on a session-scoped event loop, which the modules using them declare with
  `pytest.mark.asyncio(loop_scope='session')`. This is not a preference. An IPMI session is a UDP conversation
  whose sockets aiohmi registers against the loop that was running when it opened, in module state shared by the
  whole process. Used from a second loop it does not raise, it stops answering, so every read costs a timeout.
  A new IPMI test file without that marker will hang rather than fail, which is a bad afternoon: copy an existing
  one.

  The same constraint is why `_asyncio_debug` is a sync fixture that pulls in an async one rather than being async
  itself. Requesting an async function-scoped fixture is what builds a function-scoped loop, and a session-loop
  test must not have one at all.
- **`ipmi_simulator`** starts `ipmi_sim` for any inventory entry carrying `simulator: true`, and stops it
  afterwards. A simulator already listening on the port is used as it stands and left running, which covers one
  started by hand and a second xdist worker arriving for the same device. It skips, rather than fails, when
  `ipmi_sim` is not installed.
- **`redfish_mockups`** does the same for any entry carrying `mockup: <name>`, serving that capture with DMTF's
  mockup server in a container on the port in the entry's address. A bare name is one of the bundles under
  `tests/support/mockups`; anything with a separator is a path, so an inventory kept outside the repository can
  point at a capture of a real machine. Whether the bundle is short form is read off the tree rather than
  configured, and the certificate it presents is generated per run rather than committed.
- **`redfish_command`** returns a connected aiohmi Redfish client, one per device in the inventory. Credentials
  come from that file and must never be committed. Do not run the hardware tier with `--showlocals`, which would
  print the password into a failure report. Certificates are accepted unverified, so these tests confirm the BMC
  answers, not that it is the BMC you meant.

  One client is built per device per process, not per test. aiohmi has no Redfish logout, so each client leaves a
  session behind until the BMC times it out, and building one per test exhausted a real XCC part way through a
  run: later reads that open a secondary connection then failed with an AttributeError that looks like a confluent
  bug and is not one. Reuse is safe because the client opens and closes an aiohttp session per request rather than
  holding one, so it is not bound to the event loop it was created in.

Both the `configmanager` and `pluginmap` fixtures work by patching module globals, because that is how the code
under test stores its state. Restoration is therefore a correctness property of the suite, not a nicety: a leak
shows up as an unrelated test failing later, or as one passing because of what a previous test left behind.
Restoring a reference is not enough for a mutable container, so the fixtures swap in fresh containers and put the
originals back. `tests/unit/test_fixture_isolation.py` checks that they do. Extend it when you add a fixture that
touches global state.

## Writing async tests

The codebase is asyncio throughout and plugin dispatch is built on async generators, so:

- Write plain `async def test_*` functions. Auto mode means no decorator, and each test gets a fresh event loop.
- Use async fixtures with `yield` for anything holding a socket, a background task or a session.
- Use `unittest.mock.AsyncMock` from the stdlib. There is no third-party mocking dependency.
- **No real sleeps.** Synchronize with `asyncio.Event`, futures, or an injected clock. An `asyncio.sleep` in a test
  is a timing dependency, and with `--timeout=60` a suite full of them is both slow and unreliable.
- **A forgotten `await` fails the run.** `pytest.ini` turns "coroutine ... was never awaited" into an error. This is
  the single most likely regression in a codebase with this many await sites.
- Asyncio debug mode is on for every test, which reports slow callbacks, exceptions never retrieved from a task,
  and the source location of a coroutine rather than just its name.

  It is set on the event loop, not through `PYTHONASYNCIODEBUG`. That variable is process-wide and libraries read
  it for their own purposes: aiohttp captures it at import as `aiohttp.helpers.DEBUG` and builds its HTTP response
  parser with `lax=not DEBUG`, and strict parsing rejects a repeated singleton header. Real BMCs send them, so the
  variable turns a response production accepts into `400, Duplicate 'Etag' header found`. Nothing consults
  `loop.get_debug()` for parsing, so setting it on the loop gets the diagnostics without changing how any library
  treats the wire.

  **Do not export `PYTHONASYNCIODEBUG` yourself.** It reintroduces the strict parsing, and the hardware tier will
  then fail against firmware that is fine in production. Note that `PYTHONASYNCIODEBUG=0` does not disable it
  either: both asyncio and aiohttp test `bool()` of the string.

Existing `unittest.TestCase` and `IsolatedAsyncioTestCase` classes run under pytest unchanged. New tests should be
plain functions with fixtures, but there is no need to rewrite a working test class just to move it here.

## Python version policy

Three constraints, which are not the same thing:

| | requirement | enforced by |
|---|---|---|
| production code runs on | 3.9 | static analysis (see below) |
| **all** code, tests included, must parse on | the oldest version in the compileall matrix | CI |
| the suite is executed on | 3.10+ | the interpreter you run pytest with |

The middle row is easy to overlook. The `python-compileall` job in `.github/workflows/ci.yml` runs
`compileall` over the whole tree, `tests/` included, on every version in its matrix down to 3.8. So a test using
newer *syntax* (a `match` statement, for instance) fails CI even though no one would ever run the suite on 3.8.
Newer standard-library *calls* are fine, because `compileall` only parses. If a test ever genuinely needs new
syntax, the fix is to exclude `tests/` from the older passes and say so here, not to discover it in a CI failure.

Running the suite only on a modern interpreter also means it cannot, by itself, verify that production code still
works on 3.9. That is a static-analysis job. Note that `ruff.toml` currently claims `target-version = "py37"` and
`pyrefly.toml` claims `python-version = "3.6"`, both stale: correcting them to `py39` / `3.9` is what would
actually catch a 3.10-only construct entering production code. Executing the suite on 3.9 as well would be
stronger still, and is worth considering whenever a CI test job is added.

Relevant hazards: `asyncio.to_thread` needs 3.9+, module-level `asyncio.Lock()` in `configmanager.py` is
warning-free only on 3.10+, and `crypt` left the stdlib in 3.13.

A Python 3.9 test environment would need `pytest<9` and `pytest-asyncio<1.3`. That is the only reason those bounds
exist, and why `requirements-test.txt` does not apply them to the default setup.

## Non-goals

Decisions, not omissions:

- **No `pytest-xdist`.** The `configmanager` and `pluginmap` fixtures both patch module globals, so parallel workers
  would hide exactly the isolation bugs this design is most exposed to, and a shared simulator outliving the worker
  that started it needs solving first. Revisit once the suite is demonstrably deterministic. See "Running in
  parallel" for what is already in place for it.
- **No tox or nox.** Deferred to when a CI job is added. Note for that evaluation: a per-component split cannot
  work, because `confluent_server/setup.py.tmpl` declares no dependency on the client (the
  `Requires: confluent_client` lives only in `confluent_server.spec.tmpl`), so a server-only environment cannot
  import `confluent.core`. Any environment must install both components together.
- **No coverage threshold.** Establish a baseline first, gate later.
- **No CI job yet.** `.github/workflows/ci.yml` runs ruff, shellcheck, compileall and pyrefly, and is untouched.
- **No AST-based "contract" tests.** Asserting that source text contains an `await` duplicates what pyrefly's
  `unused-coroutine` and `not-async` checks already do properly, and breaks on any refactor.

## Property-based testing

Hypothesis is not a dependency yet. When it is added, the targets worth property-testing are the `noderange.py`
grammar, TLV encoding in `asynctlvdata` / `tlvdata`, configmanager's expression-valued attributes, and
group-to-node attribute inheritance. Each has an input space that hand-written cases cover poorly.

## Adding tests

This suite is intentionally small. It establishes the structure and the fixtures; coverage grows on top of it.

Where things go:

- Pure logic with no I/O goes in `unit/`, under the tree it exercises. Prefer `pytest.mark.parametrize` over loops
  so each case reports separately.
- Anything driving a real `ConfigManager`, a socket or a subprocess goes in `integration/` with the matching marker.
- Anything talking to a BMC or a daemon goes in `hardware/`, gated on the environment variable for the equipment it
  needs. A default `pytest` run must never reach real hardware.

When bringing in a test that was written elsewhere, drop its `sys.path` preamble and its hand-rolled setup and use
the fixtures above instead; that is what they exist for. Split large multi-subsystem files by subsystem on the way
in rather than moving them whole. Prefer tests that execute code over tests that inspect source text.

Hardware tests that write anything must keep the safety properties of whatever harness they came from: an explicit
list of calls that will never be made, the prior state captured before any write, and a restore path in a fixture
teardown that has been shown to work.

The restore runs for a failed assertion, an exception, a timeout and an interrupt, because teardown runs for all of
them. It does not run if the process dies without unwinding, so a `kill -9` or a host that loses power can leave a
device changed. Keeping the capture on disk instead would close that window; it was considered and deliberately not
built, the exposure being seconds per test against one boot override. Revisit it when a writing test lands whose
change a reboot does not undo, an account or a firmware bank rather than a boot override.
