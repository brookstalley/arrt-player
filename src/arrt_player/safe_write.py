"""The one way this Player writes a file whole: stage, force to the card, rename, and leave nothing behind.

**What a reader sees.** A file written through here is either what it was or
what it now is, never half of either: the bytes go to a temporary file in the
same directory (a rename is atomic only within a filesystem), are forced to the
card, and only then renamed over the destination. A rename can reach an SD card
before the bytes it renames do, so a power cut at the wrong moment would leave a
file that exists, is the right size and holds nothing; the `fsync` before the
rename is what orders them.

**What a failure leaves.** The destination as it was, and no temporary file, on
any exit, a `KeyboardInterrupt` and a cancelled task included. The unlink is
quiet: a card that refused the write may refuse the unlink too, and that second
refusal must not hide the first, which is the one raised.

**The temporary file's name is the caller's**, because each directory's tidy
recognises its own (`Pull._cached_render_names`, the schedule's tidy,
`WallCaches`), and a name changed here would be a stray file to one of them.

**No directory `fsync`.** The destination's own directory entry is not forced; a
cut can still leave the old file, which is whole.

The two writers that stream a download while it arrives (`pull.Pull._cache_media`,
`sources.fetch._save`) keep their own staging: their bytes are not in hand
when the file is opened.
"""

import contextlib
import os
from collections.abc import Callable
from pathlib import Path


def write_whole(path: Path, data: bytes, *, temporary: Path, mode: int | None = None) -> None:
    """Replace `path` with `data`, staged at `temporary`. Raises `OSError`; leaves `path` as it was on any failure.

    `mode` is the file's permission bits when it must be exact: the staged file
    is set to it before any byte is written, so a stale temporary file left by a
    crash, which `O_CREAT` would keep the old bits of, never carries the data
    at a looser mode. Without it the file takes the process's umask as any
    created file does.
    """
    _replacing(path, temporary, lambda: _stage(temporary, data, mode))


def write_whole_with(path: Path, produce: Callable[[Path], None], *, temporary: Path) -> None:
    """Replace `path` with what `produce` writes to `temporary`, for a writer that must be handed a path (an image encoder).

    `produce` is called with `temporary`; the file it leaves there is forced to
    the card and renamed into place. What `produce` raises is raised, after the
    temporary file is gone.
    """

    def stage() -> None:
        produce(temporary)
        with temporary.open("rb") as written:
            os.fsync(written.fileno())

    _replacing(path, temporary, stage)


def _stage(temporary: Path, data: bytes, mode: int | None) -> None:
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666 if mode is None else mode)
    with os.fdopen(descriptor, "wb") as handle:
        if mode is not None:
            os.fchmod(handle.fileno(), mode)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _replacing(path: Path, temporary: Path, stage: Callable[[], None]) -> None:
    try:
        stage()
        temporary.replace(path)
    except BaseException:  # prawduct:allow prawduct/broad-except -- cleanup-and-reraise; the temp file must go on any exit
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
