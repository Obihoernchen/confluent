"""Example of the hardware tier, and the thing that proves marker gating works.

Tests in this directory talk to real BMCs or a running daemon. The marker keeps
a default run away from hardware entirely; the per-target fixtures
(redfish_bmc, ipmi_bmc, smm) then skip individually, so naming one BMC does not
enable tests for equipment that is not present.

    CONFLUENT_TEST_REDFISH_BMC=bmc.example.org pytest -m hardware

Replace this file as real hardware tests land here.

Anything in this tier that performs a write must capture the prior state to
disk first and prove it can restore it, and must keep an explicit list of
calls it will never make. See tests/README.md.
"""

import pytest

pytestmark = pytest.mark.hardware


def test_redfish_target_is_configured(redfish_bmc):
    """Runs only when CONFLUENT_TEST_REDFISH_BMC names a target, even if
    another CONFLUENT_TEST_* variable is set."""
    assert redfish_bmc
