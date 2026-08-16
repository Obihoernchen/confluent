#!/usr/bin/python3
"""Capture every device in an inventory into replayable Redfish mockups.

A wrapper around DMTF's Redfish-Mockup-Creator, which walks a live service and
writes the tree in the layout their mockup server replays. This runs it once
per device from the same inventory the tests use, and keeps credentials out of
the process list.

    ./capture-mockups.py --inventory ~/.confluent-test-bmcs.yaml \\
        --output ~/git/confluent-test-mockups

The creator runs from DMTF's published container, so there is nothing to clone
and no python dependency to install. The container runs with host networking,
which matters for a BMC reached through a tunnel: without it, a loopback
address in the inventory would resolve to the container's own loopback rather
than the tunnel.

Serve a capture with:

    podman run -d -p 8446:8000 --security-opt label=disable \\
        -v <output>/<device>:/mockup:ro -v <certs>:/certs:ro \\
        docker.io/dmtf/redfish-mockup-server:latest \\
        -D /mockup -X -s --cert /certs/cert.pem --key /certs/key.pem

Headers are captured and the server replays them with -X, which matters
because some quirks live in the headers rather than the body: one BMC here
returns ETag twice on its service root.

Session authentication, not basic. Basic is refused outright by at least one
BMC here, where session works on all of them.

The creator also walks JsonSchemas, whose entries link out to
redfish.dmtf.org. With no route to that host a capture appears to hang, having
written nothing but schema documents. If a run seems stuck, check outbound
access before suspecting the BMC.

Strictly read-only, since the creator only ever issues GET.

Nothing is redacted: a mockup carries the serial numbers, addresses and
identifiers of the machine it came from, so keep the output somewhere you are
content for that to live.
"""

import argparse
import configparser
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

# Removed after capture unless --keep-schemas. These are the specification
# documents a service publishes about itself: identical across vendors,
# already public, and never requested by confluent, which only ever asks for
# the service root, AccountService, Chassis, Managers, SessionService, Systems
# and UpdateService. On one BMC here they were half of a 34MB capture.
# $metadata and odata are the same content under the names the service
# advertises them by.
PRUNE = ('JsonSchemas', 'schemas', 'schema', 'metadata', '$metadata', 'odata')


def devices(inventory_path):
    with open(inventory_path) as handle:
        inventory = yaml.safe_load(handle) or {}
    defaults = inventory.get('defaults') or {}
    for entry in inventory.get('redfish') or []:
        device = dict(defaults)
        device.update(entry)
        if device.get('address') and device.get('user'):
            yield device


def prune(root):
    """Drop the specification documents the capture dragged along."""
    removed = 0
    for base, directories, _ in os.walk(root, topdown=True):
        for name in list(directories):
            if name in PRUNE:
                shutil.rmtree(os.path.join(base, name), ignore_errors=True)
                directories.remove(name)
                removed += 1
    return removed


def capture(device, output, runtime, image, maxlogentries):
    name = str(device.get('name', device['address']))
    destination = os.path.join(output, name)
    os.makedirs(destination, exist_ok=True)

    config = configparser.ConfigParser()
    config['Authentication'] = {'user': device['user'],
                                'password': device.get('password') or ''}
    config['Connection'] = {'rhost': device['address'], 'Secure': 'true',
                            'Auth': 'Session'}
    # The image fixes the output at /mockup, which is where destination is
    # mounted, so Dir here is only for the record it writes.
    config['Output'] = {'Dir': '/mockup',
                        'description': 'captured from {0}'.format(name)}
    config['Options'] = {'Headers': 'true', 'Time': 'false', 'quiet': 'false',
                         'trace': 'false',
                         'maxlogentries': str(maxlogentries),
                         'forcefolderrename': 'false'}

    # A config file rather than --password, which would put the credential in
    # the process list for as long as the capture runs.
    handle, path = tempfile.mkstemp(prefix='mockup-capture-', suffix='.ini')
    os.close(handle)
    os.chmod(path, 0o600)
    try:
        with open(path, 'w') as target:
            config.write(target)
        # Output is not captured, so progress reaches the terminal. A capture
        # takes minutes and silence is indistinguishable from a hang.
        completed = subprocess.run([
            runtime, 'run', '--rm', '--network', 'host',
            '--security-opt', 'label=disable',
            '-v', '{0}:/mockup'.format(os.path.abspath(destination)),
            '-v', '{0}:/config.ini:ro'.format(path),
            image, '--config', '/config.ini'])
    finally:
        os.remove(path)

    resources = sum(1 for _, _, files in os.walk(destination)
                    for name_ in files if name_ == 'index.json')
    return name, completed.returncode, resources, destination


def main():
    parser = argparse.ArgumentParser(
        description='Capture inventory devices into Redfish mockups.')
    parser.add_argument('--inventory', required=True,
                        help='inventory YAML, as used by CONFLUENT_TEST_HARDWARE')
    parser.add_argument('--output', required=True,
                        help='directory to write one mockup per device into')
    parser.add_argument('--runtime', default='podman',
                        help='container runtime (default podman)')
    parser.add_argument('--image',
                        default='docker.io/dmtf/redfish-mockup-creator:latest',
                        help='creator image')
    parser.add_argument('--maxlogentries', type=int, default=20,
                        help='log entries per log service (default 20)')
    parser.add_argument('--keep-schemas', action='store_true',
                        help='keep JsonSchemas, schemas, metadata and odata, '
                             'which are pruned by default')
    arguments = parser.parse_args()

    if not shutil.which(arguments.runtime):
        parser.error('no {0} on PATH'.format(arguments.runtime))
    os.makedirs(arguments.output, exist_ok=True)

    failures = 0
    for device in devices(arguments.inventory):
        name, returncode, resources, destination = capture(
            device, arguments.output, arguments.runtime, arguments.image,
            arguments.maxlogentries)
        pruned = 0 if arguments.keep_schemas else prune(destination)
        sys.stderr.write('{0}: {1} resources, {2} specification directories '
                         'pruned (rc={3})\n'.format(
                             name, resources, pruned, returncode))
        if returncode != 0 or not resources:
            failures += 1
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
