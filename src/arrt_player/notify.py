"""The Player's word to systemd: `READY=1` once it is up, `WATCHDOG=1` each time it has made progress.

The unit is `Type=notify` with a watchdog (`deploy/display.service`), so systemd
counts the Player started only once it says `READY=1`, and restarts it when no
`WATCHDOG=1` has come for `WatchdogSec`. This module speaks that protocol
(`sd_notify(3)`) itself: one datagram, a newline-separated list of assignments,
to the `AF_UNIX` socket `$NOTIFY_SOCKET` names, a path or, after an `@`, a name
in Linux's abstract namespace. No library is added for it.

**A pet means the Player made progress, never only that it is alive.** A timer
that pets proves only that timers fire, and a Player whose loop turns while
everything it does is stuck is the wedge the watchdog is for. So a pet is sent
only from the places `__main__` names as progress: the supervisor finished a
cycle, the slideshow reached its next step, and, in setup mode, which does
nothing but serve its page, the page is still being served by a loop that
turns (`Watchdog.while_running`). What each costs, and the longest honest gap
between two of them, is in the unit's comment.

**Without `NOTIFY_SOCKET` nothing is sent**: a Mac, a test, a run in a
terminal. That is said once, at DEBUG, because there it is the expected case.
A socket that will not take a message is said once at WARNING, because under
the unit systemd will restart a Player that cannot pet it.

**Nothing here raises or blocks.** The socket is non-blocking: a full queue at
systemd costs that one message, never a turn of the loop the walls run on.
"""

import asyncio
import logging
import os
import socket
import time
from collections.abc import Callable, Mapping
from typing import Final

from arrt_player.episodes import ReportOnce

log = logging.getLogger(__name__)

#: The variable systemd names its notification socket in.
NOTIFY_SOCKET: Final[str] = "NOTIFY_SOCKET"

#: The least time between two pets. Progress comes about once a second while a
#: slideshow holds an item (each check of the screen is a step), and systemd
#: needs a pet only once per `WatchdogSec`; this keeps the datagrams to one per
#: few seconds. A term of the unit's `WatchdogSec` arithmetic: a pet suppressed
#: by it can leave the last one sent up to this much older.
PET_MIN_SECONDS: Final[float] = 5.0

#: How often setup mode pets while its page is served. Setup mode has no cycle
#: of its own; this is the whole of its gap, well inside `WatchdogSec`.
SETUP_PET_SECONDS: Final[float] = 30.0


def socket_address(value: str) -> str | bytes | None:
    """The `AF_UNIX` address a `$NOTIFY_SOCKET` value names, or None for a form this does not speak.

    A value starting `/` is a path. One starting `@` is a name in the abstract
    namespace, which the kernel spells with a leading NUL byte. Anything else
    (systemd also knows `vsock:`, which the unit's address families refuse) is
    None.
    """
    if value.startswith("/"):
        return value
    if value.startswith("@") and len(value) > 1:
        return b"\0" + value[1:].encode()
    return None


class Notifier:
    """Sends notification datagrams to the socket `$NOTIFY_SOCKET` names. Never raises; never blocks."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        raw = (os.environ if environ is None else environ).get(NOTIFY_SOCKET, "")
        self._address = socket_address(raw) if raw else None
        self._unsent = ReportOnce()
        if not raw:
            log.debug(
                "NOTIFY_SOCKET is not set, so this Player is not run as a notify unit and tells systemd nothing",
                extra={"event": "notify.absent"},
            )
        elif self._address is None:
            log.warning(
                "NOTIFY_SOCKET is %r, which names no AF_UNIX socket; systemd is told nothing and will restart this Player",
                raw,
                extra={"event": "notify.unusable", "notify_socket": raw},
            )

    @property
    def active(self) -> bool:
        """Whether there is a socket to send to."""
        return self._address is not None

    def send(self, message: str) -> bool:
        """Send one notification (`READY=1`, `WATCHDOG=1`). True when the socket took it."""
        if self._address is None:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                # Non-blocking: a full queue at systemd costs this message, never a turn of the loop.
                channel.settimeout(0.0)
                channel.sendto(message.encode(), self._address)
        except OSError as exc:
            if self._unsent.begin():
                log.warning(
                    "systemd's notification socket refused %s (%s); a watchdog will restart this Player if this goes on",
                    message,
                    exc,
                    extra={"event": "notify.unsent", "notification": message},
                )
            return False
        if self._unsent.end():
            log.info("systemd's notification socket takes messages again", extra={"event": "notify.sent"})
        return True


class Watchdog:
    """`READY=1` once, and `WATCHDOG=1` on progress, at most once per `pet_min_seconds`."""

    def __init__(
        self,
        notifier: Notifier,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        pet_min_seconds: float = PET_MIN_SECONDS,
    ) -> None:
        self._notifier = notifier
        self._monotonic = monotonic
        self._pet_min = pet_min_seconds
        #: Whether `ready` was called, and whether systemd has been told.
        self._ready_wanted = False
        self._ready = False
        self._petted_at: float | None = None

    @classmethod
    def from_environment(cls) -> "Watchdog":
        """The process's watchdog, on the socket this process was started with, if any."""
        return cls(Notifier())

    @classmethod
    def silent(cls) -> "Watchdog":
        """One that sends nothing: for a caller that is not given the process's."""
        return cls(Notifier(environ={}))

    def ready(self) -> None:
        """Tell systemd the Player is up. Sent once; a socket that refused it is tried again at the next progress."""
        self._ready_wanted = True
        if not self._ready:
            self._ready = self._notifier.send("READY=1")

    def progress(self) -> None:
        """The Player made progress: pet the watchdog, unless one went less than `pet_min_seconds` ago."""
        if self._ready_wanted and not self._ready:
            self.ready()
        now = self._monotonic()
        if self._petted_at is not None and now - self._petted_at < self._pet_min:
            return
        if self._notifier.send("WATCHDOG=1"):
            self._petted_at = now

    async def while_running(self, task: "asyncio.Future[object]", *, every: float | None = None) -> None:
        """Count `task` still running, seen from this loop, as progress every `every` seconds, until it ends.

        **Setup mode's only progress**, and the weakest of the three: it proves
        the loop that serves the page turns and the page's task has not ended,
        not that a request was answered. Setup mode does nothing else, so a
        wedge there is a loop that has stopped turning, which stops this too.
        """
        period = SETUP_PET_SECONDS if every is None else every
        while not task.done():
            self.progress()
            await asyncio.wait({task}, timeout=period)
