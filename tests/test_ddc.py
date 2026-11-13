"""DDC/CI: the bytes, the replies and their retries, and finding a connector's bus.

The byte strings are the ones the reference LG exchanged on the Pi on 2026-11-13
(`hdmi-output-findings.md` § Controlling the monitor over the cable), not values
computed by this module, so a wrong checksum seed or byte order fails here
rather than on a monitor. `I2cTransport` itself is exercised only on the Pi.
"""

import errno
from pathlib import Path

import pytest
from fakes import drm_tree

from arrt_player.ddc import (
    BRIGHTNESS,
    REPLY_WAIT_SECONDS,
    Ddc,
    Garbled,
    NoAnswer,
    NoBus,
    Reading,
    Unsupported,
    bus_for,
    get_request,
    parse_reply,
    set_request,
)

# Exchanged with the LG: request, then its reply.
GET_BRIGHTNESS = bytes.fromhex("51820110ac")
BRIGHTNESS_10_OF_100 = bytes.fromhex("6e88020010000064000aca")
GET_CONTRAST = bytes.fromhex("51820112ae")
CONTRAST_70_OF_100 = bytes.fromhex("6e88020012000064004684")
INPUT_HDMI_1 = bytes.fromhex("6e880200600000120011d7")
UNSUPPORTED_0XDC = bytes.fromhex("6e880201dc01ffff000068")
SET_BRIGHTNESS_10 = bytes.fromhex("51840310000aa2")


# -- the bytes -----------------------------------------------------------------------


def test_the_requests_are_the_bytes_the_monitor_answered():
    assert get_request(BRIGHTNESS) == GET_BRIGHTNESS
    assert get_request(0x12) == GET_CONTRAST
    assert set_request(BRIGHTNESS, 10) == SET_BRIGHTNESS_10


def test_a_set_carries_its_value_high_byte_first():
    request = set_request(BRIGHTNESS, 0x0102)
    assert request[4:6] == b"\x01\x02"


@pytest.mark.parametrize("value", [-1, 0x10000])
def test_a_value_that_is_not_two_bytes_is_refused(value):
    with pytest.raises(ValueError, match="two bytes"):
        set_request(BRIGHTNESS, value)


@pytest.mark.parametrize(
    ("code", "reply", "expected"),
    [
        (BRIGHTNESS, BRIGHTNESS_10_OF_100, Reading(current=10, maximum=100)),
        (0x12, CONTRAST_70_OF_100, Reading(current=70, maximum=100)),
        # A maximum and a current that differ in both bytes, so a swapped pair shows.
        (0x60, INPUT_HDMI_1, Reading(current=0x11, maximum=0x12)),
    ],
)
def test_a_reply_reads_as_the_value_the_monitor_reported(code, reply, expected):
    assert parse_reply(code, reply) == expected


def test_an_unsupported_feature_is_told_apart_from_a_bad_reply():
    with pytest.raises(Unsupported):
        parse_reply(0xDC, UNSUPPORTED_0XDC)


def test_a_reply_that_fails_its_checksum_is_garbled_even_with_the_unsupported_flag():
    # The unsupported reply with its checksum broken. Believing the result byte
    # before the checksum would write a working monitor off.
    broken = UNSUPPORTED_0XDC[:-1] + bytes([UNSUPPORTED_0XDC[-1] ^ 0x01])
    with pytest.raises(Garbled, match="checksum"):
        parse_reply(0xDC, broken)


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        (BRIGHTNESS_10_OF_100[:-1], "10 bytes"),
        (bytes.fromhex("6e80be"), "3 bytes"),  # DDC/CI's null message: the display was not ready
        (CONTRAST_70_OF_100, "feature 0x12"),  # a well-formed reply for another feature
    ],
)
def test_a_reply_that_is_not_an_answer_to_this_request_is_garbled(reply, match):
    with pytest.raises(Garbled, match=match):
        parse_reply(BRIGHTNESS, reply)


def _resigned(reply: bytes) -> bytes:
    """`reply` with a correct checksum, so a test reaches the check after it."""
    body = reply[:-1]
    checksum = 0x50
    for byte in body:
        checksum ^= byte
    return body + bytes([checksum])


@pytest.mark.parametrize(
    ("position", "value", "match"),
    [
        (0, 0x6F, "not a VCP reply"),  # source address
        (1, 0x87, "not a VCP reply"),  # length
        (2, 0x07, "not a VCP reply"),  # opcode
        (3, 0x02, "result byte"),  # a result neither 0 nor 1
    ],
)
def test_a_correctly_signed_reply_with_a_wrong_header_is_garbled(position, value, match):
    reply = bytearray(BRIGHTNESS_10_OF_100)
    reply[position] = value
    with pytest.raises(Garbled, match=match):
        parse_reply(BRIGHTNESS, _resigned(bytes(reply)))


# -- the exchange ---------------------------------------------------------------------


class Wire:
    """A transport that records what was sent and plays back replies in turn."""

    def __init__(self, *replies: bytes) -> None:
        self.sent: list[bytes] = []
        self._replies = list(replies)
        self.closed = False

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def receive(self, length: int) -> bytes:
        return self._replies.pop(0)[:length]

    def close(self) -> None:
        self.closed = True


def test_a_read_sends_the_request_waits_and_reads_the_reply():
    wire = Wire(BRIGHTNESS_10_OF_100)
    pauses: list[float] = []

    reading = Ddc(wire, sleep=pauses.append).read(BRIGHTNESS)

    assert reading == Reading(current=10, maximum=100)
    assert wire.sent == [GET_BRIGHTNESS]
    assert pauses == [REPLY_WAIT_SECONDS]


