#!/bin/bash
# Install the freshly built rpms on a clean EL9/EL10 system with EPEL enabled
# and verify the server actually imports and starts.
#
# Usage: smoketest.sh <dir-with-rpms>
set -x

RPMDIR=$(readlink -f "$1")
FAIL=0

findrpm() {
    local pat=$1
    local f
    f=$(find "$RPMDIR" -name "$pat" ! -name '*.src.rpm' | head -1)
    if [ -z "$f" ]; then
        echo "MISSING rpm matching $pat in $RPMDIR" >&2
        find "$RPMDIR" -name '*.rpm' >&2
        return 1
    fi
    echo "$f"
}

CLIENT=$(findrpm 'confluent_client-*.noarch.rpm') || FAIL=1
VTBUFFERD=$(findrpm 'confluent_vtbufferd-*.rpm') || FAIL=1
IMGUTIL=$(findrpm 'confluent_imgutil-*.noarch.rpm') || FAIL=1
SERVER=$(findrpm 'confluent_server-*.noarch.rpm') || FAIL=1
OSDEPLOY=$(findrpm 'confluent_osdeploy-x86_64-*.noarch.rpm') || FAIL=1
[ "$FAIL" = 1 ] && exit 1

dnf -y install epel-release
dnf config-manager --set-enabled crb || true
dnf -y install procps-ng util-linux

echo "::group::install client, vtbufferd, imgutil via dnf"
dnf -y install "$CLIENT" "$VTBUFFERD" "$IMGUTIL" || FAIL=1
echo "::endgroup::"

echo "::group::install server (dnf, fall back to --nodeps for known-missing deps)"
if ! dnf -y install "$SERVER"; then
    # expected: python3-webauthn (and python3-eficompressor on el9) are not in EPEL
    rpm -qpR "$SERVER" | grep -vE '^(confluent|rpmlib|/)' | sed 's/ [<>=].*//' | sort -u > /tmp/reqs
    xargs -a /tmp/reqs dnf -y install --skip-broken
    rpm -ivh --nodeps "$SERVER" || FAIL=1
fi
echo "::endgroup::"

echo "::group::install osdeploy (--nodeps: requires Lenovo confluent_ipxe)"
rpm -ivh --nodeps "$OSDEPLOY" || FAIL=1
test -d /opt/confluent/lib/osdeploy/el10/profiles || FAIL=1
echo "::endgroup::"

echo "::group::import and daemon smoke test"
export PYTHONPATH=/opt/confluent/lib/python
python3 -c "import confluent.main; print('IMPORT confluent.main: OK')" || FAIL=1
python3 -c "import confluent.snmputil; print('IMPORT confluent.snmputil: OK')" || FAIL=1
python3 -c "import aiohmi.redfish.command; print('IMPORT aiohmi.redfish: OK')" || FAIL=1
# the rpm sets up non-root operation (/etc/confluent owned by confluent), so
# the daemon must run as the confluent user; the asyncio port only supports
# foreground mode (-f), so background it ourselves
mkdir -p /var/run/confluent
chown confluent:confluent /var/run/confluent
timeout 30 runuser -u confluent -- /opt/confluent/bin/confluent -f > /tmp/confluentd.log 2>&1 &
sleep 10
if pgrep -f '/opt/confluent/bin/confluent' > /dev/null && [ -S /var/run/confluent/api.sock ]; then
    echo "DAEMON_RUNNING=yes"
else
    echo "DAEMON_RUNNING=no"
    cat /tmp/confluentd.log || true
    ls -la /var/run/confluent/ || true
    FAIL=1
fi
pkill -f '/opt/confluent/bin/confluent' || true
echo "::endgroup::"

exit $FAIL
