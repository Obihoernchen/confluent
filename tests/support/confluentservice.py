"""Run a confluent service against a temp datastore, for the CLI tests.

Not a test module. It is executed as a subprocess by the confluent_service
fixture, which is deliberate: a service in the test process would share
configmanager's module globals with the fixtures that reset them, and would
have to run its socket in whichever event loop happened to be current. A
separate process shares nothing and is closer to how confluent actually runs.

Reads its configuration from argv: a socket path, then a JSON node definition
mapping of {nodename: {attribute: value}}. Prints READY on stdout once the
socket is accepting, so the parent knows when to proceed.
"""

import asyncio
import json
import os
import sys
import tempfile


def _configure():
    from confluent.config import configmanager as cfm

    cfm.ConfigManager._cfgdir = tempfile.mkdtemp(prefix='confluent-test-cfg-')
    cfm.statelessmode = True
    cfm._cfgstore = None
    cfm.init(stateless=True)
    # Audit and trace logging write here. Left at the default, the service
    # cannot write to /var/log/confluent as an ordinary user and every request
    # raises PermissionError from a background task.
    cfm.set_global('logdirectory', tempfile.mkdtemp(prefix='confluent-test-log-'))
    return cfm


async def main(socketpath, nodes):
    cfm = _configure()
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

    asyncio.ensure_future(
        sockapi._unixdomainhandler(socketpath=socketpath))

    while not os.path.exists(socketpath):
        await asyncio.sleep(0.05)
    sys.stdout.write('READY\n')
    sys.stdout.flush()

    while True:
        await asyncio.sleep(3600)


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1], json.loads(sys.argv[2])))