def test_a_garbled_reply_is_asked_for_again():
    wire = Wire(b"\x6e\x80\xbe", BRIGHTNESS_10_OF_100)

    reading = Ddc(wire, sleep=lambda _: None).read(BRIGHTNESS)

    assert reading.current == 10
    assert wire.sent == [GET_BRIGHTNESS, GET_BRIGHTNESS]


def test_a_reply_garbled_on_every_try_gives_up_after_the_last():
    wire = Wire(*[b"\x6e\x80\xbe"] * 5)

    with pytest.raises(Garbled):
        Ddc(wire, sleep=lambda _: None, tries=3).read(BRIGHTNESS)

    assert len(wire.sent) == 3


def test_an_unsupported_feature_is_not_asked_again():
    wire = Wire(UNSUPPORTED_0XDC, BRIGHTNESS_10_OF_100)

    with pytest.raises(Unsupported):
        Ddc(wire, sleep=lambda _: None).read(0xDC)

    assert wire.sent == [get_request(0xDC)]


def test_a_read_needs_at_least_one_try():
    with pytest.raises(ValueError, match="at least one"):
        Ddc(Wire(), tries=0)


def test_a_write_sends_the_set_and_waits_before_anything_else():
    wire = Wire()
    pauses: list[float] = []

    Ddc(wire, sleep=pauses.append).write(BRIGHTNESS, 10)

    assert wire.sent == [SET_BRIGHTNESS_10]
    assert pauses == [REPLY_WAIT_SECONDS]


def test_closing_the_bus_closes_its_transport():
    wire = Wire()
    Ddc(wire).close()
    assert wire.closed


def test_a_failing_bus_is_an_oserror_not_a_garbled_reply():
    class Refusing:
        def send(self, data: bytes) -> None:
            raise PermissionError(errno.EACCES, "Permission denied", "/dev/i2c-20")

        def receive(self, length: int) -> bytes:
            raise AssertionError("never reached")

    with pytest.raises(PermissionError):
        Ddc(Refusing(), sleep=lambda _: None).read(BRIGHTNESS)


@pytest.mark.parametrize("code", [121, errno.ENXIO])
@pytest.mark.parametrize("on", ["send", "receive"])
def test_nothing_acknowledging_the_address_is_the_screens_answer_not_the_hosts(code, on):
    # EREMOTEIO is what the Pi's empty HDMI-A-2 answered, measured 2026-11-13.
    class Silent:
        def send(self, data: bytes) -> None:
            if on == "send":
                raise OSError(code, "Remote I/O error")

        def receive(self, length: int) -> bytes:
            raise OSError(code, "Remote I/O error")

    with pytest.raises(NoAnswer, match="nothing answers"):
        Ddc(Silent(), sleep=lambda _: None).read(BRIGHTNESS)


def test_a_write_nobody_acknowledges_is_no_answer_too():
    class Silent:
        def send(self, data: bytes) -> None:
            raise OSError(121, "Remote I/O error")

        def receive(self, length: int) -> bytes:
            raise AssertionError("never reached")

    with pytest.raises(NoAnswer):
        Ddc(Silent(), sleep=lambda _: None).write(BRIGHTNESS, 10)


# -- the bus --------------------------------------------------------------------------


def _with_ddc(root: Path, connector: str, bus: str) -> None:
    # The kernel's link is relative and deep; only its last component names the bus.
    (root / connector / "ddc").symlink_to(f"../../../../soc/fef04500.i2c/{bus}")


def test_the_bus_is_read_from_the_connectors_ddc_link(tmp_path):
    root = drm_tree(
        tmp_path / "drm",
        {"card1-HDMI-A-1": ("connected", "3840x2160\n"), "card1-HDMI-A-2": ("disconnected", "")},
    )
    _with_ddc(root, "card1-HDMI-A-1", "i2c-20")
    _with_ddc(root, "card1-HDMI-A-2", "i2c-21")

    assert bus_for("hdmi-a-1", drm_root=root, dev_root=Path("/dev")) == Path("/dev/i2c-20")
    assert bus_for("hdmi-a-2", drm_root=root, dev_root=Path("/dev")) == Path("/dev/i2c-21")


def test_the_first_card_listing_the_socket_is_the_one_used(tmp_path):
    root = drm_tree(
        tmp_path / "drm",
        {"card0-HDMI-A-1": ("connected", "1920x1080\n"), "card1-HDMI-A-1": ("connected", "1920x1080\n")},
    )
    _with_ddc(root, "card0-HDMI-A-1", "i2c-3")
    _with_ddc(root, "card1-HDMI-A-1", "i2c-20")

    assert bus_for("hdmi-a-1", drm_root=root).name == "i2c-3"


def test_a_connector_with_no_ddc_link_has_no_bus(tmp_path):
    root = drm_tree(tmp_path / "drm", {"card1-HDMI-A-1": ("connected", "1920x1080\n")})

    with pytest.raises(NoBus, match="card1-HDMI-A-1 has no DDC bus"):
        bus_for("hdmi-a-1", drm_root=root)


def test_an_output_the_kernel_does_not_list_has_no_bus(tmp_path):
    root = drm_tree(tmp_path / "drm", {"card1-HDMI-A-1": ("connected", "1920x1080\n")})
    _with_ddc(root, "card1-HDMI-A-1", "i2c-20")

    with pytest.raises(NoBus, match="no connector for hdmi-a-2"):
        bus_for("hdmi-a-2", drm_root=root)


def test_a_host_with_no_drm_directory_has_no_bus(tmp_path):
    with pytest.raises(NoBus, match="cannot list"):
        bus_for("hdmi-a-1", drm_root=tmp_path / "absent")
