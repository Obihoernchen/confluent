"""Run a confluent service against a temp datastore, for the CLI tests.

Not a test module. It is executed as a subprocess by the confluent_service
fixture in tests/conftest.py, whose docstring says why a subprocess rather
than a service in the test process.

Reads a socket path and a working directory from argv, and the node
definitions, {nodename: {attribute: value}}, as JSON on stdin rather than argv
because they hold BMC passwords. The datastore and the logs go under that
directory, which the fixture owns. Prints READY on stdout once the socket is
accepting, so the parent knows when to proceed, and everything it writes
afterwards is read by the fixture and held against the test that was running.
"""

import asyncio
import json
import os
import sys


# The socket listener task, see run() for why it is kept here.
_listener = None


def _configure(directory):
    from confluent.config import configmanager as cfm

    cfgdirectory = os.path.join(directory, 'cfg')
    os.makedirs(cfgdirectory, exist_ok=True)
    cfm.ConfigManager._cfgdir = cfgdirectory
    cfm.statelessmode = True
    cfm._cfgstore = None
    cfm.init(stateless=True)
    # Audit and trace logging write here. Left at the default, the service
    # cannot write to /var/log/confluent as an ordinary user and every request
    # raises PermissionError from a background task. Under the fixture's
    # directory rather than a mkdtemp of its own, so the parent knows where to
    # read it and pytest is the one that cleans it up.
    logdirectory = os.path.join(directory, 'log')
    os.makedirs(logdirectory, exist_ok=True)
    cfm.set_global('logdirectory', logdirectory)
    return cfm


async def main(socketpath, nodes, directory):
    cfm = _configure(directory)
    cfg = cfm.ConfigManager(None)
    await cfg.set_node_attributes(nodes, autocreate=True)

    import confluent.core as core
    import confluent.log as log
    import confluent.sockapi as sockapi

    # The real registry rather than a curated subset, so dispatch resolves
    # handlers exactly as it does in production.
    core.load_plugins()

    sockapi.auditlog = log.Logger('audit')
    sockapi.tracelog = log.Logger('trace')

    # Held in a name for the lifetime of the process. A task referenced by
    # nothing may be collected mid-flight, which would take the socket
    # listener with it and leave the service accepting nothing.
    global _listener
    _listener = asyncio.ensure_future(
        sockapi._unixdomainhandler(socketpath=socketpath))

    while not os.path.exists(socketpath):
        await asyncio.sleep(0.05)
    sys.stdout.write('READY\n')
    sys.stdout.flush()

    while True:
        await asyncio.sleep(3600)


if __name__ == '__main__':
    # stdin is fully consumed before anything else runs, and the parent closes
    # its end straight after writing, so this does not block.
    # debug=True on the loop, never through PYTHONASYNCIODEBUG: see the
    # _asyncio_debug_function docstring in tests/conftest.py. This is the one
    # component in the suite that dispatches the way production does, so an
    # unretrieved task exception here is worth more than anywhere else.
    asyncio.run(main(sys.argv[1], json.loads(sys.stdin.read()), sys.argv[2]),
                debug=True)
