"""An HDMI screen's brightness, following the sun over DDC/CI (#34).

The Frame's brightness has followed the sun since 2024 (`brightness.py`); this is
the same curve for a monitor on an HDMI connector, mapped onto the monitor's own
scale between a night level and a day level (`ScreenBrightnessSettings`).

**Written only when the value changes**, exactly as the Frame is, so a brightness
somebody sets by hand on the monitor stays until the sun moves the value on
(owner, 2026-11-13). The monitor's current value is read on every pass, but only
to learn its scale and that it answers, never to "correct" a hand-set value.

**Nothing is written to a screen that is not showing this wall's art.** A screen
that is absent or dark is left alone, so a brightness write can never be what
wakes one. A screen that goes away and comes back is probed afresh, because the
screen that comes back need not be the one that left.

**A screen that does not answer is said once and left alone.** One that says it
has no brightness control (`ddc.Unsupported`, or a scale whose maximum is 0) is
not asked again until it is replaced. One where nothing acknowledges the
address (`ddc.NoAnswer`: no DDC/CI, or asleep) is said once, asked again on each
interval, and said again when it answers. A fault of this host (a missing `i2c`
group, an unloaded `i2c-dev`) is said once per episode and tried again on the
next interval, and never stops the wall.

**A value counts as written only once the monitor reads it back.** The monitor
sends nothing in reply to a set, so a write whose read-back fails is tried
again at the next interval rather than taken on trust.

**The exchange runs in a thread, and closing waits for it.** Cancelling an
`asyncio.to_thread` does not stop the thread, so without the lock a wall
stopping mid-exchange would close the device under it, and the thread would
write DDC bytes to whatever reused the descriptor.

**A brightness set by hand is overwritten when the Player restarts**, as the
Frame's is: what this Player last wrote lives in memory, and a restarted one
cannot tell a hand-set value from one it wrote itself.
"""

import asyncio
import logging
import threading
from collections.abc import Callable

from arrt_player import brightness as brightness_module
from arrt_player.config import Location, ScreenBrightnessSettings
from arrt_player.ddc import BRIGHTNESS, DdcBus, Garbled, NoAnswer, NoBus, Reading, Unsupported
from arrt_player.episodes import ReportOnce
from arrt_player.wall import Clock

log = logging.getLogger(__name__)


