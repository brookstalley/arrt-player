"""A monitor's own controls over its HDMI cable: DDC/CI, from Python, with no `ddcutil`.

A monitor that speaks DDC/CI answers on the I2C bus that runs inside the HDMI
cable, at address 0x37, and exposes its controls as numbered VCP features
(brightness is `0x10`). This module reads and sets one feature at a time. It
knows nothing about the sun or a wall; `displays/screen.py` decides what to set
and when.

**No `ddcutil`.** Reading and setting a feature is a write, a wait and a read of
eleven bytes, and the owner chose the Player's own code over another package and
program on every Pi (owner, 2026-11-13). The protocol below was measured against
the reference LG before this was written (`hdmi-output-findings.md`
§ Controlling the monitor over the cable), and the tests hold it to the bytes the
monitor actually sent.

**The bus is found from the connector, never assumed.** The kernel links each
connector's sysfs directory to its DDC bus (`card1-HDMI-A-1/ddc` → `i2c-20`), and
reading that link is the only way this module learns a bus number.

**Four ways a reading can fail, kept apart.** A monitor that does not support a
feature says so in a well-formed reply (`Unsupported`), and that is an answer
about the screen, never retried. Nothing acknowledging the display's address
(`NoAnswer`) is about the screen too: nothing plugged in (measured, `EREMOTEIO`
on the Pi's empty connector), a screen with no DDC/CI, or one asleep. A reply
that fails its checks (`Garbled`) is retried, because DDC/CI over a long cable
drops bytes. Any other `OSError` is about this host: a missing `i2c` group, or an
unloaded `i2c-dev` module. Treating a screen's answer like the host's fault would
keep hammering a TV that will never answer, and treating the host's fault like
the screen's would write a working monitor off.
"""

import errno
import fcntl
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from arrt_player.client import DRM_ROOT

#: The feature code for a monitor's brightness (MCCS).
BRIGHTNESS: Final[int] = 0x10

#: The display's DDC/CI address on the bus.
DISPLAY_ADDRESS: Final[int] = 0x37
#: `ioctl` request that binds an open I2C device to one peer address (`linux/i2c-dev.h`).
I2C_SLAVE: Final[int] = 0x0703

#: A request's checksum is seeded with the display's write address (0x37 << 1).
_REQUEST_SEED: Final[int] = 0x6E
#: A reply's checksum is seeded with the host's virtual address, 0x50. Measured:
#: it held on every reply the LG sent.
_REPLY_SEED: Final[int] = 0x50
#: The source-address byte a host puts at the head of every request.
_HOST: Final[int] = 0x51

_GET_VCP: Final[int] = 0x01
_SET_VCP: Final[int] = 0x03
_VCP_REPLY: Final[int] = 0x02
#: The only reply this module reads: source, length, opcode, result, code,
#: type, maximum (2), current (2) and checksum.
_REPLY_LENGTH: Final[int] = 11
#: A VCP value is two bytes on the wire.
_MAX_VALUE: Final[int] = 0xFFFF

#: How long a display gets between a request and the read of its reply. The
#: specification asks for 40 ms; 50 ms was measured to work every time on the LG.
REPLY_WAIT_SECONDS: Final[float] = 0.05
#: How many times a garbled reply is asked for again before the read gives up.
DEFAULT_TRIES: Final[int] = 3

_CONNECTOR_DIR: Final = re.compile(r"card\d+-HDMI-A-(\d+)")


class Unsupported(Exception):
    """The monitor answered, and said it has no such feature. A fact about the screen."""


class Garbled(Exception):
    """A reply that failed its checks: a short read, a wrong header, a bad checksum."""


class NoAnswer(Exception):
    """Nothing on the bus acknowledged the display's address. A fact about the screen."""


#: Linux's `EREMOTEIO`, by number: the Pi reported 121 (measured), and Python's
#: `errno` has no such name on macOS, where the suite also runs.
EREMOTEIO: Final[int] = 121
#: What an I2C driver reports when no device acknowledges the address: Linux's
#: drivers use either, and the Pi's uses `EREMOTEIO`.
_NOBODY_THERE: Final[frozenset[int]] = frozenset({EREMOTEIO, errno.ENXIO})


class NoBus(Exception):
    """The connector has no DDC bus the kernel will name."""


@dataclass(frozen=True)
class Reading:
    """A feature's value as the monitor reported it, on the monitor's own scale."""

    current: int
    maximum: int


def _checksum(seed: int, data: bytes) -> int:
    result = seed
    for byte in data:
        result ^= byte
    return result


def get_request(code: int) -> bytes:
    """The bytes that ask the monitor for one feature's value."""
    body = bytes([_HOST, 0x80 | 2, _GET_VCP, code])
    return body + bytes([_checksum(_REQUEST_SEED, body)])


def set_request(code: int, value: int) -> bytes:
    """The bytes that set one feature. The monitor sends no reply to a set."""
    if not 0 <= value <= _MAX_VALUE:
        raise ValueError(f"a VCP value is two bytes, and {value} is not")
    body = bytes([_HOST, 0x80 | 4, _SET_VCP, code, value >> 8, value & 0xFF])
    return body + bytes([_checksum(_REQUEST_SEED, body)])


