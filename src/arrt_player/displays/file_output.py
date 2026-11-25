"""The file outputs: a screen and a label drawn to a directory, for a Player with no hardware (`FILE_OUTPUT_DIR`).

**One more implementation of `displays.screen.ScreenOutput`, not a test double.**
The supervisor places a wall on it by the name the client heartbeat reports
(`config.FILE_SCREEN_OUTPUT`, kind `framebuffer`), the wall's schedule composes
for its size as for an HDMI connector's mode, and `ScreenDisplay` drives it
exactly as it drives `kms.KmsOutput`: each picture is fitted to the screen by the
same `kms.fitted` and written where a reader can see it, rather than scanned out.
So a real Player process can be run against a real server where there is no
screen, and what it would have drawn can be asserted.

**What it writes, one directory per output** (`<FILE_OUTPUT_DIR>/file-screen-0/`):

* `picture.png` — the picture as drawn: the composed render fitted to the screen,
  lossless, `screen` pixels.
* `shown.json` — what is on the screen now, replaced whole on every draw, with
  `format` (1), `output`, `wall_id`, `outcome` (`picture`, or `blank` for a wall
  that has left the screen), `work_id` (null for a blank, or for a picture drawn
  outside a work's binding), `render` (the composed file in the wall's cache, or
  null), `picture` (the file name above), `picture_sha256` (of that file's bytes,
  so a reader can tell the picture it read is the one this record describes),
  `screen` ([width, height]) and `shown_at` (RFC 3339, UTC).

**The label output, `<FILE_OUTPUT_DIR>/file-label-0/`** (`FILE_OUTPUT_LABEL=on`), is
a `LabelPanel` like the e-paper one, on a surface that lays the label out with
no typesetter (`panel.file_surface`), so the renderer, the label rule, the draw
gate and every journal line run as on a wall. Its `shown.json`, replaced whole
on every drawing, holds `format` (1), `output`, `label_id`, `wall_id` (the wall
the label is mapped to caption), `outcome` (`caption`, `card` or `blank`: the
label rule's decision), `state` (the display state it decided from, or
`unmapped` for the blank a label leaves when its mapping goes), `work_id` (with a
caption), `wall_name` (with a card), `label` (the text a caption sets, as the
server sent it), `lines` (each laid-out line's text, top to bottom) and
`shown_at`.

**How the label's record knows whose drawing it is.** A surface is handed a
layout, which says what the ink is and not why, so the decision travels beside
it in context variables, which a draw's worker thread inherits as it inherits
the work id every journal line carries: `FileLabelPanel.draw` binds the drawing,
and the entry point's label worker binds the mapping (`captioning`) around every
renderer, on any surface.
A layout handed over with either unbound is refused, `SurfaceUnavailable`, and
nothing is written.

**Each file is written whole or not at all** (`write_atomically`), the picture
before the record, so a record never names a picture that is not there yet.
Under the unit's `UMask=0077` both are the service account's alone.

**A write that fails is made again by `refresh`**, on every poll until it lands,
whichever of the two files it stopped at: a new picture beside the old record is
the one window this order leaves, which a reader tells by `picture_sha256` not
matching the picture, and which the next `refresh` closes. The record is written
last, so it never names a picture that is not there.

**Whether a wall has shown a work** is `shown.json` reading that `wall_id`, that
`work_id` and `outcome` `picture`. A reader waits for it with a deadline: a wall
shows its first work once its feed is pulled and composed, which is seconds.
"""

import hashlib
import io
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from PIL import Image

from arrt_player import logs
from arrt_player.client import FRAMEBUFFER_KIND, LabelAssignment, OutputReport
from arrt_player.config import FILE_LABEL_OUTPUT, FILE_SCREEN_OUTPUT, FileOutputSettings
from arrt_player.kms import fitted
from arrt_player.label_renderer import LabelPanel
from arrt_player.label_rule import Drawing
from arrt_player.panel import Layout, SurfaceUnavailable, plain
from arrt_player.panel.file_surface import FILE_LABEL_SIZE, FileLabelSurface
from arrt_player.safe_write import write_whole

