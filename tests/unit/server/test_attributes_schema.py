"""Invariants of the node/group attribute schema.

confluent/config/attributes.py is a pure data table with no imports, and
several consumers index into it without guarding. In particular
plugins/configuration/attributes.py reads
allattributes.node[attribute]['description'] directly, so an entry added
without a description is a KeyError at request time rather than a startup
error. These tests make that class of mistake fail at commit time instead.
"""

import pytest

from confluent.config import attributes
from confluent.config import configmanager


ATTRIBUTE_TABLES = pytest.mark.parametrize('table', [
    pytest.param(attributes.node, id='node'),
    pytest.param(attributes.user, id='user'),
])


@ATTRIBUTE_TABLES
def test_every_attribute_has_a_nonempty_description(table):
    missing = sorted(name for name, spec in table.items()
                     if not spec.get('description', '').strip())
    assert missing == [], 'attributes without a usable description'


@ATTRIBUTE_TABLES
def test_attribute_names_are_strings(table):
    assert [name for name in table if not isinstance(name, str)] == []


@ATTRIBUTE_TABLES
def test_attribute_specs_are_dicts(table):
    assert [name for name, spec in table.items()
            if not isinstance(spec, dict)] == []


def test_validvalues_are_sequences():
    """Consumers iterate validvalues, so a bare string would silently iterate
    per character."""
    bad = sorted(name for name, spec in attributes.node.items()
                 if 'validvalues' in spec
                 and not isinstance(spec['validvalues'], (list, tuple)))
    assert bad == []


def test_declared_types_are_supported():
    declared = {spec['type'] for spec in attributes.node.values()
                if 'type' in spec}
    assert declared <= {bool, list}


def test_alias_targets_resolve_to_real_attributes():
    """configmanager._attraliases maps short names onto real attribute names.
    An alias pointing at a nonexistent attribute would accept a set and then
    never be readable."""
    unresolved = sorted(target for target in configmanager._attraliases.values()
                        if target not in attributes.node)
    assert unresolved == []


def test_aliases_do_not_shadow_real_attributes():
    """A name that is both an alias and a real attribute would be ambiguous."""
    collisions = sorted(set(configmanager._attraliases) & set(attributes.node))
    assert collisions == []


def test_node_table_is_populated():
    """Guards against an import or refactor silently emptying the schema, which
    would make every other assertion in this file vacuous."""
    assert attributes.node
    # Long-standing attributes that deployments and the CLI both rely on.
    assert {'groups', 'console.method',
            'hardwaremanagement.manager'} <= set(attributes.node)
