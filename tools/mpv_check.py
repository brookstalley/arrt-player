"""Play files on an HDMI connector through the Player's own mpv renderer, and say what happened to each.

Run on the Pi, as the account the unit runs as, with the Player stopped (only one
program can hold the screen):

    sudo systemctl stop display
    sudo -u tvpi uv run python tools/mpv_check.py --min 4 --max 8 /path/to/media/*
    sudo systemctl start display

Every file goes through `mpv.MpvOutput` and `dwell.dwell_seconds`, exactly as the
slideshow will put it up, so what a person at the set sees is what the Player
does: not a hand-written mpv command line that could drift from it. **The person
at the set is the measurement.** mpv's own report is printed beside each item,
and it has been wrong before: it once said 0 dropped frames while nothing at all
reached the screen. So ask them what they saw, in particular whether the text
console showed between items.

All the files must be in one directory, which becomes the renderer's media root.
"""

import argparse
import sys
import tempfile
import time
from pathlib import Path

from arrt_player.dwell import DwellLimits, DwellLimitsInvalid, dwell_seconds
from arrt_player.mpv import Loaded, MpvOutput

#: How often the renderer is asked whether the item is still up.
CHECK_EVERY_SECONDS = 0.5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--connector", default="hdmi-a-1")
    parser.add_argument("--min", type=float, default=5.0, dest="minimum")
    parser.add_argument("--max", type=float, default=10.0, dest="maximum")
    arguments = parser.parse_args(argv)

    files = [path.resolve() for path in arguments.files]
    roots = {path.parent for path in files}
    if len(roots) != 1:
        print("mpv_check: give files from one directory; it becomes the media root", file=sys.stderr)
        return 2
    try:
        limits = DwellLimits(minimum=arguments.minimum, maximum=arguments.maximum)
    except DwellLimitsInvalid as exc:
        print(f"mpv_check: {exc}", file=sys.stderr)
        return 2

    shown = 0
    with (
        tempfile.TemporaryDirectory(prefix="mpv-check-") as runtime,
        MpvOutput(arguments.connector, runtime_dir=Path(runtime), media_root=roots.pop()) as output,
    ):
        for path in files:
            outcome = output.load(path)
            if not isinstance(outcome, Loaded):
                print(f"NOT SHOWN  {path.name}: {outcome.reason}")
                continue
            dwell = dwell_seconds(outcome.duration, limits)
            length = "still" if not outcome.duration else f"{outcome.duration:.2f} s clip"
            print(f"showing    {path.name} ({length}) for {dwell:.1f} s", flush=True)
            deadline = time.monotonic() + dwell
            failure = None
            while failure is None and time.monotonic() < deadline:
                time.sleep(CHECK_EVERY_SECONDS)
                failure = output.check()
            if failure is not None:
                print(f"LOST       {path.name}: {failure.reason}")
            else:
                shown += 1
    print(f"{shown} of {len(files)} shown, by mpv's account. Ask the person at the set what they saw.")
    return 0 if shown == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
