"""One request across several devices at once.

The only place in the suite that produces genuinely concurrent dispatch. Every
other test speaks to one device at a time, so the fan-out in core, where a
noderange is split by handler, worked through a queue and gathered back, is
never exercised the way production exercises it.

That matters because it is where async defects actually live. A unit test is
sequential and finishes in milliseconds, so it cannot show a response being
lost between workers, a node being reported twice, a queue that never reaches
its completion count, or an await that quietly serialises what should overlap.
Several real devices answering at different speeds can.

These assert on the shape of the result rather than on timing. Timing would be
the more direct measure of whether dispatch overlaps, but it is also the
quickest way to a test that fails on a loaded machine for no real reason. A
lost or duplicated node is unambiguous.
"""

import pytest

pytestmark = [pytest.mark.hardware, pytest.mark.readonly]


def _reporting_nodes(output):
    """The set of nodes that said something, from "node: value" lines."""
    return {line.split(':', 1)[0].strip()
            for line in output.splitlines() if ':' in line}


def _require_several(nodes):
    if len(nodes) < 2:
        pytest.skip('needs at least two devices to dispatch concurrently')


def test_every_node_in_a_noderange_answers_exactly_once(run_cli,
                                                        service_nodes):
    """No node lost, none duplicated.

    A response dropped between the fan-out and the gather shows up here as a
    missing node, and a completion counted twice shows up as a duplicate.
    Neither is visible when talking to one device.
    """
    _require_several(service_nodes)
    noderange = ','.join(service_nodes)

    returncode, output = run_cli('nodepower', noderange)

    assert returncode == 0, output
    lines = [line for line in output.splitlines() if ':' in line]
    assert _reporting_nodes(output) == set(service_nodes), output
    assert len(lines) == len(service_nodes), \
        'expected one line per node, got:\n{0}'.format(output)


def test_a_noderange_is_stable_across_repeats(run_cli, service_nodes):
    """The same request gives the same set of nodes every time.

    A race in the fan-out does not necessarily fail on the first attempt. This
    is a cheap way to give one a chance to show itself without resorting to
    timing assertions.
    """
    _require_several(service_nodes)
    noderange = ','.join(service_nodes)

    seen = []
    for _ in range(3):
        returncode, output = run_cli('nodepower', noderange)
        assert returncode == 0, output
        seen.append(_reporting_nodes(output))

    assert seen[0] == set(service_nodes)
    assert all(result == seen[0] for result in seen), \
        'the same noderange reported different nodes on repeat: {0}'.format(seen)


def test_one_bad_node_does_not_suppress_the_others(run_cli, service_nodes):
    """A noderange mixing real and unknown nodes still answers for the real
    ones.

    The error path through a concurrent fan-out is its own hazard: one failing
    branch must not abandon the others, and must not be swallowed either.
    """
    _require_several(service_nodes)

    returncode, output = run_cli(
        'nodepower', ','.join(service_nodes + ['nosuchnode']))

    assert output.strip(), 'the whole request produced no output'
    assert 'Unexpected' not in output, output
    if returncode == 0:
        # Some tools report the unknown node and carry on.
        assert set(service_nodes) <= _reporting_nodes(output), output
    else:
        # Others refuse the range outright, which is defensible provided the
        # reason names the node at fault rather than failing silently.
        assert 'nosuchnode' in output, output
