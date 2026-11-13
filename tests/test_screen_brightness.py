"""An HDMI screen's brightness following the sun (#34), through the wall that drives it.

Driven one `tick()` at a time, as `test_screen.py` drives the screen, with a
`FakeMonitor` behind the DDC/CI seam. The location is on the equator at the
prime meridian, so the clock's noon UTC is the sun's noon there and its
midnight is night: the curve's two ends, 50 and 5, are the values asserted.
Steps are not multiples of the interval, so a write made early cannot pass for
one correctly held back.
"""

import asyncio
import errno
import logging
import threading
from dataclasses import replace

import pytest
from fakes import FakeMonitor, RecordingOutput

from arrt_player import __main__ as entry
from arrt_player.config import Location, ScreenBrightnessSettings
from arrt_player.ddc import Garbled, NoBus
from arrt_player.displays.screen import screen_wall
from arrt_player.displays.screen_brightness import ScreenBrightness
from arrt_player.manifest import Watcher
from arrt_player.wall import Wall

EQUATOR: Location = Location(latitude=0.0, longitude=0.0, name="Null Island", region="")
SETTINGS = ScreenBrightnessSettings(location=EQUATOR, minimum=5, maximum=50, interval_seconds=300.0)
#: Past the interval, by an amount no multiple of it.
PAST_THE_INTERVAL = 307.3
#: From the clock's noon UTC to midnight, and not a whole number of intervals.
TO_MIDNIGHT = 12 * 3600 + 11.0


@pytest.fixture
def wall(client_settings, wall_dir):
    return client_settings.wall("living-room")


@pytest.fixture
def output() -> RecordingOutput:
    return RecordingOutput()


@pytest.fixture
def monitor() -> FakeMonitor:
    return FakeMonitor(current=10, maximum=100)


@pytest.fixture
def opened(monitor) -> list[FakeMonitor]:
    """Every bus the brightness opened, so a test can see it reopened after a fault."""
    return []


@pytest.fixture
def brightness(wall, monitor, opened, clock) -> ScreenBrightness:
    def bus():
        opened.append(monitor)
        return monitor

    return ScreenBrightness(
        settings=SETTINGS,
        location=EQUATOR,
        output="hdmi-a-1",
        wall_id=wall.wall_id,
        bus=bus,
        clock=clock.as_clock(),
    )


@pytest.fixture
def screen(wall, output, clock, publish, brightness) -> Wall:
    publish.geometry = wall.geometry_for(output.screen)
    return screen_wall(
        wall=wall,
        output=output,
        watcher=Watcher(wall.manifest_path),
        clock=clock.as_clock(),
        brightness=brightness,
    )


def events(caplog, name: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.__dict__.get("event") == name]


# -- following the sun -------------------------------------------------------------------


async def test_at_noon_the_screen_is_set_to_the_day_level_and_at_midnight_to_the_night_level(
    screen, publish, monitor, clock, caplog
):
    publish(["w1"], interval_seconds=86400)

    with caplog.at_level(logging.INFO):
        await screen.tick()
        assert monitor.writes == [50]

        clock.advance(TO_MIDNIGHT)
        await screen.tick()

    assert monitor.writes == [50, 5]
    assert monitor.current == 5
    said = events(caplog, "screen.brightness_set")
    assert [(record.brightness, record.read_back, record.output) for record in said] == [
        (50, 50, "hdmi-a-1"),
        (5, 5, "hdmi-a-1"),
    ]
    assert all(record.levelno == logging.INFO for record in said)


async def test_the_sun_is_not_consulted_again_before_the_interval(screen, publish, monitor, clock):
    publish(["w1"], interval_seconds=86400)
    await screen.tick()
    reads = monitor.reads

    clock.advance(SETTINGS.interval_seconds - 0.7)
    await screen.tick()
    assert monitor.reads == reads, "the monitor was asked again before the interval"

    clock.advance(1.4)
    await screen.tick()
    assert monitor.reads > reads


