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
has no brightness control (`ddc.Unsupported`) is not asked again until it is
replaced. One where nothing acknowledges the address (`ddc.NoAnswer`: no DDC/CI,
or asleep) is asked again on the next interval, quietly. A fault of this host (a
missing `i2c` group, an unloaded `i2c-dev`) is said once per episode and tried
again on the next interval, and never stops the wall.
"""

import asyncio
import logging
from collections.abc import Callable

from arrt_player import brightness as brightness_module
from arrt_player.config import Location, ScreenBrightnessSettings
from arrt_player.ddc import BRIGHTNESS, DdcBus, Garbled, NoAnswer, NoBus, Unsupported
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
        sun = brightness_module.sun_state(
            self._clock.now(),
            latitude=self._location.latitude,
            longitude=self._location.longitude,
            location_name=self._location.name,
            location_region=self._location.region,
        )
        target = brightness_module.television_brightness(
            sun.relative_brightness,
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
            if self._silent.begin():
                self._said_unsupported(str(exc))
            return
        except (Garbled, NoBus, OSError) as exc:
            self._failed(exc)
            return
        self._answered()
        if outcome is not None:
            self._said_set(*outcome, solar_angle=sun.solar_angle_degrees)

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
        self._silent.end()
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
        self._drop_the_bus()

    def _set(self, target: int) -> tuple[int, int] | None:
        """Write `target`, capped at the monitor's maximum, if it is not what this Player last wrote.

        Returns the value written and what the monitor then reads, or None when
        nothing needed writing. Runs in a thread: each exchange waits on the bus.
        """
        if self._bus is None:
            self._bus = self._open_bus()
        # Read first, every time: it is how a screen that stopped answering is
        # noticed, and the only way to learn this monitor's maximum.
        reading = self._bus.read(BRIGHTNESS)
        value = min(target, reading.maximum)
        if value == self._written:
            return None
        self._bus.write(BRIGHTNESS, value)
        self._written = value
        # The monitor sends nothing back to a set, so whether it took the value
        # is learned only by asking.
        return value, self._bus.read(BRIGHTNESS).current

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
                log.debug("closing the DDC bus of %s failed (%s)", self._output, exc, extra={"event": "screen.bus_close_failed"})
            self._bus = None
