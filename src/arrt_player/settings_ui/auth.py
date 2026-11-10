"""The settings page's password, its session cookie, its setup code and its throttle.

All stdlib. The password is kept as a scrypt hash, and the parameters travel inside
the encoding, so a later change of cost still verifies the passwords already
stored. The cookie is an expiry signed with HMAC under the stored session secret,
so it survives the restart a save causes, and changing the password (which mints
a new secret) ends every session at once.

**Nothing here logs.** A password, its hash, the secret and a cookie are all
credentials. The one value that is printed is the setup code, and only by the
entry point (`__main__`): it is valid only while no password is set, it changes
on every start, and it stops working the moment a password is set.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

#: scrypt's cost. 2**14 with r=8 is about 16 MiB and a few tenths of a second on
#: a Pi 4, which is fine for a page one person logs in to.
SCRYPT_N: Final[int] = 2**14
SCRYPT_R: Final[int] = 8
SCRYPT_P: Final[int] = 1

#: The shortest password the page accepts: 8, the owner's choice (2026-11-10).
#: The page sits on a LAN behind a throttle that doubles to a minute per wrong
#: guess, which is what makes a short floor tolerable for a client token.
MINIMUM_PASSWORD_LENGTH: Final[int] = 8

#: How long a login lasts.
SESSION_SECONDS: Final[int] = 30 * 24 * 60 * 60

#: The cookie's name.
COOKIE_NAME: Final[str] = "arrt_player_session"

#: The longest a throttled login waits.
MAXIMUM_THROTTLE_SECONDS: Final[float] = 60.0

#: The doubling past which the wait is the maximum anyway: 2**6 = 64 > 60.
_LARGEST_DOUBLING: Final[int] = 6


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    """Whether `password` is the one `encoded` was made from. A malformed encoding matches nothing."""
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(digest))


def new_session_secret() -> str:
    return secrets.token_hex(32)


def new_setup_code() -> str:
    return secrets.token_urlsafe(12)


def code_matches(offered: str, expected: str) -> bool:
    return hmac.compare_digest(offered.encode(), expected.encode())


def _signature(secret: str, expiry: int) -> str:
    return hmac.new(bytes.fromhex(secret), str(expiry).encode(), hashlib.sha256).hexdigest()


def session_cookie(secret: str, *, now: float) -> str:
    expiry = int(now) + SESSION_SECONDS
    return f"{expiry}.{_signature(secret, expiry)}"


def session_valid(cookie: str | None, secret: str | None, *, now: float) -> bool:
    """Whether the cookie was signed under `secret` and has not expired."""
    if not cookie or not secret:
        return False
    expiry_text, _, signature = cookie.partition(".")
    try:
        expiry = int(expiry_text)
        expected = _signature(secret, expiry)
    except ValueError:
        return False
    return hmac.compare_digest(signature, expected) and expiry > now


@dataclass
class Throttle:
    """Slows a run of wrong passwords, per process.

    Each failure doubles the wait before the next attempt is heard, up to a
    minute; a right password resets it. Per process, not per address: the page
    has one user, and an address is the attacker's to choose.
    """

    monotonic: Callable[[], float]
    failures: int = 0
    _not_before: float = field(default=0.0)

    def wait_seconds(self) -> float:
        """How long until an attempt will be heard; 0 when it will be now."""
        return max(0.0, self._not_before - self.monotonic())

    def failed(self) -> None:
        self.failures += 1
        # The exponent is capped before it is raised, not after: 2.0 ** 1024
        # overflows a float, which would turn the 1025th wrong guess into a
        # server error on every login, the right password's included.
        exponent = min(self.failures - 1, _LARGEST_DOUBLING)
        self._not_before = self.monotonic() + min(2.0**exponent, MAXIMUM_THROTTLE_SECONDS)

    def succeeded(self) -> None:
        self.failures = 0
        self._not_before = 0.0