async def test_a_brightness_set_by_hand_stays_until_the_curve_moves(screen, publish, monitor, clock):
    """Owner, 2026-11-13: written only when the computed value changes, as on the Frame."""
    publish(["w1"], interval_seconds=86400)
    await screen.tick()
    assert monitor.writes == [50]

    monitor.current = 30  # somebody pressed the monitor's own buttons
    for _ in range(3):
        clock.advance(PAST_THE_INTERVAL)
        await screen.tick()
    assert monitor.writes == [50]
    assert monitor.current == 30

    clock.advance(TO_MIDNIGHT)
    await screen.tick()
    assert monitor.writes == [50, 5]


async def test_a_monitor_whose_scale_ends_below_the_day_level_is_set_to_its_own_maximum(screen, publish, monitor):
    monitor.maximum = 40
    publish(["w1"], interval_seconds=86400)

    await screen.tick()

    assert monitor.writes == [40]


async def test_a_monitor_that_keeps_its_old_value_after_a_write_is_said_as_a_warning(screen, publish, monitor, caplog):
    monitor.ignores_writes = True
    publish(["w1"], interval_seconds=86400)

    with caplog.at_level(logging.INFO):
        await screen.tick()

    [said] = events(caplog, "screen.brightness_set")
    assert (said.brightness, said.read_back) == (50, 10)
    assert said.levelno == logging.WARNING


# -- what is left alone ----------------------------------------------------------------------


async def test_a_screen_showing_nothing_of_this_walls_is_not_written(screen, monitor, clock):
    # No feed: the screen is connected and dark.
    for _ in range(2):
        await screen.tick()
        clock.advance(PAST_THE_INTERVAL)

    assert monitor.writes == []
    assert monitor.reads == 0


async def test_a_disconnected_screen_is_not_written(screen, publish, output, monitor, clock):
    publish(["w1"], interval_seconds=86400)
    output.connected = False

    for _ in range(2):
        await screen.tick()
        clock.advance(PAST_THE_INTERVAL)

    assert monitor.writes == []


async def test_a_screen_plugged_back_in_is_asked_afresh_and_set_at_once(screen, publish, output, monitor, clock):
    """The screen that comes back need not be the one that left, so nothing about the old one carries over."""
    publish(["w1"], interval_seconds=86400)
    await screen.tick()
    assert monitor.writes == [50]

    output.connected = False
    clock.advance(11.3)
    await screen.tick()
    output.connected = True
    monitor.current = 80  # another monitor, at its own setting
    clock.advance(9.1)  # well inside the interval
    await screen.tick()

    assert monitor.writes == [50, 50]
    assert monitor.closed == 1, "the old screen's bus was kept open"


async def test_a_screen_with_no_brightness_control_is_said_once_and_never_asked_again(screen, publish, monitor, clock, caplog):
    monitor.unsupported = True
    publish(["w1"], interval_seconds=86400)

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            await screen.tick()
            clock.advance(PAST_THE_INTERVAL)

    assert monitor.writes == []
    assert monitor.reads == 1
    [said] = events(caplog, "screen.brightness_unsupported")
    assert "does not support" in said.reason


async def test_a_screen_nobody_answers_for_is_said_once_and_asked_again_each_interval(screen, publish, monitor, clock, caplog):
    monitor.silent = True
    publish(["w1"], interval_seconds=86400)

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            await screen.tick()
            clock.advance(PAST_THE_INTERVAL)
        assert monitor.writes == []

        monitor.silent = False  # it woke, or its DDC/CI was switched on
        await screen.tick()

    assert len(events(caplog, "screen.brightness_no_answer")) == 1
    assert events(caplog, "screen.brightness_unsupported") == [], "an asleep screen was written off as unsupported"
    assert len(events(caplog, "screen.brightness_answering")) == 1
    assert monitor.writes == [50]


# -- this host's faults ------------------------------------------------------------------------


async def test_a_bus_this_host_cannot_open_is_said_once_retried_and_its_recovery_said(
    screen, publish, monitor, opened, clock, caplog
):
    monitor.fails = PermissionError(errno.EACCES, "Permission denied", "/dev/i2c-20")
    publish(["w1"], interval_seconds=86400)

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            await screen.tick()
            clock.advance(PAST_THE_INTERVAL)
        assert monitor.writes == []

        monitor.fails = None
        await screen.tick()

    [failed] = events(caplog, "screen.brightness_failed")
    assert failed.levelno == logging.WARNING
    assert "Permission denied" in failed.error
    assert len(events(caplog, "screen.brightness_recovered")) == 1
    assert monitor.writes == [50]
    assert len(opened) == 4, "a bus that failed was not reopened on the next try"


