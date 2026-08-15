"""Tests for aiohmi.util.parse.parse_time.

BMCs report timestamps in an assortment of formats, and parse_time works
through them by trial and error. Table-driven tests keep the accepted set
explicit, so a change to the fallback order is visible.
"""

from datetime import datetime

import pytest
from dateutil import tz

from aiohmi.util import parse


def test_none_passes_through():
    assert parse.parse_time(None) is None


def test_utc_zulu_timestamp():
    result = parse.parse_time('2026-08-15T13:45:07Z')
    assert result == datetime(2026, 8, 15, 13, 45, 7, tzinfo=tz.tzutc())
    assert result.tzinfo is not None


@pytest.mark.parametrize('timeval,offset_seconds', [
    ('2026-08-15T13:45:07+02:00', 2 * 3600),
    ('2026-08-15T13:45:07+00:00', 0),
    ('2026-08-15T13:45:07-05:00', -5 * 3600),
])
def test_explicit_utc_offsets(timeval, offset_seconds):
    result = parse.parse_time(timeval)
    assert result.utcoffset().total_seconds() == offset_seconds
    assert result.replace(tzinfo=None) == datetime(2026, 8, 15, 13, 45, 7)


def test_offset_with_fractional_seconds():
    """The fractional part is interpreted as milliseconds, so '.500' is half a
    second. Only a 3-digit fraction round-trips correctly: '.5' would be read
    as 5ms rather than 500ms."""
    result = parse.parse_time('2026-08-15T13:45:07.500+02:00')
    assert result.replace(tzinfo=None) == datetime(2026, 8, 15, 13, 45, 7, 500000)
    assert result.utcoffset().total_seconds() == 2 * 3600


@pytest.mark.parametrize('timeval,expected', [
    ('2026-08-15T13:45:07', datetime(2026, 8, 15, 13, 45, 7)),
    ('2026-08-15', datetime(2026, 8, 15)),
    ('08/15/2026', datetime(2026, 8, 15)),
])
def test_naive_formats(timeval, expected):
    result = parse.parse_time(timeval)
    assert result == expected
    assert result.tzinfo is None


@pytest.mark.parametrize('timeval', [
    '',
    'not a time',
    '15/08/2026',           # day-first is not accepted
    '2026-13-45T99:99:99Z',
])
def test_unparseable_values_return_none(timeval):
    assert parse.parse_time(timeval) is None
