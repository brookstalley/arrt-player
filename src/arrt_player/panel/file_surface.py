"""A label surface with no ink: what a Player's file label output lays its label out on (`FILE_OUTPUT_LABEL`).

**One more `LabelSurface`, held by a `LabelPanel` like the e-paper one**, so the
label renderer, the label rule, the draw gate and budget, the layout tier and
every journal line run exactly as they do on a wall. Instead of putting the
laid-out label on a panel, `show` hands it to the writer it was built with,
which records what was decided (`displays.file_output`, where the file is
described). A writer that fails with `OSError` is a surface that would not take
the label, said as every other one is.

**It needs no typesetter.** Text is measured by arithmetic (`measured`): a glyph
is 0.6 of the type's size wide and a row 1.2 of it tall, which is near enough to
a real face for the layout's drop and shrink rules to behave as they do on a
panel, and is no claim about any face. Its type is sized for the reference wall
(a 6-inch panel read from 84 inches), because a file has no physical size and no
reader; its size is the caller's, the reference panel's by default.

Imports nothing outside `panel`, as the rest of this package does.
"""

import math
from collections.abc import Callable
from typing import Final

from arrt_player.panel.layout import Extent, Geometry, Layout, Measure
from arrt_player.panel.legibility import TypeScale, margin_for, type_scale_for
from arrt_player.panel.styling import Line, set_text
from arrt_player.panel.surface import LabelSurface, SurfaceUnavailable

#: The file label's default size: the reference panel's, an IT8951 at 1448×1072.
FILE_LABEL_SIZE: Final[tuple[int, int]] = (1448, 1072)
#: The reference wall's viewing conditions, which size the file label's type.
REFERENCE_DIAGONAL_INCHES: Final[float] = 6.0
REFERENCE_VIEWING_DISTANCE_INCHES: Final[float] = 84.0
#: A glyph's advance and a row's height, as fractions of the type's size.
GLYPH_WIDTH: Final[float] = 0.6
ROW_HEIGHT: Final[float] = 1.2


def measured(line: Line, size_px: int, wrap_px: int) -> Extent:
    """`line` set at `size_px` and wrapped to `wrap_px`, by arithmetic rather than a face."""
    text = set_text(line)
    glyph = max(1, round(size_px * GLYPH_WIDTH))
    per_row = max(1, wrap_px // glyph)
    rows = max(1, math.ceil(len(text) / per_row))
    return Extent(width_px=min(len(text) * glyph, wrap_px), height_px=rows * math.ceil(size_px * ROW_HEIGHT), rows=rows)


class FileLabelSurface(LabelSurface):
    """A surface whose `show` hands each laid-out label to `write`. Raises `SurfaceUnavailable` when that fails."""

    def __init__(self, write: Callable[[Layout], None], *, size: tuple[int, int] = FILE_LABEL_SIZE) -> None:
        self._write = write
        self._type_scale = type_scale_for(
            width_px=size[0],
            height_px=size[1],
            diagonal_inches=REFERENCE_DIAGONAL_INCHES,
            viewing_distance_inches=REFERENCE_VIEWING_DISTANCE_INCHES,
        )
        self._geometry = Geometry(width_px=size[0], height_px=size[1], margin_px=margin_for(self._type_scale))

    @property
    def geometry(self) -> Geometry:
        return self._geometry

    @property
    def type_scale(self) -> TypeScale:
        return self._type_scale

    @property
    def measure(self) -> Measure:
        return measured

    def show(self, layout: Layout) -> None:
        try:
            self._write(layout)
        except OSError as exc:
            raise SurfaceUnavailable(f"the label could not be written ({exc})") from exc

    def close(self) -> None:
        return