@pytest.mark.parametrize(
    "fault",
    [Garbled("a reply whose checksum fails"), OSError(errno.EIO, "Input/output error")],
    ids=["garbled", "host"],
)
async def test_a_garbled_bus_or_a_host_fault_is_said_once_and_tried_again(screen, publish, monitor, clock, caplog, fault):
    monitor.fails = fault
    publish(["w1"], interval_seconds=86400)

    with caplog.at_level(logging.INFO):
        for _ in range(2):
            await screen.tick()
            clock.advance(PAST_THE_INTERVAL)
        monitor.fails = None
        await screen.tick()

    assert len(events(caplog, "screen.brightness_failed")) == 1
    assert monitor.writes == [50]


async def test_a_connector_with_no_bus_is_said_once_and_never_stops_the_wall(wall, output, clock, publish, caplog):
    def no_bus():
        raise NoBus("card1-HDMI-A-1 has no DDC bus")

    brightness = ScreenBrightness(
        settings=SETTINGS, location=EQUATOR, output="hdmi-a-1", wall_id=wall.wall_id, bus=no_bus, clock=clock.as_clock()
    )
    publish.geometry = wall.geometry_for(output.screen)
    screen = screen_wall(
        wall=wall, output=output, watcher=Watcher(wall.manifest_path), clock=clock.as_clock(), brightness=brightness
    )
    publish(["w1", "w2"], interval_seconds=60)

    with caplog.at_level(logging.INFO):
        await screen.tick()
        clock.advance(PAST_THE_INTERVAL)
        await screen.tick()

    [failed] = events(caplog, "screen.brightness_failed")
    assert "no DDC bus" in failed.error
    assert len(output.shown) == 2


async def test_a_write_whose_read_back_fails_is_written_again_at_the_next_interval(screen, publish, monitor, clock):
    """The monitor says nothing to a set, so a write is taken as done only once it reads back."""
    publish(["w1"], interval_seconds=86400)
    reads = 0

    def flaky_read(code):
        nonlocal reads
        reads += 1
        if reads == 2:  # the read-back of the first write
            raise Garbled("a reply whose checksum fails")
        return FakeMonitor.read(monitor, code)

    monitor.read = flaky_read
    await screen.tick()
    assert monitor.writes == [50]

    clock.advance(PAST_THE_INTERVAL)
    await screen.tick()

    assert monitor.writes == [50, 50]


async def test_a_monitor_reporting_a_scale_of_nothing_is_never_written(screen, publish, monitor, clock, caplog):
    monitor.maximum = 0
    publish(["w1"], interval_seconds=86400)

    with caplog.at_level(logging.INFO):
        for _ in range(2):
            await screen.tick()
            clock.advance(PAST_THE_INTERVAL)

    assert monitor.writes == []
    [said] = events(caplog, "screen.brightness_unsupported")
    assert "scale of 0 to 0" in said.reason


async def test_closing_waits_for_an_exchange_in_flight_and_nothing_reopens_the_bus(brightness, monitor, opened, clock):
    """Cancelling a thread's await does not stop the thread, so close must not pull the bus from under it."""
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def slow_read(code):
        entered.set()
        release.wait(5)
        order.append("read")
        return FakeMonitor.read(monitor, code)

    monitor.read = slow_read
    applying = asyncio.create_task(brightness.apply(present=True, showing=True))
    await asyncio.to_thread(entered.wait, 5)

    closing = asyncio.create_task(asyncio.to_thread(lambda: (brightness.close(), order.append("closed"))))
    await asyncio.sleep(0.05)
    assert order == [], "the bus was closed while an exchange was still using it"
    release.set()
    await applying
    await closing

    assert order[-1] == "closed"
    assert order.count("read") >= 1
    assert monitor.closed == 1
    clock.advance(PAST_THE_INTERVAL)  # due again, so only the close can keep the bus shut
    await brightness.apply(present=True, showing=True)
    assert len(opened) == 1, "a closed brightness opened the bus again"


