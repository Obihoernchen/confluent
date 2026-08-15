"""Tests for the client-side natural sort helpers.

Note that confluent_server/confluent/util.py carries a near-duplicate pair of
naturalize_string/natural_sort, and confluent/noderange.py a third variant as
humanify_nodename. They are not currently shared. This file tests the client
copy, which is the one confluent.collective.manager imports via the
namespace-package merge.
"""

import pytest

from confluent import sortutil


@pytest.mark.parametrize('key,expected', [
    ('n1', ['n', 1, '']),
    ('N1', ['n', 1, '']),
    ('node10', ['node', 10, '']),
    ('r1u10', ['r', 1, 'u', 10, '']),
    ('plain', ['plain']),
    ('', ['']),
])
def test_naturalize_string(key, expected):
    assert sortutil.naturalize_string(key) == expected


def test_natural_sort_orders_numerically_not_lexically():
    names = ['n10', 'n1', 'n100', 'n2', 'n20']
    assert sortutil.natural_sort(names) == ['n1', 'n2', 'n10', 'n20', 'n100']


def test_natural_sort_is_case_insensitive_on_text():
    assert sortutil.natural_sort(['b1', 'A1', 'a2']) == ['A1', 'a2', 'b1']


def test_natural_sort_handles_multiple_number_groups():
    names = ['r2u1', 'r1u10', 'r1u2', 'r10u1']
    assert sortutil.natural_sort(names) == ['r1u2', 'r1u10', 'r2u1', 'r10u1']


def test_natural_sort_handles_differing_name_shapes():
    """A numbered and an unnumbered name still sort through the normal path:
    the keys differ at their first string component."""
    assert sortutil.natural_sort(['n1', 'node']) == ['n1', 'node']


def test_naturalize_string_rejects_bytes():
    """The regex is a str pattern, so a bytes key cannot be built at all. This
    is what the fallback below exists for."""
    with pytest.raises(TypeError):
        sortutil.naturalize_string(b'n1')


def test_natural_sort_falls_back_to_ascii_when_no_key_can_be_built():
    """natural_sort is documented to fall back rather than raise.

    Two str inputs can never trigger it: naturalize_string always yields str
    at even positions and int at odd ones, so same-position comparisons are
    always same-type. Bytes do trigger it, per the test above.

    The expected order is the discriminator. ASCII order puts b'n10' first
    because '1' < '2'; a working natural sort would put b'n2' first. So this
    fails if the fallback is removed, and also fails if the fallback is
    somehow bypassed.
    """
    assert sortutil.natural_sort([b'n10', b'n2']) == [b'n10', b'n2']


def test_natural_sort_does_not_mutate_input():
    names = ['n10', 'n1']
    sortutil.natural_sort(names)
    assert names == ['n10', 'n1']
