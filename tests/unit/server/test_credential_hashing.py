"""How a stored credential is hashed, and that it stays verifiable.

`hashcrypt_value` is what `crypted.*` attributes hold, and `selfservice.py`
authenticates a node by re-crypting the presented key against the salt it
reads back out of the stored value. Those two have to agree, and they have to
go on agreeing across a change of interpreter: a hash written by one confluent
is verified by the next one, possibly years later and on a different distro.

That is why the pinned vector below matters more than it looks. `crypt` left
the standard library in 3.13, so a Python without it reaches libcrypt some
other way, and nothing in the round trip test would notice a backend that
hashed differently: it would write and verify consistently within itself while
rejecting every credential stored before the change. The vector is what
catches that, and it is why this is one of the cases the README allows a unit
test for, since no CLI path can be made to hash a chosen salt.
"""

import pytest

from confluent.config import configmanager


# A password, a salt, and what glibc-compatible SHA-512 crypt makes of them.
# Generated with the stdlib crypt module on Python 3.12 and fixed here, so it
# is a statement about the algorithm rather than about whatever is installed.
KNOWN_PASSWORD = 'hunter2'
KNOWN_SALT = '$6$abcdefghijkl'
KNOWN_HASH = ('$6$abcdefghijkl$ztHGAvIiw4wUuq8EI6DH3vo22C0s1bVBJNNzAvTWFeXf'
              'pi.CJR/hbYwc3pzUQ6TSH21cZTqnkuwxh8IgCO3BX0')


def _salt_of(hashvalue):
    """The salt, read back the way selfservice.py reads it.

    Deliberately the same three-way split rather than a tidier one, because
    what is being checked is that hashcrypt_value writes something that
    function can take apart again.
    """
    return '$'.join(hashvalue.split('$', 3)[:-1]) + '$'


def test_the_hash_is_sha512_crypt():
    """Whatever provides crypt must produce the same hash as the stdlib did.

    A backend that hashed differently would be self-consistent and would still
    reject every credential written before it arrived.
    """
    assert configmanager.crypt.crypt(KNOWN_PASSWORD, KNOWN_SALT) == KNOWN_HASH


def test_a_stored_hash_verifies_the_way_selfservice_does():
    hashvalue = configmanager.hashcrypt_value(KNOWN_PASSWORD)

    salt = _salt_of(hashvalue)
    assert configmanager.crypt.crypt(KNOWN_PASSWORD, salt) == hashvalue


def test_a_wrong_password_does_not_verify():
    """Not a tautology: the check above passes for a backend that ignores its
    input entirely."""
    hashvalue = configmanager.hashcrypt_value(KNOWN_PASSWORD)

    salt = _salt_of(hashvalue)
    assert configmanager.crypt.crypt('not the password', salt) != hashvalue


def test_each_value_gets_its_own_salt():
    """Two nodes with the same key must not share a hash, or one stored value
    tells an attacker about the other."""
    assert (configmanager.hashcrypt_value(KNOWN_PASSWORD)
            != configmanager.hashcrypt_value(KNOWN_PASSWORD))


@pytest.mark.parametrize('password', ['', 'p' * 200, 'pässw0rd!', 'a b\tc'])
def test_awkward_passwords_still_round_trip(password):
    """An empty key, a long one, non-ASCII and whitespace. These are where a
    ctypes wrapper around libcrypt would differ from the stdlib if it did."""
    hashvalue = configmanager.hashcrypt_value(password)

    assert configmanager.crypt.crypt(password, _salt_of(hashvalue)) == hashvalue


def test_the_salt_says_sha512():
    """confluent picks the method, not the platform default, so pin it."""
    assert configmanager.hashcrypt_value(KNOWN_PASSWORD).startswith('$6$')
