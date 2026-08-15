"""Tests for the noderange expression grammar.

noderange.py is the best pure-logic target in the tree: its only dependencies
are copy, itertools, pyparsing and re, and NodeRange accepts config=None with
purenumeric=True so a large part of it needs no ConfigManager at all.
"""

import pytest

from confluent import noderange


@pytest.mark.parametrize('nodename,expected', [
    ('n1', ['n', 1, '']),
    ('n10', ['n', 10, '']),
    ('N10', ['n', 10, '']),
    ('r1u10', ['r', 1, 'u', 10, '']),
    ('node', ['node']),
])
def test_humanify_nodename_splits_digits_and_lowercases(nodename, expected):
    assert noderange.humanify_nodename(nodename) == expected


def test_humanify_nodename_sorts_naturally():
    names = ['n10', 'n1', 'n2', 'n20', 'n3']
    assert sorted(names, key=noderange.humanify_nodename) == [
        'n1', 'n2', 'n3', 'n10', 'n20']


@pytest.mark.parametrize('nodename,expected', [
    ('n1', ['n', '{}', '']),
    ('n100', ['n', '{}', '']),
    ('r1u10', ['r', '{}', 'u', '{}', '']),
    ('node', ['node']),
])
def test_unnumber_nodename_stubs_out_numbers(nodename, expected):
    assert noderange.unnumber_nodename(nodename) == expected


@pytest.mark.parametrize('nodename,expected', [
    ('n1', ['1']),
    ('r1u10', ['1', '10']),
    ('node', []),
])
def test_getnumbers_nodename(nodename, expected):
    assert noderange.getnumbers_nodename(nodename) == expected


def test_unnumber_and_getnumbers_are_complementary():
    """The template plus the numbers should reconstruct the original name."""
    nodename = 'r3u14b2'
    template = ''.join(noderange.unnumber_nodename(nodename))
    assert template.format(*noderange.getnumbers_nodename(nodename)) == nodename


def test_group_elements_chunks_by_shape():
    elems = ['n1', 'n2', 'n3', 'r1u1', 'r1u2', 'n10']
    assert noderange.group_elements(elems) == [
        ['n1', 'n2', 'n3'],
        ['r1u1', 'r1u2'],
        ['n10'],
    ]


def test_group_elements_of_empty_input():
    assert noderange.group_elements([]) == [[]]


@pytest.mark.parametrize('expression,expected', [
    ('1', {'1'}),
    ('1-4', {'1', '2', '3', '4'}),
    ('1,3', {'1', '3'}),
    ('1-3,7', {'1', '2', '3', '7'}),
])
def test_purenumeric_noderange(expression, expected):
    """purenumeric mode needs no ConfigManager, which is what makes it unit
    testable here."""
    assert set(noderange.NodeRange(expression, purenumeric=True).nodes) == expected


def test_invalid_syntax_is_rejected():
    with pytest.raises(Exception, match='Invalid syntax'):
        noderange.NodeRange('n[1-', purenumeric=True)


@pytest.mark.parametrize('expression,expected', [
    ('n[1-4]', {'n1', 'n2', 'n3', 'n4'}),
    ('n[1-3],n7', {'n1', 'n2', 'n3', 'n7'}),
    ('r[1-2]u[1-2]', {'r1u1', 'r1u2', 'r2u1', 'r2u2'}),
])
def test_bracket_expansion_without_a_config(expression, expected):
    """With config=None every expanded entity is taken at face value, so the
    bracket grammar can be tested on its own."""
    assert set(noderange.NodeRange(expression).nodes) == expected


async def test_range_and_group_resolution(configmanager):
    """The config-backed path: a range only resolves to nodes that exist, and
    a bare group name expands to its members."""
    await configmanager.set_group_attributes({'mygroup': {}}, autocreate=True)
    await configmanager.set_node_attributes({
        'n1': {'groups': ['mygroup']},
        'n2': {'groups': ['mygroup']},
        'n3': {'groups': ['mygroup']},
    }, autocreate=True)

    assert set(noderange.NodeRange('n1-n3', configmanager).nodes) == {
        'n1', 'n2', 'n3'}
    assert set(noderange.NodeRange('mygroup', configmanager).nodes) == {
        'n1', 'n2', 'n3'}


async def test_unknown_node_is_rejected(configmanager):
    await configmanager.set_node_attributes({'n1': {}}, autocreate=True)
    with pytest.raises(Exception, match='n9 not a recognized node, group, or alias'):
        noderange.NodeRange('n9', configmanager)