async def test_a_bus_that_will_not_close_is_said_where_the_journal_shows_it(screen, publish, monitor, caplog):
    def refuse():
        raise OSError(errno.EIO, "Input/output error")

    monitor.close = refuse
    publish(["w1"], interval_seconds=86400)
    await screen.tick()
    stop = asyncio.Event()
    stop.set()

    with caplog.at_level(logging.INFO):
        await screen.run(stop)

    [said] = events(caplog, "screen.bus_close_failed")
    assert said.levelno == logging.WARNING


async def test_a_brightness_fault_never_stops_the_wall(screen, publish, monitor, output, clock):
    monitor.fails = OSError(errno.EIO, "Input/output error")
    publish(["w1", "w2"], interval_seconds=60)

    await screen.tick()
    clock.advance(61.3)
    await screen.tick()

    assert len(output.shown) == 2


async def test_a_wall_that_stops_closes_the_bus(screen, publish, monitor):
    publish(["w1"], interval_seconds=86400)
    await screen.tick()
    stop = asyncio.Event()
    stop.set()

    await screen.run(stop)

    assert monitor.closed == 1


# -- the entry point ----------------------------------------------------------------------------


def test_no_location_or_the_switch_off_means_no_brightness(client_settings, clock):
    wall = client_settings.wall("living-room")
    no_location = ScreenBrightnessSettings(location=None)
    switched_off = replace(SETTINGS, enabled=False)

    assert entry.screen_brightness(wall, "hdmi-a-1", no_location, clock.as_clock()) is None
    assert entry.screen_brightness(wall, "hdmi-a-1", switched_off, clock.as_clock()) is None
    assert entry.screen_brightness(wall, "hdmi-a-1", None, clock.as_clock()) is None
    assert entry.screen_brightness(wall, "hdmi-a-1", SETTINGS, clock.as_clock()) is not None


async def test_a_screen_walls_worker_hands_its_brightness_to_the_wall(client_settings, monkeypatch):
    """`run_screen_wall` builds the brightness from the settings it is given and puts it on the wall."""
    handed: list[object] = []

    class Built:
        async def run(self, stop):
            return

    def recording_wall(**kwargs):
        handed.append(kwargs["brightness"])
        return Built()

    async def no_pull(wall, stop, loop):
        return

    monkeypatch.setattr(entry, "screen_output", lambda wall, output: RecordingOutput())
    monkeypatch.setattr(entry, "screen_wall", recording_wall)
    monkeypatch.setattr(entry, "_beside_its_pull", no_pull)
    chosen = ScreenBrightnessSettings(location=EQUATOR, minimum=17, maximum=23, interval_seconds=91.0)

    await entry.run_screen_wall(client_settings.wall("living-room"), "hdmi-a-1", asyncio.Event(), brightness=chosen)
    await entry.run_screen_wall(client_settings.wall("living-room"), "hdmi-a-1", asyncio.Event())

    [built, absent] = handed
    assert isinstance(built, ScreenBrightness)
    assert built._settings == chosen
    assert absent is None


def test_the_entry_point_opens_the_bus_of_the_walls_own_connector(client_settings, clock, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(entry, "screen_ddc", lambda output: asked.append(output) or FakeMonitor())
    wall = client_settings.wall("living-room")

    built = entry.screen_brightness(wall, "hdmi-a-2", SETTINGS, clock.as_clock())
    assert built is not None
    built._set(50)

    assert asked == ["hdmi-a-2"]


async def test_the_supervisor_hands_its_screen_brightness_to_a_screen_wall(client_settings, monkeypatch, tmp_path):
    """Through `run_wall`, with levels no default has, so an unwired setting cannot pass."""
    seen: list[ScreenBrightnessSettings | None] = []

    async def recording(wall, output, stop, *, clock=None, brightness=None):
        seen.append(brightness)

    monkeypatch.setattr(entry, "run_screen_wall", recording)
    chosen = ScreenBrightnessSettings(location=EQUATOR, minimum=17, maximum=23, interval_seconds=91.0)
    client = replace(client_settings, screen_brightness=chosen)
    report = entry.OutputReport(name="hdmi-a-1", kind="framebuffer", connected=True, screen=(1920, 1080))

    await entry.run_wall(client, client.wall("living-room"), report, stop=None)

    assert seen == [chosen]
