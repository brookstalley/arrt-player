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
from collections import deque
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
#: guess from one address, under a ceiling for all of them together, which is
#: what makes a short floor tolerable for the token it guards.
MINIMUM_PASSWORD_LENGTH: Final[int] = 8

#: How long a login lasts.
SESSION_SECONDS: Final[int] = 30 * 24 * 60 * 60

#: The cookie's name.
COOKIE_NAME: Final[str] = "arrt_player_session"

#: The longest a throttled login waits.
MAXIMUM_THROTTLE_SECONDS: Final[float] = 60.0

#: The doubling past which the wait is the maximum anyway: 2**6 = 64 > 60.
_LARGEST_DOUBLING: Final[int] = 6

#: The ceiling on wrong passwords from every address together: at most this
#: many in any `CEILING_WINDOW_SECONDS`. **Well above one person's typing**: one
#: address that keeps guessing is held to about fifteen in ten minutes by its
#: own doubling, and a person who has forgotten the password tries a handful.
#: It is what bounds a guesser that can take many addresses on the LAN.
CEILING_FAILURES: Final[int] = 30
CEILING_WINDOW_SECONDS: Final[float] = 600.0


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
class _Run:
    """One address's run of wrong passwords."""

    failures: int = 0
    not_before: float = 0.0


@dataclass
class Throttle:
    """Slows a run of wrong passwords from one address, under a ceiling for all of them.

    Each failure from an address doubles that address's wait before its next
    attempt is heard, up to a minute; a right password from it resets it.
    **Per address, so a guesser cannot lock the owner out**: with one throttle
    for the process, every wrong guess also delayed the owner's right one. The
    page answers only to its own names (`server._known_hosts`), so a guesser
    must be on the LAN with an address of its own, and the owner's is not the
    one slowed. The ceiling (`CEILING_FAILURES`) holds a guesser with many
    addresses; reaching it makes every address wait, which is the price of the
    bound, and one far above what a person types.
    """

    monotonic: Callable[[], float]
    _runs: dict[str, _Run] = field(default_factory=dict)
    #: When the latest failures were charged, from any address, oldest first.
    #: No more than the ceiling is kept: the oldest of those is the one whose
    #: leaving the window lets the next attempt be heard.
    _recent: deque[float] = field(default_factory=lambda: deque(maxlen=CEILING_FAILURES))

    def wait_seconds(self, address: str) -> float:
        """How long until an attempt from `address` will be heard; 0 when it will be now."""
        now = self.monotonic()
        self._forget(now)
        run = self._runs.get(address)
        own = run.not_before - now if run is not None else 0.0
        everyone = self._recent[0] + CEILING_WINDOW_SECONDS - now if len(self._recent) >= CEILING_FAILURES else 0.0
        return max(0.0, own, everyone)

    def failed(self, address: str) -> None:
        now = self.monotonic()
        run = self._runs.setdefault(address, _Run())
        run.failures += 1
        # The exponent is capped before it is raised, not after: 2.0 ** 1024
        # overflows a float, which would turn the 1025th wrong guess into a
        # server error on every login, the right password's included.
        exponent = min(run.failures - 1, _LARGEST_DOUBLING)
        run.not_before = now + min(2.0**exponent, MAXIMUM_THROTTLE_SECONDS)
        self._recent.append(now)

    def succeeded(self, address: str) -> None:
        """A right password: this address starts again, and the charge its attempt made is given back."""
        self._runs.pop(address, None)
        # A login is charged as a failure before its password is checked, so
        # concurrent attempts cannot all pass the check; a right one gives that
        # charge back rather than count against the ceiling.
        if self._recent:
            self._recent.pop()

    def _forget(self, now: float) -> None:
        """Drop failures older than the ceiling's window, and runs that have been quiet as long.

        Without it the table grows by one entry for every address that ever
        guessed. A run is kept a whole window after its wait ends, so a guesser
        that pauses does not start again at a one-second wait.
        """
        while self._recent and self._recent[0] <= now - CEILING_WINDOW_SECONDS:
            self._recent.popleft()
        for address in [key for key, run in self._runs.items() if run.not_before <= now - CEILING_WINDOW_SECONDS]:
            del self._runs[address]