#: The layout's version, in every record. A reader refuses one it does not know.
FORMAT: Final[int] = 1
#: The record of what an output shows now, in its directory.
RECORD_FILENAME: Final[str] = "shown.json"
#: The picture as drawn, in a screen output's directory.
PICTURE_FILENAME: Final[str] = "picture.png"
#: `outcome` for a picture, and for the black a wall leaves behind.
PICTURE: Final[str] = "picture"
BLANK: Final[str] = "blank"


def write_atomically(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one step, flushed to the disk first. Raises `OSError`.

    A reader polling the file never sees half of it, and power lost after the
    rename cannot leave a file that exists and holds nothing (`safe_write`).
    """
    write_whole(path, data, temporary=path.with_name(f".{path.name}.tmp"))


def write_record(directory: Path, record: dict[str, Any]) -> None:
    """Write an output's `shown.json`, with the layout's version. Raises `OSError`."""
    directory.mkdir(parents=True, exist_ok=True)
    document = {"format": FORMAT, **record}
    write_atomically(directory / RECORD_FILENAME, (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode())


def output_report(settings: FileOutputSettings) -> OutputReport:
    """The file screen as the client heartbeat states it: connected, at its configured size.

    `framebuffer`, the contract's word for a screen this client draws itself,
    which this is; it has no identity to read.
    """
    return OutputReport(name=FILE_SCREEN_OUTPUT, kind=FRAMEBUFFER_KIND, connected=True, screen=settings.screen)


def now_utc() -> datetime:
    return datetime.now(UTC)


class FileScreenOutput:
    """One wall's screen, drawn to `<directory>/<name>/`, behind `displays.screen.ScreenOutput`.

    **Always listed and connected**: a directory has no cable to pull. A picture
    that cannot be written raises `OSError`, which the screen display answers as
    it answers a screen that refused a picture: said once, the wall goes on.
    """

    def __init__(
        self,
        settings: FileOutputSettings,
        *,
        wall_id: str,
        name: str = FILE_SCREEN_OUTPUT,
        now: Callable[[], datetime] = now_utc,
    ) -> None:
        self._directory = settings.directory / name
        self._name = name
        self._size = settings.screen
        self._wall_id = wall_id
        self._now = now
        #: The render this output should be showing, and the work it is of; None
        #: for a screen that is meant to be black, or has shown nothing yet.
        self._render: Path | None = None
        self._work_id: str | None = None
        #: Whether the last `show` or `blank` is not on the disk yet: set when it is
        #: asked for, cleared when both the picture and its record are written, so a
        #: write that failed is made again by `refresh` however much of it landed.
        self._owed = False

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def listed(self) -> bool:
        return True

    @property
    def connected(self) -> bool:
        return True

    @property
    def screen(self) -> tuple[int, int] | None:
        return self._size

    def show(self, render: Path) -> None:
        # `ScreenOutput.show` is handed only a path, and the composed file's name is a
        # key, not a work: the schedule binds the work around every `show`, and a
        # worker thread inherits it.
        self._render, self._work_id = render, logs.current_work_id()
        self._owed = True
        self._draw(render)

    def refresh(self) -> None:
        """Draw what the output should be showing again if its last write failed or its picture is gone. One `stat` if not."""
        if self._owed or (self._render is not None and not (self._directory / PICTURE_FILENAME).is_file()):
            self._redraw()

    def blank(self) -> None:
        """Draw the screen black and forget the render: a black picture, and a record saying `blank`."""
        self._render, self._work_id = None, None
        self._owed = True
        self._redraw()

    def _redraw(self) -> None:
        if self._render is None:
            self._write(Image.new("RGB", self._size), render=None, outcome=BLANK)
        else:
            self._draw(self._render)

    def _draw(self, render: Path) -> None:
        self._write(fitted(render, self._size), render=render, outcome=PICTURE)

    def _write(self, picture: Image.Image, *, render: Path | None, outcome: str) -> None:
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        data = buffer.getvalue()
        self._directory.mkdir(parents=True, exist_ok=True)
        write_atomically(self._directory / PICTURE_FILENAME, data)
        write_record(
            self._directory,
            {
                "output": self._name,
                "wall_id": self._wall_id,
                "outcome": outcome,
                "work_id": self._work_id,
                "render": str(render) if render is not None else None,
                "picture": PICTURE_FILENAME,
                "picture_sha256": hashlib.sha256(data).hexdigest(),
                "screen": list(self._size),
                "shown_at": self._now().isoformat(),
            },
        )
        self._owed = False


# -- the label ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Drawn:
    drawing: Drawing
    label_id: str


#: The drawing being put on a file label now, bound by `FileLabelPanel.draw`.
_DRAWING: ContextVar[_Drawn | None] = ContextVar("file_label_drawing", default=None)
#: The mapping a label worker draws for, bound by `captioning`.
_CAPTIONING: ContextVar[LabelAssignment | None] = ContextVar("file_label_captioning", default=None)


@contextmanager
def captioning(assignment: LabelAssignment) -> Iterator[None]:
    """Bind the mapping a label worker draws for, so a label output that records its drawings can say whose.

    Bound around every renderer the entry point starts, whatever its surface:
    a surface that records nothing reads nothing, and binding it only for some
    would be a second way of running a label.
    """
    token = _CAPTIONING.set(assignment)
    try:
        yield
    finally:
        _CAPTIONING.reset(token)


class FileLabelPanel(LabelPanel):
    """The file label output: a `LabelPanel` whose every draw binds its drawing for the record.

    Everything else is the panel's own: the gate, the budget, the episodes, the
    journal lines and the report (kind `epaper`, the only label kind the
    contract names, under the name `file-label-0`).
    """

    async def draw(self, drawing: Drawing, *, label_id: str) -> None:
        token = _DRAWING.set(_Drawn(drawing=drawing, label_id=label_id))
        try:
            await super().draw(drawing, label_id=label_id)
        finally:
            _DRAWING.reset(token)


class _LabelRecord:
    """Writes a file label's `shown.json` from a layout and the decision bound beside it."""

    def __init__(self, directory: Path, *, name: str, now: Callable[[], datetime]) -> None:
        self.directory = directory
        self._name = name
        self._now = now

    def __call__(self, layout: Layout) -> None:
        drawn = _DRAWING.get()
        if drawn is None:
            # Only `FileLabelPanel.draw` reaches here with a drawing bound; a
            # record without its decision would be evidence of nothing.
            raise SurfaceUnavailable("a file label was handed a layout with no drawing bound to it")
        assignment = _CAPTIONING.get()
        if assignment is None:
            # The entry point's label worker binds the mapping around every
            # renderer; a record naming no wall would be evidence of nothing.
            raise SurfaceUnavailable("a file label was handed a layout with no mapping bound to it")
        drawing = drawn.drawing
        write_record(
            self.directory,
            {
                "output": self._name,
                "label_id": drawn.label_id,
                "wall_id": assignment.wall_id,
                "outcome": drawing.outcome.value,
                "state": drawing.state,
                "work_id": drawing.work_id,
                "wall_name": drawing.wall_name,
                "label": drawing.label,
                "lines": [plain(block.runs) for block in layout.blocks],
                "shown_at": self._now().isoformat(),
            },
        )


def file_label_panel(
    settings: FileOutputSettings, *, name: str = FILE_LABEL_OUTPUT, now: Callable[[], datetime] = now_utc
) -> FileLabelPanel:
    """The file label output, writing to `<directory>/<name>/`."""
    surface = FileLabelSurface(_LabelRecord(settings.directory / name, name=name, now=now), size=FILE_LABEL_SIZE)
    return FileLabelPanel(name=name, surface=surface, error=None, size=FILE_LABEL_SIZE)
