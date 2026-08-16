"""Configuration for ipmi_sim, the simulated BMC behind the IPMI tier.

OpenIPMI ships ipmi_sim, a BMC that speaks RMCP+ on a UDP port and answers
from a described machine rather than from hardware. That makes it to the IPMI
tier roughly what the DMTF mockups are to the Redfish one: the same tests,
against something that needs no device.

The comparison stops at the transport, though, and it is worth being clear
about what this does and does not prove. A mockup replays what a real machine
answered, vendor sections included. ipmi_sim answers from a model of a BMC
that OpenIPMI wrote, so it exercises session setup, command dispatch and every
read path up to the point where it either answers or refuses. It cannot tell
you what a Lenovo XCC does. Nothing here selects a vendor OEM handler, and
nothing should be written that assumes one.

The configuration lives in this module rather than in committed .conf files
because both need the port substituted, and the FRU image is a byte layout
that is far easier to read as the code that builds it than as a row of hex.

The stock files under /etc/ipmi are not used: the package installs them mode
600 root-only, so a test run as an ordinary user cannot read them. Everything
needed is written fresh into a temp directory instead.
"""


def _typed_string(text):
    """A FRU type/length field holding 8-bit ASCII.

    The top two bits of the length byte select the encoding, and 0xc0 is the
    one that means "the rest is plain ASCII".
    """
    encoded = text.encode('ascii')
    return bytes([0xc0 | len(encoded)]) + encoded


def _checksum(data):
    """The zero checksum FRU areas end with: the byte that sums them to 0."""
    return (-sum(data)) & 0xff


def _fru_image(manufacturer, product, serial, part):
    """A minimal but valid FRU inventory image, board area only.

    Enough for a BMC to report a system that names itself. The chassis,
    product and multirecord areas are all left out, which is legal: the common
    header carries an offset of zero for each, meaning absent.
    """
    board = bytearray([
        0x01,              # format version
        0x00,              # length, filled in below once it is known
        0x00,              # language code, English
        0x00, 0x00, 0x00,  # manufacturing date, unspecified
    ])
    board += _typed_string(manufacturer)
    board += _typed_string(product)
    board += _typed_string(serial)
    board += _typed_string(part)
    board += _typed_string('')  # FRU file ID, empty but present
    board += b'\xc1'            # no more fields in this area

    # Areas are measured in multiples of 8 bytes and end with their checksum,
    # so pad to leave exactly one byte for it.
    board += b'\x00' * ((-(len(board) + 1)) % 8)
    board[1] = (len(board) + 1) // 8
    board.append(_checksum(board))

    header = bytearray([
        0x01,  # format version
        0x00,  # internal use area, absent
        0x00,  # chassis info area, absent
        0x01,  # board info area, at offset 1 * 8
        0x00,  # product info area, absent
        0x00,  # multirecord area, absent
        0x00,  # pad
    ])
    header.append(_checksum(header))
    return bytes(header) + bytes(board)


def _emulation(fru):
    """The machine ipmi_sim presents: one BMC, two sensors and a FRU.

    Deliberately close to the example OpenIPMI ships. Sensor 0 must be the
    watchdog, which is a convention of the simulator rather than of IPMI, and
    the temperature sensor after it is there so that a sensor read has
    something to return rather than an empty repository.

    persist_sdr is not used. It makes the simulator keep its sensor records in
    the state directory, which means the first start of a fresh directory
    answers sensor reads differently from every later one, and a test tier
    that depends on how many times it has been run before is not one worth
    having.
    """
    return '\n'.join([
        'mc_setbmc 0x20',
        'mc_add 0x20 0 no-device-sdrs 0x23 9 8 0x9f 0x1291 0xf02',
        'sel_enable 0x20 1000 0x0a',
        '',
        '# The watchdog, which the simulator expects to be sensor zero.',
        'sensor_add 0x20 0 0 35 0x6f event-only',
        '',
        '# A temperature sensor, with thresholds so a read has a shape.',
        'sensor_add 0x20 0 1 0x01 0x01',
        'sensor_set_value 0x20 0 1 0x60 0',
        'sensor_set_threshold 0x20 0 1 settable 111000 0xa0 0x90 0x70 00 00 00',
        '',
        '# FRU inventory, so the BMC can name the system it belongs to.',
        'mc_add_fru_data 0x20 0 {0} data {1}'.format(
            len(fru), ' '.join('0x{0:02x}'.format(byte) for byte in fru)),
        '',
        'mc_enable 0x20',
        '',
    ])


def _lan(port, user, password):
    """The LAN channel, and the one account reaching it.

    Bound to loopback on purpose. The simulator has no security worth the
    name, its password is in this file, and nothing about a test needs it
    reachable from anywhere else.

    The listed authentication types are the IPMI 1.5 ones and are ignored on
    an RMCP+ session, which is what confluent opens. They are here because
    the simulator's parser wants the field.
    """
    return '\n'.join([
        'name "confluent-test"',
        'set_working_mc 0x20',
        '  startlan 1',
        '    addr 127.0.0.1 {0}'.format(port),
        '    priv_limit admin',
        '    allowed_auths_callback none md2 md5 straight',
        '    allowed_auths_user none md2 md5 straight',
        '    allowed_auths_operator none md2 md5 straight',
        '    allowed_auths_admin none md2 md5 straight',
        '    guid a123456789abcdefa123456789abcdef',
        '  endlan',
        '  user 1 true  ""       "{0}" user  10 none md2 md5 straight'.format(
            password),
        '  user 2 true  "{0}" "{1}" admin 10 none md2 md5 straight'.format(
            user, password),
        '',
    ])


def write_configuration(directory, port, user, password):
    """Write the pair of files ipmi_sim needs, and return their paths.

    ``directory`` is expected to be a per-run temp directory: nothing here is
    meant to survive a run, and the simulator additionally wants somewhere
    writable of its own for state.
    """
    fru = _fru_image('Contoso', 'Test Server 1U', 'SN0123456789',
                     'PN-TEST-0001')

    lanconf = directory / 'lan.conf'
    lanconf.write_text(_lan(port, user, password))

    emulation = directory / 'sim.emu'
    emulation.write_text(_emulation(fru))

    return lanconf, emulation
