"""Guards the packaging invariant the whole test setup depends on.

confluent_server/confluent and confluent_client/confluent are both PEP 420
namespace packages (neither has an __init__.py). With both roots on sys.path
Python merges them into a single "confluent" package, which is also how they
are installed on a real system: both RPMs land in
/opt/confluent/lib/python/confluent/.

That merge is only safe because the two trees share no module names. If a name
were ever added to both, one would silently shadow the other depending on
sys.path order, and the failure would surface somewhere far away. pyrefly.toml
notes the same constraint for its search-path setting.

The dependency runs in one direction too: the server imports modules that live
only in the client tree, so the server cannot be imported alone.
"""

import pathlib

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER_PKG = REPO_ROOT / 'confluent_server' / 'confluent'
CLIENT_PKG = REPO_ROOT / 'confluent_client' / 'confluent'

# Imported by the server (core.py, httpapi.py, sockapi.py, consoleserver.py,
# collective/manager.py, webauthn.py) but defined in the client tree.
CLIENT_MODULES_THE_SERVER_NEEDS = (
    'asynctlvdata',
    'tlvdata',
    'sortutil',
)


def _module_names(pkgdir):
    return {path.relative_to(pkgdir).as_posix()
            for path in pkgdir.rglob('*.py')}


def test_the_two_confluent_trees_share_no_module_names():
    overlap = sorted(_module_names(SERVER_PKG) & _module_names(CLIENT_PKG))
    assert overlap == [], (
        'confluent_server and confluent_client both define these modules; one '
        'will shadow the other depending on sys.path order')


def test_neither_package_root_has_an_init():
    """An __init__.py in either root would end the namespace merge and make
    whichever tree came first on sys.path the only importable one."""
    assert not (SERVER_PKG / '__init__.py').exists()
    assert not (CLIENT_PKG / '__init__.py').exists()


def test_confluent_namespace_spans_both_trees():
    import confluent

    portions = {pathlib.Path(p).resolve() for p in confluent.__path__}
    assert SERVER_PKG.resolve() in portions
    assert CLIENT_PKG.resolve() in portions


@pytest.mark.parametrize('modname', CLIENT_MODULES_THE_SERVER_NEEDS)
def test_client_modules_the_server_depends_on_are_importable(modname):
    module = __import__('confluent.' + modname, fromlist=[modname])
    assert pathlib.Path(module.__file__).resolve().is_relative_to(CLIENT_PKG.resolve())


def test_server_and_client_modules_coexist_in_one_process():
    """The single most important property for a root-level test run: server and
    client modules import together without shadowing each other."""
    import confluent.client
    import confluent.core

    assert pathlib.Path(confluent.core.__file__).resolve().is_relative_to(
        SERVER_PKG.resolve())
    assert pathlib.Path(confluent.client.__file__).resolve().is_relative_to(
        CLIENT_PKG.resolve())
