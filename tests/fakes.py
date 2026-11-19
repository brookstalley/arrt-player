"""Doubles for the devices a Player draws on: the label panel, a screen, a monitor's DDC/CI.

Each models the device's *observable* behaviour and the ways it fails, armed on
demand, rather than its protocol: a label surface that refuses or hangs, a screen
that records every picture and every blank, a monitor that ignores a write. The
television's double has a module of its own (`fake_tv.py`) and is re-exported
here, so a test importing it from this module keeps working.
"""

import math
import threading
import time
from pathlib import Path
from typing import Final

from fake_tv import FOREIGN_IMAGE, FakeTv  # noqa: F401 -- re-exported, so no importer changes

from arrt_player.ddc import BRIGHTNESS, NoAnswer, Reading, Unsupported
from arrt_player.panel import (
    Extent,
    Geometry,
    LabelSurface,
    Layout,
    Line,
    Measure,
    SurfaceUnavailable,
    TypeScale,
    set_text,
    type_scale_for,
)

#: How long an armed-to-hang draw waits before giving up on being released. Long
#: enough that no passing test reaches it, short enough that a failing one does
#: not take the session with it.
RELEASE_TIMEOUT_SECONDS: Final[float] = 30.0


class FakeSurface(LabelSurface):
    """A label surface that records what it was given, and can fail like a real one.

    **Its failure is a raise, not a falsy return**, because that is what the
    driver seam guarantees: `omni-epd`'s `display()` returns None whether it
    worked or not, so the real surface must convert that into an exception or the
    panel's failure becomes a label silently months out of date. A fake that
    reported failure by returning would let a caller pass every test against a
    real one that cannot.
    """

    def __init__(
        self,
        *,
        width_px: int = 1448,
        height_px: int = 1072,
        margin_px: int = 40,
        type_scale: TypeScale | None = None,
    ) -> None:
        self._geometry = Geometry(width_px=width_px, height_px=height_px, margin_px=margin_px)
        #: **The reference wall's, derived rather than invented**, so a caller who
        #: does not care about type still gets sizes a real panel would produce —
        #: a fake carrying made-up numbers here would let a caller pass against
        #: proportions no device has.
        self._type_scale = type_scale or type_scale_for(
            width_px=1448, height_px=1072, diagonal_inches=6.0, viewing_distance_inches=84.0
        )
        #: Every layout this surface was asked to draw, in order.
        self.shown: list[Layout] = []
        self.closed = 0
        #: Armed failure: the panel refuses whatever it is handed.
        self.refuses = False
        #: Armed failure of a different kind: **measuring** blows up, with
        #: something that is not `SurfaceUnavailable`. That is not a hypothetical
        #: shape — the real surface's `measure` reaches Pango through C bindings,
        #: which raise GLib errors related to nothing this codebase can name, and
        #: it is read by the caller *outside* the `show` that converts failures.
        self.measurement_explodes = False
        #: How long a draw takes, in real seconds. **The real one is not
        #: instantaneous and no amount of arranging makes it so**: a full 16-level
        #: frame takes the time measured in `platform-and-dependency-findings.md`
        #: § The e-paper panel, with no partial refresh, so a fake that
        #: returned immediately would let a caller drawing on the event loop pass
        #: every test a caller drawing off it passes.
        self.draw_takes_seconds = 0.0
        #: Armed failure of the kind that raises nothing: the panel goes into a
        #: transaction and does not come out. It waits for `release`, which the
        #: fixture always sets — a thread parked in here outlives the test.
        self.blocks = False
        self.release = threading.Event()
        #: Where a draw currently is, for a test that needs to look at the world
        #: *during* one rather than after it.
        self.entered = threading.Event()
        self.left = threading.Event()
        #: How many draws have been started — as against `shown`, which counts the
        #: ones that finished. The two differ exactly when a draw is in flight or
        #: was abandoned, which is the whole subject of the caller's one-at-a-time
        #: gate.
        self.draws_begun = 0

    @property
    def geometry(self) -> Geometry:
        return self._geometry

    def resize(self, *, width_px: int, height_px: int, margin_px: int) -> None:
        """Change the usable area under a running daemon.

        **A geometry is a setting, so it can change while the process lives** —
        an operator edits the margin or the viewing distance and restarts
        nothing. That makes "the surface has no usable area" a condition that
        clears, and a fake fixed at construction cannot express the clearing.
        """
        self._geometry = Geometry(width_px=width_px, height_px=height_px, margin_px=margin_px)

    @property
    def type_scale(self) -> TypeScale:
        return self._type_scale

    @property
    def measure(self) -> Measure:
        return self._measure

    def _measure(self, line: Line, size_px: int, wrap_px: int) -> Extent:
        """Predictable metrics — a stand-in for a rasterizer, not a font.

        **Measures `set_text`, and a version of this that did not was latent for
        one commit.** A line is runs now, so `len()` over the argument counts a
        handful of `Run` objects rather than a line of characters: every label
        then measures one row tall however long the name is, and the drop rule
        can never fire through this surface. Nothing went red, because no test
        driving `FakeSurface` asserts a drop — which is exactly why the guard is
        the conversion here rather than a test somewhere noticing.
        """
        if self.measurement_explodes:
            raise RuntimeError("the text stack could not build a font map")
        text = set_text(line)
        glyph = max(1, size_px // 2)
        per_row = max(1, wrap_px // glyph)
        rows = max(1, math.ceil(len(text) / per_row))
        return Extent(width_px=min(len(text) * glyph, wrap_px), height_px=rows * size_px, rows=rows)

    def show(self, layout: Layout) -> None:
        self.draws_begun += 1
        self.left.clear()
        self.entered.set()
        try:
            if self.refuses:
                raise SurfaceUnavailable("the panel is not responding")
            if self.blocks:
                # **Bounded, though a real wedged panel is not.** A test that
                # arranges a hang and then fails its own assertion would otherwise
                # leave this thread parked for the rest of the session — and if the
                # caller under test ever draws on the event loop, the thread parked
                # here is the one that would have to set the flag releasing it.
                self.release.wait(timeout=RELEASE_TIMEOUT_SECONDS)
                raise SurfaceUnavailable("released without drawing")
            if self.draw_takes_seconds:
                time.sleep(self.draw_takes_seconds)
            self.shown.append(layout)
        finally:
            self.left.set()

    def close(self) -> None:
        self.closed += 1

    @property
    def last_text(self) -> list[str]:
        """The lines of the most recent label, for a test to read at a glance.

        **What each line says, not how it is set** — the family name arrives here
        as recorded rather than in the capitals a panel would draw. That is right
        for what these tests ask, which is whether the correct work got captioned;
        a wiring test that had to spell `RUSCHA` would be asserting typography it
        is not about. The styling is pinned in `test_label_metadata.py` and, on
        real type, in `tests/raster/test_pango.py`.
        """
        return [block.text for block in self.shown[-1].blocks] if self.shown else []


class RecordingOutput:
    """A screen that remembers every render it was asked to draw, in order.

    Stands in for the HDMI output behind the `ScreenOutput` interface, which is
    what lets a wall on a connector be tested on a machine that has none.

    **A blank is recorded with how many renders came before it**, so a test can
    tell a screen drawn black after its wall left from one drawn black before
    the next wall's first picture, which a count alone cannot.
    """

    def __init__(self, *, connected: bool = True, screen: tuple[int, int] | None = (1920, 1080)) -> None:
        self.shown: list[Path] = []
        #: Whether the output exists at all. False models a connector the kernel
        #: stopped listing under a running worker.
        self.listed = True
        self.connected = connected
        self.screen = screen
        #: Armed to make `show` raise, as a driver that loses its device would.
        self.fails: Exception | None = None
        #: How many times the loop asked for a redraw, and what `refresh` raises when armed.
        self.refreshed = 0
        self.refresh_fails: Exception | None = None
        #: For each blank, how many renders had been drawn before it.
        self.blanked: list[int] = []
        #: Armed to make `blank` raise, as a card another program took would.
        self.blank_fails: Exception | None = None

    def show(self, render: Path) -> None:
        if self.fails is not None:
            raise self.fails
        self.shown.append(render)

    def refresh(self) -> None:
        self.refreshed += 1
        if self.refresh_fails is not None:
            raise self.refresh_fails

    def blank(self) -> None:
        if self.blank_fails is not None:
            raise self.blank_fails
        self.blanked.append(len(self.shown))


def drm_tree(root: Path, connectors: dict[str, tuple[str, str]]) -> Path:
    """A `/sys/class/drm` holding these connectors, as `{"card1-HDMI-A-1": ("connected", "1920x1080\\n")}`."""
    root.mkdir(parents=True, exist_ok=True)
    for name, (status, modes) in connectors.items():
        connector = root / name
        connector.mkdir()
        (connector / "status").write_text(status + "\n")
        (connector / "modes").write_text(modes)
    return root


class FakeMonitor:
    """A monitor's DDC/CI, behind `ddc.DdcBus`: brightness on its own scale, and the ways it fails.

    Fails where the real exchange fails: `unsupported` answers as a monitor with
    no brightness control does, `silent` as nothing acknowledging the address
    does, and `fails` as a fault of the host. `ignores_writes` models the
    monitor that takes a set and keeps its old value, which only a read-back
    reveals.
    """

    def __init__(self, *, current: int = 10, maximum: int = 100) -> None:
        self.current = current
        self.maximum = maximum
        self.unsupported = False
        self.silent = False
        self.fails: Exception | None = None
        self.ignores_writes = False
        self.reads = 0
        self.writes: list[int] = []
        self.closed = 0

    def read(self, code: int) -> Reading:
        self._answer()
        self.reads += 1
        if self.unsupported or code != BRIGHTNESS:
            raise Unsupported(f"the monitor does not support feature 0x{code:02x}")
        if self.silent:
            raise NoAnswer("nothing answers at the display's address")
        return Reading(current=self.current, maximum=self.maximum)

    def write(self, code: int, value: int) -> None:
        self._answer()
        if self.silent:
            raise NoAnswer("nothing answers at the display's address")
        self.writes.append(value)
        if not self.ignores_writes:
            self.current = value

    def close(self) -> None:
        self.closed += 1

    def _answer(self) -> None:
        if self.fails is not None:
            raise self.fails
