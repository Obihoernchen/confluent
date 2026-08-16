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
same subsystem will eventually be tested over more than one (`test_ipmi_firmware.py`, `test_pdu_outlets.py`).

**Do not put the safety level in the filename.** It already lives in the marker, which is what actually gates the
test, and `-m readonly` selects on it. A filename that says `readonly` is a second copy of that fact which goes
stale the day the file gains a test at another level. Organise by subsystem instead: subsystems are stable, and
when a BMC misbehaves you want "the sensor tests", not "the read-only tests".

Two shapes are worth keeping distinct:

- **A sweep**, parametrized over many operations, asserting one property that holds for all of them.
  `test_redfish_reads.py` is this: every read either answers or refuses with a reason. Parametrizing gives one
  result line per operation per device, so the pass and refuse pattern across a fleet is readable straight from
  the summary, without a hand-written report.
- **Subsystem files** for invariants specific to one area, where the assertion is about meaning rather than shape.
  `test_redfish_firmware.py` is this: a category must filter rather than return everything, a version must not
  carry a status bit as a digit.

Reach for the sweep when the same question is being asked of many operations, and a subsystem file when a
particular answer has to be checked.

## Markers

| marker | what it means | how to enable |
|---|---|---|
| *(none)* | pure unit test, always runs | |
| `integration` | temp datastore, sockets, subprocesses | always runs |
| `hardware` | real BMC or running daemon | set `CONFLUENT_TEST_HARDWARE` or a `CONFLUENT_TEST_*` address |
| `lab` | a provisioned deployment lab (real PXE/BMC infrastructure) | set `CONFLUENT_TEST_LAB` |

Every `hardware` test additionally carries one **safety level**, described below.

Marked tests are collected and reported as skipped, not deselected, so a run always says what it did not do.
Use `-m hardware` to select only that tier, which does deselect everything else.

The marker is a coarse gate that keeps a default run away from hardware entirely. For per-target precision use the
`redfish_bmc`, `ipmi_bmc` and `smm` fixtures: each skips on its own variable, so naming one BMC does not enable
tests for equipment that is not attached.

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

The single `CONFLUENT_TEST_REDFISH_BMC` / `_USER` / `_PASSWORD` variables still work for a quick one-off and are
treated as one more target, with `CONFLUENT_TEST_REDFISH_ALLOW` as their ceiling.

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

Assertions are about output shape rather than values, since power state, firmware versions and inventory differ
per machine. A device may refuse a command it does not support, provided it explains itself: an unsupported
identify light is a skip, an unexplained failure is a failure. That is the same contract the protocol-level sweep
applies one layer down.

### Testing without hardware

The tier does not care whether a device is real. Point `CONFLUENT_TEST_HARDWARE` at an inventory describing a
simulated one and the same tests run, which is what makes this usable where no hardware is attached, continuous
integration included.

`tests/support/inventory-simulated.yaml` is a worked example against `sushy-tools`, which needs no hypervisor and
no privileges. Its header carries the emulator configuration and the commands to start it.

A simulator implements less than real firmware, so entries carry a `known_failures` mapping of nodeid substring to
reason. Those tests still run and report as xfail rather than being skipped or deleted, and one that starts passing
shows up as an unexpected pass so the entry can be removed.

Keep two kinds of entry apart in that mapping, as the example does. A gap in the simulation is one thing. A
confluent defect the simulator exposed is another, and should be fixed rather than accumulated: pointing the suite
at a device publishing a minimal but legal Redfish tree found two on the first run.

### Running in parallel

Hardware tests are almost entirely network wait, so they parallelise well: three BMCs took 22.9s sequentially and
14.5s with `-n 3`, which is the slowest single device rather than the sum.

```sh
python3 -m pytest -m hardware -n 3
```

Size `-n` by the number of devices, not by CPU count. `--dist loadgroup` is already in `addopts` and is inert
without `-n`, so this needs no other flags.

**The unit of parallelism is the device, not the test.** Each target gets its own `xdist_group`, so every test for
one machine lands on one worker. That keeps concurrent load on a controller to what a single client would produce,
avoids multiplying Redfish sessions that aiohmi never closes, and is the same mechanism that will give a future OS
deployment test exclusive use of a node.

Do not use the default `--dist load`. It splits files across workers, and `tests/unit/test_fixture_isolation.py`
depends on its tests running together and in order: distributed, its checks run where nothing was ever dirtied and
pass while proving nothing. That file carries an `xdist_group` for the same reason.

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
- **`redfish_bmc`, `ipmi_bmc`, `smm`** return the address of a specific piece of test equipment, or skip when its
  variable is unset.
- **`redfish_command`** returns a connected aiohmi Redfish client, and additionally needs
  `CONFLUENT_TEST_REDFISH_USER` and `CONFLUENT_TEST_REDFISH_PASSWORD`. Credentials come from the environment only
  and must never be committed. Do not run the hardware tier with `--showlocals`, which would print the password
  into a failure report. Certificates are accepted unverified, so these tests confirm the BMC answers, not that it
  is the BMC you meant.

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
  would hide exactly the isolation bugs this design is most exposed to. Revisit once the suite is demonstrably
  deterministic.
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
list of calls that will never be made, a capture of the prior state written to disk before any write, and a restore
path that has been shown to work.
