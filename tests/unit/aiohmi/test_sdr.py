"""Tests for the IPMI SDR numeric conversions.

Ported from confluent_server/aiohmi/tests/unit/ipmi/test_sdr.py, which was
inherited from pyghmi and was the only test tracked in the repository. The
oslotest base class it used has been dropped.
"""

import pytest

from aiohmi.ipmi import sdr


@pytest.mark.parametrize('value,bits,expected', [
    (127, 8, 127),      # sign bit clear, returned unchanged
    (0, 8, 0),
    (126, 8, 126),
    (128, 8, -127),     # sign bit set: 0 - (128 ^ 255)
    (255, 8, 0),        # all ones is negative zero in ones complement
    (254, 8, -1),
    (32767, 16, 32767),
    (65535, 16, 0),
])
def test_ones_complement(value, bits, expected):
    assert sdr.ones_complement(value, bits) == expected


@pytest.mark.parametrize('value,bits,expected', [
    (127, 8, 127),      # sign bit clear, returned unchanged
    (0, 8, 0),
    (128, 8, -128),     # most negative value at 8 bits
    (255, 8, -1),
    (254, 8, -2),
    (32767, 16, 32767),
    (65535, 16, -1),
])
def test_twos_complement(value, bits, expected):
    assert sdr.twos_complement(value, bits) == expected


@pytest.mark.parametrize('bits', [8, 16, 32])
def test_complements_agree_on_non_negative_values(bits):
    """Below the sign bit both encodings are the identity."""
    for value in (0, 1, 2, (1 << (bits - 1)) - 1):
        assert sdr.ones_complement(value, bits) == value
        assert sdr.twos_complement(value, bits) == value


@pytest.mark.parametrize('bits', [8, 16])
def test_twos_complement_round_trips(bits):
    """Encoding a negative number and decoding it returns the original."""
    for original in (-1, -2, -(1 << (bits - 1))):
        encoded = original & ((1 << bits) - 1)
        assert sdr.twos_complement(encoded, bits) == original
