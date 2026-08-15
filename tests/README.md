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

## Markers

| marker | what it means | how to enable |
|---|---|---|
| *(none)* | pure unit test, always runs | |
| `integration` | temp datastore, sockets, subprocesses | always runs |
| `hardware` | real BMC or running daemon | set `CONFLUENT_TEST_REDFISH_BMC`, `CONFLUENT_TEST_IPMI_BMC` or `CONFLUENT_TEST_SMM` |
| `lab` | a provisioned deployment lab (real PXE/BMC infrastructure) | set `CONFLUENT_TEST_LAB` |

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

## Writing async tests

The codebase is asyncio throughout and plugin dispatch is built on async generators, so:

- Write plain `async def test_*` functions. Auto mode means no decorator, and each test gets a fresh event loop.
- Use async fixtures with `yield` for anything holding a socket, a background task or a session.
- Use `unittest.mock.AsyncMock` from the stdlib. There is no third-party mocking dependency.
- **No real sleeps.** Synchronize with `asyncio.Event`, futures, or an injected clock. An `asyncio.sleep` in a test
  is a timing dependency, and with `--timeout=60` a suite full of them is both slow and unreliable.
- **A forgotten `await` fails the run.** `pytest.ini` turns "coroutine ... was never awaited" into an error. This is
  the single most likely regression in a codebase with this many await sites.
- Asyncio debug mode is on for the whole session, which reports slow callbacks and exceptions never retrieved from
  a task.

Existing `unittest.TestCase` and `IsolatedAsyncioTestCase` classes run under pytest unchanged. New tests should be
plain functions with fixtures, but there is no need to rewrite a working test class just to move it here.

## Python version policy

Two separate guarantees:

| | floor | enforced by |
|---|---|---|
| production code (`confluent_server/`, `confluent_client/`) | 3.9 | static analysis |
| test code (`tests/`) | 3.10+ | the interpreter you run pytest with |

Test code may use 3.10+ features freely; tests never ship. Production code must stay 3.9-clean, because installed
systems run it. pytest cannot check that from a single interpreter, so it is a static-analysis job. Note that
`ruff.toml` currently claims `target-version = "py37"` and `pyrefly.toml` claims `python-version = "3.6"`, both
stale: correcting them to `py39` / `3.9` is what would actually catch a 3.10-only construct entering production
code.

Relevant hazards: `asyncio.to_thread` needs 3.9+, module-level `asyncio.Lock()` in `configmanager.py` is
warning-free only on 3.10+, and `crypt` left the stdlib in 3.13.

Only a hypothetical Python 3.9 test environment would need `pytest<9` and `pytest-asyncio<1.3`, which is why
`requirements-test.txt` pins nothing.

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
