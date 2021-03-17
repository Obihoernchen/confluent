#!/usr/bin/python
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2021 Lenovo
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

try:
    import confluent.sshutil as sshutil
except ImportError:
    pass
import eventlet
import eventlet.green.subprocess as subprocess
import msgpack
import os
import struct
import sys

running_status = {}
class PlayRunner(object):
    def __init__(self, playfiles, nodes):
        self.playfiles = playfiles
        self.nodes = nodes
        self.worker = None
        self.results = []
        self.complete = False

    def _start_playbooks(self):
        self.worker = eventlet.spawn(self._really_run_playbooks)

    def get_available_results(self):
        avail = self.results
        self.results = []
        return avail

    def _really_run_playbooks(self):
        with open(os.devnull, 'w+') as devnull:
            targnodes = ','.join(self.nodes)
            for playfilename in self.playfiles:
                worker = subprocess.Popen(
                    [sys.executable, __file__, targnodes, playfilename],
                    stdin=devnull, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
                stdout, self.stderr = worker.communicate()
                current = memoryview(stdout)
                while len(current):
                    sz = struct.unpack('=q', current[:8])[0]
                    result = msgpack.unpackb(current[8:8+sz], raw=False)
                    self.results.append(result)
                    current = current[8+sz:]
        self.complete = True


def run_playbooks(playfiles, nodes):
    sshutil.prep_ssh_key('/etc/confluent/ssh/automation')
    runner = PlayRunner(playfiles, nodes)
    for node in nodes:
        running_status[node] = runner
    runner._start_playbooks()


def print_result(result, state, collector):
    output = {
        'task_name': result.task_name,
        'changed': result._result['changed'],
    }
    output['state'] = state
    output['warnings'] = result._result.get('warnings', [])
    try:
        del result._result['warnings']
    except KeyError:
        pass
    if collector:
        output['errorinfo'] = collector._dump_results(result._result)
    msg = msgpack.packb(output, use_bin_type=True)
    msglen = len(msg)
    sys.stdout.buffer.write(struct.pack('=q', msglen))
    sys.stdout.buffer.write(msg)

if __name__ == '__main__':
    from ansible.inventory.manager import InventoryManager
    from ansible.parsing.dataloader import DataLoader
    from ansible.executor.task_queue_manager import TaskQueueManager
    from ansible.vars.manager import VariableManager
    from ansible.playbook.play import Play
    from ansible import context
    from ansible.module_utils.common.collections import ImmutableDict
    from ansible.plugins.callback import CallbackBase
    import yaml

    class ResultsCollector(CallbackBase):

        def v2_runner_on_unreachable(self, result):
            print_result(result, 'UNREACHABLE', self)

        def v2_runner_on_ok(self, result, *args, **kwargs):
            print_result(result, 'ok', self)

        def v2_runner_on_failed(self, result, *args, **kwargs):
            print_result(result, 'FAILED', self)

    context.CLIARGS = ImmutableDict(
        connection='smart', module_path=['/usr/share/ansible'], forks=10,
        become=None, become_method=None, become_user=None, check=False,
        diff=False, verbosity=0, remote_user='root')


    invlist = sys.argv[1] + ','
    loader = DataLoader()
    invman = InventoryManager(loader=loader, sources=invlist)
    varman = VariableManager(loader=loader, inventory=invman)

    plays = yaml.safe_load(open(sys.argv[2]))
    if isinstance(plays, dict):
        plays = [plays]
    taskman = TaskQueueManager(inventory=invman, loader=loader, passwords={},
        variable_manager=varman, stdout_callback=ResultsCollector())
    for currplay in plays:
        currplay['hosts'] = sys.argv[1]
        play = Play().load(currplay, loader=loader)
        try:
            taskman.run(play)
        finally:
            taskman.cleanup()
            if loader:
                loader.cleanup_all_tmp_files()
