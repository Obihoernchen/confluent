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
runtime dependencies importable. Measured into an empty virtualenv rather than copied from the packaging, this is
the set that gets collection to succeed:

```sh
pip install aiohttp asyncssh cryptography libarchive-c lxml msgpack psutil pyparsing pysnmp python-dateutil pyyaml
```

`webauthn` is not among them, and `psutil` is not optional the way the others are: `confluent/util.py` falls
back to `netifaces` when it is missing, so you need one of the two. `EfiCompressor`, `libvirt` and
`confluent.pam` are genuinely optional.

On Python 3.13 and newer add `legacycrypt`, because `crypt` left the stdlib in that release.
`configmanager.py` and `selfservice.py` fall back to it, and to `crypt_r` after that, so either module under
either name will do; a distro that ships a `crypt.py` shim needs neither.

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

### Where the reasoning lives

**Why a thing is done the way it is belongs in the docstring next to it, and nowhere else.** This file says what
exists and what you must not do, then points. The inventory files say how to run them and what their entries mean.
Neither restates a mechanism.

That is not tidiness. A fact written in four places goes stale in four places, and it has: two review rounds of
this branch produced nine findings, five of them documentation that had come to contradict the code, and none of
them a defect in a fixture. When a copy of an explanation would be convenient, write a pointer instead.

### When a unit test earns its place

**Reach for the CLI first.** `test_cli_readonly.py` runs the command a user runs, through argument parsing, the
socket protocol, dispatch, a plugin and the protocol library. It is the interface that has to stay stable anyway,
so a test written against it survives refactoring underneath, and one test covers every layer at once.

A unit test is worth its maintenance when the CLI cannot reach the input. Three cases, and they are the only ones
in `tests/unit` today:

- **A pure function over an input domain the device controls.** `aiohmi.util.parse.parse_time` is reachable
  through `nodeeventlog`, but only with whatever format that BMC emits, and a mockup replays only what was
  captured. The unit test feeds it ten formats, including the fractional-second one that is currently read as
  milliseconds. You cannot ask a BMC for that string.
- **Error paths needing malformed input.** A bad address, a truncated record, a section name with a typo. Devices
  do not produce these to order.
- **Code the CLI genuinely cannot reach**, including the framework testing itself.

**What not to write**, because this is the kind that actually costs maintenance: tests that assert *how* something
is done. Mocking internals, pinning call sequences, reaching into private attributes. They break on every
refactor, they teach people that failing tests are noise, and they were never evidence the feature worked. If a
fake webclient is needed to test `Command.get_health`, the mockups already cover it better and one layer higher.

One trap worth naming: do not assert a behaviour you know is wrong so the suite stays green. Fixing the bug would
then break the test, which is how a suite starts arguing against its own codebase. `test_parse.py` documents the
fractional-second defect in a docstring and asserts the correct case beside it.

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

ipmi:
  - name: ipmi-lab1
    address: 192.0.2.20
    password: ...
    simulator: true            # stand in for it rather than contacting one
