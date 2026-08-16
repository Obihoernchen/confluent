"""Run a confluent service against a temp datastore, for the CLI tests.

Not a test module. It is executed as a subprocess by the confluent_service
fixture, which is deliberate: a service in the test process would share
configmanager's module globals with the fixtures that reset them, and would
have to run its socket in whichever event loop happened to be current. A
separate process shares nothing and is closer to how confluent actually runs.

Reads its configuration from argv: a socket path, a JSON node definition
mapping of {nodename: {attribute: value}}, and a directory to log into. Prints
READY on stdout once the socket is accepting, so the parent knows when to
proceed.

Everything it writes afterwards, on stdout or stderr, is read by the fixture
and held against the test that was running. A service that logs a traceback
has found something, and nothing else in the suite is watching for it.
"""

import asyncio
import json
import os
import sys
import tempfile


# The socket listener task, see run() for why it is kept here.
_listener = None


def _configure(logdirectory):
    from confluent.config import configmanager as cfm

    cfm.ConfigManager._cfgdir = tempfile.mkdtemp(prefix='confluent-test-cfg-')
    cfm.statelessmode = True
    cfm._cfgstore = None
    cfm.init(stateless=True)
    # Audit and trace logging write here. Left at the default, the service
    # cannot write to /var/log/confluent as an ordinary user and every request
    # raises PermissionError from a background task. Given by the fixture
    # rather than made here, so that the parent knows where to read it and so
    # it lands somewhere pytest cleans up.
    cfm.set_global('logdirectory', logdirectory)
    return cfm


async def main(socketpath, nodes, logdirectory):
    cfm = _configure(logdirectory)
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
    # debug=True for the same reasons the in-process tests set it on their own
    # loop: a task whose exception nobody retrieved, and a coroutine that was
    # never awaited, are both silent otherwise, and this is the one component
    # in the suite that dispatches the way production does. Set here rather
    # than through PYTHONASYNCIODEBUG, which aiohttp reads for its own purposes
    # and which changes how the wire is parsed. See the _asyncio_debug fixture.
    asyncio.run(main(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]),
                debug=True)
