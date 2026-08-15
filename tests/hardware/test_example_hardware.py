"""Example of the hardware tier, and the thing that proves marker gating works.

Tests in this directory talk to real BMCs or a running daemon. They are
deselected unless the corresponding CONFLUENT_TEST_* variable is set, so a bare
`pytest` never contacts hardware.

    CONFLUENT_TEST_REDFISH_BMC=bmc.example.org pytest -m hardware

Replace this file as real hardware tests land here.

Anything in this tier that performs a write must capture the prior state to
disk first and prove it can restore it, and must keep an explicit list of
calls it will never make. See tests/README.md.
"""

import os

import pytest

pytestmark = pytest.mark.hardware


def test_marker_gating_is_active():
    """If this runs at all, an env var enabled the tier."""
    assert any(os.environ.get(name) for name in (
        'CONFLUENT_TEST_REDFISH_BMC',
        'CONFLUENT_TEST_IPMI_BMC',
        'CONFLUENT_TEST_SMM',
    ))