class ScreenBrightness:
    """One screen's brightness. `apply` is called on every pass of its wall."""

    def __init__(
        self,
        *,
        settings: ScreenBrightnessSettings,
        location: Location,
        output: str,
        wall_id: str,
        bus: Callable[[], DdcBus],
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._location = location
        self._output = output
        self._wall_id = wall_id
        self._open_bus = bus
        self._clock = clock
        self._bus: DdcBus | None = None
        #: When the sun was last consulted, on the monotonic clock.
        self._at: float | None = None
        #: The value this Player last wrote to the screen there now, or None.
        self._written: int | None = None
        #: The screen there now said it has no brightness control.
        self._unsupported = False
        self._silent = ReportOnce()
        self._failing = ReportOnce()
        #: Held for every exchange with the bus and for closing it, which run on
        #: different threads.
        self._lock = threading.Lock()
        self._closed = False

    async def apply(self, *, present: bool, showing: bool) -> None:
        """Follow the sun on a screen showing art. `present`: a screen is plugged in at all."""
        if not present:
            self._forget_the_screen()
        elif showing and not self._unsupported and self._due():
            await self._follow_the_sun()

    def _due(self) -> bool:
        elapsed = self._clock.monotonic()
        if self._at is not None and elapsed - self._at < self._settings.interval_seconds:
            return False
        self._at = elapsed
        return True

    async def _follow_the_sun(self) -> None:
        target, sun = brightness_module.level_at(
            self._clock.now(),
            latitude=self._location.latitude,
            longitude=self._location.longitude,
            location_name=self._location.name,
            location_region=self._location.region,
            minimum=self._settings.minimum,
            maximum=self._settings.maximum,
        )
        try:
            outcome = await asyncio.to_thread(self._set, target)
        except Unsupported as exc:
            self._unsupported = True
            self._said_unsupported(str(exc))
            return
        except NoAnswer as exc:
            self._no_answer(exc)
            return
        except (Garbled, NoBus, OSError) as exc:
            self._failed(exc)
            return
        self._answered()
        if outcome is not None:
            self._said_set(*outcome, solar_angle=sun.solar_angle_degrees)

    def _no_answer(self, exc: NoAnswer) -> None:
        if self._silent.begin():
            log.info(
                "nothing answers on the DDC/CI bus of %s (%s); asking again in %g s",
                self._output,
                exc,
                self._settings.interval_seconds,
                extra={"event": "screen.brightness_no_answer", "output": self._output, "wall_id": self._wall_id},
            )

    def _failed(self, exc: Exception) -> None:
        self._drop_the_bus()
        if self._failing.begin():
            log.warning(
                "could not set the brightness of %s (%s); trying again in %g s",
                self._output,
                exc,
                self._settings.interval_seconds,
                extra={
                    "event": "screen.brightness_failed",
                    "output": self._output,
                    "wall_id": self._wall_id,
                    "error": str(exc),
                },
            )

    def _answered(self) -> None:
        if self._silent.end():
            log.info(
                "the DDC/CI bus of %s answers again",
                self._output,
                extra={"event": "screen.brightness_answering", "output": self._output, "wall_id": self._wall_id},
            )
        if self._failing.end():
            log.info(
                "the brightness of %s can be set again",
                self._output,
                extra={"event": "screen.brightness_recovered", "output": self._output, "wall_id": self._wall_id},
            )

    def _said_set(self, value: int, read_back: int, *, solar_angle: float) -> None:
        # A monitor that takes the write and keeps its old value is worth a warning:
        # the curve would otherwise look applied in the journal and not on the wall.
        log.log(
            logging.INFO if read_back == value else logging.WARNING,
            "set the brightness of %s to %d (the monitor reads %d)",
            self._output,
            value,
            read_back,
            extra={
                "event": "screen.brightness_set",
                "output": self._output,
                "wall_id": self._wall_id,
                "brightness": value,
                "read_back": read_back,
                "solar_angle": round(solar_angle, 2),
            },
        )

    def close(self) -> None:
        """Let go of the bus, waiting for an exchange in flight to finish first."""
        with self._lock:
            self._closed = True
            self._drop_the_bus()

    def _set(self, target: int) -> tuple[int, int] | None:
        """Write `target`, capped at the monitor's maximum, if it is not what this Player last wrote.

        Returns the value written and what the monitor then reads, or None when
        nothing needed writing. Runs in a thread: each exchange waits on the bus.
        """
        with self._lock:
            if self._closed:
                return None
            if self._bus is None:
                self._bus = self._open_bus()
            # Read first, every time: it is how a screen that stopped answering
            # is noticed, and the only way to learn this monitor's maximum.
            reading = self._bus.read(BRIGHTNESS)
            value = min(target, _maximum_of(reading))
            if value == self._written:
                return None
            self._bus.write(BRIGHTNESS, value)
            # The monitor sends nothing back to a set, so whether it took the
            # value is learned only by asking; until then it is not written.
            read_back = self._bus.read(BRIGHTNESS).current
            self._written = value
            return value, read_back

    def _said_unsupported(self, reason: str) -> None:
        log.info(
            "leaving the brightness of %s alone: %s",
            self._output,
            reason,
            extra={"event": "screen.brightness_unsupported", "output": self._output, "wall_id": self._wall_id, "reason": reason},
        )

    def _forget_the_screen(self) -> None:
        """A screen went away: whatever comes back is asked afresh, and written to afresh."""
        self._drop_the_bus()
        self._at = None
        self._written = None
        self._unsupported = False
        self._silent.end()

    def _drop_the_bus(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except OSError as exc:
                # WARNING, not DEBUG: the journal runs at INFO, and a descriptor
                # that would not close is a leak somebody may need to find.
                log.warning(
                    "closing the DDC/CI bus of %s failed (%s)",
                    self._output,
                    exc,
                    extra={"event": "screen.bus_close_failed", "output": self._output, "wall_id": self._wall_id},
                )
            self._bus = None


def _maximum_of(reading: Reading) -> int:
    """The monitor's scale. A maximum of 0 is no scale at all, and is not written to."""
    if reading.maximum <= 0:
        raise Unsupported(f"the monitor reports a brightness scale of 0 to {reading.maximum}")
    return reading.maximum