```

`redfish` and `ipmi` are the only sections a run accepts, because they are the only equipment any test drives.
A section for enclosures, CDUs, PDUs or switches is a typo until a test needs one, and is refused as such.
`_TARGET_FIXTURES` in `conftest.py` is where a method is added, and doing it there makes the section name and
`--require-target` legal at the same moment.

An entry may also carry `simulator: true`, which asks for a simulated device to be started on the port in its
address rather than a real one being contacted. Only `ipmi` supports it today. See "Testing without hardware".

Fixtures aggregate methods into roles, so a test asks for the broadest one it genuinely works against:

| fixture | methods |
|---|---|
| `redfish_target`, `ipmi_target` | that one method |
| `bmc_target` | `redfish`, `ipmi` |

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

`test_cli_bootdev.py` and `test_cli_bmcpassword.py` do the same for a write, which is a distinct path rather than
the same one with an argument: the tools send an update instead of a fetch. `test_ipmi_bootdev.py` and
`test_redfish_bootdev.py` set the same override one layer down, and being library calls they reach none of it.

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

Add `--hw-level=reversible` to either to include the write tests, which the default ceiling skips. Nothing
described by these two inventories is a machine, so there is nothing for a write to disturb.

Each command above is the whole setup: the fixtures start what a device stands in for and stop it afterwards.
The IPMI one needs `ipmi_sim`, from OpenIPMI's lanserv tools (`OpenIPMI-lanserv` on Fedora and EL). The Redfish
one needs podman or docker, and pulls DMTF's mockup server image on first use. Where neither is present the tests
skip rather than fail, so an inventory naming a stand-in stays usable on a machine that cannot run it.

Neither fixture will run against something it cannot identify, so anything else answering on the port aborts the
run rather than being read as the device the inventory names. Give a stand-in you start by hand a port no
inventory uses. The two differ in what they can identify, for reasons in their docstrings.

Neither is a substitute for the other, and the difference is worth keeping in mind when reading a green run:

- **A mockup replays what a real machine answered**, vendor extensions included, so it exercises the OEM handler
  that machine selects. That is why captures beat emulators, and why the vendor captures are worth keeping even
  though they cannot be published.
- **`ipmi_sim` answers from a model of a BMC that OpenIPMI wrote.** It proves the session comes up and every read
  path is reached, and it proves nothing about any vendor. Nothing here selects an OEM handler, and no test may
  assume one. `tests/support/ipmisim.py` describes the machine it presents and what that is worth.

What the simulator does not implement is recorded as `known_failures` in its inventory rather than worked around,
so those tests still run and would report an unexpected pass if a later `ipmi_sim` grew the feature. It has no
chassis boot options at all, and it refuses the FRU read behind `nodeinventory`.

Reaching the simulator through confluent, rather than only at the protocol layer, needs the commit "Read a port off
the manager address over ipmi too". Before it, the ipmi plugin connected to 623 whatever the address said, and the
simulator cannot have 623 because binding it takes privileges no test run should want.

`tests/support/capture-mockups.py` captures a device, running DMTF's Redfish-Mockup-Creator from their published
container so there is nothing to clone or install. It prunes the specification documents afterwards, since
confluent never requests them. Its docstring carries the details, and `tests/support/mockups/README.md` has the
command for serving one by hand.

A replayed device answers reads and holds writes in memory, but has no state machine behind an action: a boot
override can be set and read back, while a reset is accepted and changes nothing. Read-only and reversible
property tests suit it; power and reset tests do not.

An entry missing `address` or `user` is refused at collection rather than skipped. It used to be dropped in
silence, so a run reported a full pass over the devices it did read and never mentioned the one it did not.

Entries may carry a `known_failures` mapping of nodeid substring to reason, for whatever a given device cannot
satisfy. Those tests still run and report as xfail rather than being skipped or deleted, so one that starts
passing shows up as an unexpected pass and the entry can go. Keep two kinds of entry apart: a gap in the replay is
one thing, and a confluent defect the replay exposed is another that should be fixed rather than accumulated.

An entry whose key matches nothing collected is refused at collection. These match on a substring of the nodeid,
so renaming a test is enough to leave a record that reads as a watched defect while nothing watches it.

Two more things keep the list honest, because a stale entry is the normal end state and half of them are silent.
Against a simulator or a replayed capture the marker is strict, so an entry whose defect has been fixed fails the
run rather than reporting an unexpected pass nobody reads; real hardware stays lenient, where one flaky read
should not turn a run red. And an entry whose tests all skipped is named in the terminal summary, because it
decided nothing and looks identical whether the defect is still there or was fixed a year ago. Of nineteen
entries retired on 2026-09-02, nine were unexpected passes and ten were that second, quieter kind.

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

## Continuous integration

`.github/workflows/ci.yml` carries a `Pytest` job running the three tiers that need no hardware: unit and
integration, Redfish against the vendored DMTF mockups, and IPMI against `ipmi_sim` from Ubuntu's `openipmi`
package. Both hardware tiers pass `--require-target`, so a runner missing a container runtime or the simulator
fails rather than skipping its way to a green run, and `--hw-level=reversible`, without which every write test
skips and the job proves reads only.

Two legs, 3.12 and 3.13, because those are the two sides of the `crypt` fallback in `configmanager.py` and
`selfservice.py`. Around two minutes each, of which the tests themselves are under thirty seconds; the rest is
installing dependencies and pulling the 175MB replay image.

That image is pinned by digest in `conftest.py`, and the job reads the digest back out of it rather than
repeating it, so the two cannot drift and a green run last month used the same replay server as one today.

## Fixtures

All in `conftest.py`, where each one's docstring carries the reasoning. This is the index: what it gives you, and
what you have to know before reaching for it.

| fixture | gives you |
|---|---|
| `configmanager` | a `ConfigManager` on a temp directory, stateless, nothing written to disk |
| `confluent_cfgdir` | the same isolation without the manager, for asserting about the directory |
| `pluginmap` | an empty plugin map and a freshly built route tree |
| `confluent_service`, `run_cli` | a real service on a temp socket, and the `node*` tools run against it |
| `service_node`, `service_nodes` | the name that service knows a device by, or all of them at once |
| `redfish_command`, `ipmi_command`, `bmc_command` | a connected client for one device |
| `redfish_target`, `ipmi_target`, `bmc_target` | the inventory entry, one test run per device |
| `ipmi_simulator`, `redfish_mockups` | whatever stands in for a device, started and stopped for you |
| `_isolate_service_cfg`, `_collect_garbage`, `_reset_noderange_cache`, `_asyncio_debug` | autouse, no action needed |

Four things to know before writing against them:

- **Do not call `core.load_plugins()` from a test.** It mutates `sys.path`, imports every plugin by bare module
  name (pysnmp, asyncssh, aiohttp, EfiCompressor), registers the affluent plugin, and is not safe to call twice.
  `pluginmap` builds the route tree directly instead.
- **A module using `ipmi_command` or `bmc_command` must carry `pytest.mark.asyncio(loop_scope='session')`.**
  Forgetting it is refused at collection, so the mistake names itself, but copying an existing IPMI file means the
  question never comes up.
- **Do not run the hardware tier with `--showlocals`.** It would print a BMC password into a failure report.
- **Certificates are accepted unverified**, so these tests confirm the BMC answers, not that it is the BMC you
  meant. Anything relying on identity needs a pinned fingerprint, the way confluent itself does it.

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

  **Do not export `PYTHONASYNCIODEBUG` yourself**, and note that setting it to `0` does not turn it off. It makes
  aiohttp parse the wire more strictly and the hardware tier then fails against firmware that is fine in
  production. The mechanism, and why the flag goes on the loop instead, are in the `_asyncio_debug_function`
  docstring in `conftest.py`.

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
warning-free only on 3.10+, and `crypt` left the stdlib in 3.13, on which see the dependency note under
"Running". CI runs the suite on both 3.12 and 3.13 so that both sides of that fallback are exercised.

A Python 3.9 test environment would need `pytest<9` and `pytest-asyncio<1.3`. That is the only reason those bounds
exist, and why `requirements-test.txt` does not apply them to the default setup.

## Non-goals

Decisions, not omissions:

- **No `pytest-xdist`.** The `configmanager` and `pluginmap` fixtures both patch module globals, so parallel workers
  would hide exactly the isolation bugs this design is most exposed to, and a shared simulator outliving the worker
  that started it needs solving first. Revisit once the suite is demonstrably deterministic. See "Running in
  parallel" for what is already in place for it.
- **No tox or nox.** Still deferred. Note for that evaluation: a per-component split cannot
  work, because `confluent_server/setup.py.tmpl` declares no dependency on the client (the
  `Requires: confluent_client` lives only in `confluent_server.spec.tmpl`), so a server-only environment cannot
  import `confluent.core`. Any environment must install both components together.
- **No coverage threshold.** Establish a baseline first, gate later.
- ~~**No CI job yet.**~~ `.github/workflows/ci.yml` gained a `Pytest` job. See "Continuous integration".
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