def parse_reply(code: int, reply: bytes) -> Reading:
    """The monitor's answer to `get_request(code)`, or why it is not one.

    **Every check is made before the result byte is believed.** A reply whose
    checksum fails could carry a result byte of 1 by accident, and that would
    write a working monitor off as unsupported.
    """
    if len(reply) != _REPLY_LENGTH:
        raise Garbled(f"a reply of {len(reply)} bytes, not {_REPLY_LENGTH}")
    if _checksum(_REPLY_SEED, reply[:-1]) != reply[-1]:
        raise Garbled(f"a reply whose checksum fails ({reply.hex()})")
    if reply[0] != _REQUEST_SEED or reply[1] != 0x80 | (_REPLY_LENGTH - 3) or reply[2] != _VCP_REPLY:
        raise Garbled(f"a reply that is not a VCP reply ({reply.hex()})")
    if reply[4] != code:
        raise Garbled(f"a reply for feature 0x{reply[4]:02x}, not 0x{code:02x}")
    if reply[3] == 1:
        raise Unsupported(f"the monitor does not support feature 0x{code:02x}")
    if reply[3] != 0:
        raise Garbled(f"a reply with result byte 0x{reply[3]:02x}")
    return Reading(current=reply[8] << 8 | reply[9], maximum=reply[6] << 8 | reply[7])


class Transport(Protocol):
    """Raw bytes to and from the display, and nothing about what they mean."""

    def send(self, data: bytes) -> None: ...

    def receive(self, length: int) -> bytes: ...

    def close(self) -> None: ...


class DdcBus(Protocol):
    """One monitor's features: what `displays/screen_brightness.py` drives, and what the suite doubles."""

    def read(self, code: int) -> Reading: ...

    def write(self, code: int, value: int) -> None: ...

    def close(self) -> None: ...


class Ddc:
    """DDC/CI over a transport: the request, the wait, the reply and its retries."""

    def __init__(
        self,
        transport: Transport,
        *,
        sleep: Callable[[float], None] = time.sleep,
        tries: int = DEFAULT_TRIES,
    ) -> None:
        if tries < 1:
            raise ValueError(f"a read needs at least one try, not {tries}")
        self._transport = transport
        self._sleep = sleep
        self._tries = tries

    def read(self, code: int) -> Reading:
        """The feature's value. Raises `Unsupported`, `NoAnswer`, `Garbled` after every try, or `OSError`."""
        attempt = 1
        while True:
            self._send(get_request(code))
            self._sleep(REPLY_WAIT_SECONDS)
            try:
                return parse_reply(code, self._receive(_REPLY_LENGTH))
            except Garbled:
                if attempt >= self._tries:
                    raise
                attempt += 1
                # The display gets the same pause before it is asked again.
                self._sleep(REPLY_WAIT_SECONDS)

    def write(self, code: int, value: int) -> None:
        """Set the feature. The monitor sends nothing back, so the caller reads to confirm."""
        self._send(set_request(code, value))
        # The display is not ready for the next request until this has passed.
        self._sleep(REPLY_WAIT_SECONDS)

    def close(self) -> None:
        self._transport.close()

    def _send(self, data: bytes) -> None:
        try:
            self._transport.send(data)
        except OSError as exc:
            raise _screen_or_host(exc) from exc

    def _receive(self, length: int) -> bytes:
        try:
            return self._transport.receive(length)
        except OSError as exc:
            raise _screen_or_host(exc) from exc


def _screen_or_host(exc: OSError) -> Exception:
    """`NoAnswer` when nothing acknowledged the address, and the error itself otherwise."""
    if exc.errno in _NOBODY_THERE:
        return NoAnswer(f"nothing answers at the display's address ({exc})")
    return exc


class I2cTransport:
    """The bus device, opened on first use and bound to the display's address.

    Opening needs read and write on the device, which is `root:i2c 0660` on
    Raspberry Pi OS: the Player's account is in group `i2c` for this. The device
    exists only while `i2c-dev` is loaded (`deploy/README.md`).
    """

    def __init__(self, device: Path) -> None:
        self._device = device
        self._fd: int | None = None

    @property
    def device(self) -> Path:
        return self._device

    def send(self, data: bytes) -> None:
        written = os.write(self._open(), data)
        if written != len(data):
            raise OSError(f"wrote {written} of {len(data)} bytes to {self._device}")

    def receive(self, length: int) -> bytes:
        return os.read(self._open(), length)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _open(self) -> int:
        if self._fd is None:
            fd = os.open(self._device, os.O_RDWR)
            try:
                fcntl.ioctl(fd, I2C_SLAVE, DISPLAY_ADDRESS)
            except OSError:
                os.close(fd)
                raise
            self._fd = fd
        return self._fd


def bus_for(output: str, *, drm_root: Path = DRM_ROOT, dev_root: Path = Path("/dev")) -> Path:
    """The DDC bus device for an output named as the client names it (`hdmi-a-1`).

    The connector directory is matched as `client.hdmi_outputs` matches it: by
    the socket number, on the first card that lists one. Raises `NoBus` when the
    output is not listed or the kernel links no DDC bus to it.
    """
    number = output.removeprefix("hdmi-a-")
    try:
        directories = sorted(drm_root.iterdir())
    except OSError as exc:
        raise NoBus(f"cannot list {drm_root} ({exc})") from exc
    for directory in directories:
        matched = _CONNECTOR_DIR.fullmatch(directory.name)
        if matched is None or matched.group(1) != number:
            continue
        try:
            target = (directory / "ddc").readlink()
        except OSError as exc:
            raise NoBus(f"{directory.name} has no DDC bus ({exc})") from exc
        return dev_root / target.name
    raise NoBus(f"no connector for {output} under {drm_root}")
