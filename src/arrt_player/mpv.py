"""Drawing stills and clips on an HDMI connector through mpv, a program this process runs and owns.

**Why mpv rather than the dumb buffers `kms.py` draws stills with.** Smooth video on
a Pi 4 needs its hardware decoder and its display hardware's scaler, and Python
drawing frames into a buffer has neither. mpv has both. The options it runs with
were measured on the wall's Pi (Trixie, kernel 6.18.34+rpt, mpv 0.40, a 4K LG,
2026-11-10, with the owner at the set):

* `--hwdec=v4l2m2m` decodes H.264 in hardware without copying the frames back.
* `--gpu-hwdec-interop=drmprime-overlay` with the video on the **primary** plane
  and mpv's own drawing on the **overlay** plane lets the display hardware scale
  1080p to 4K: 0 dropped frames and 0 failed commits over 20 s, and the owner saw
  it play. With the planes the other way round every atomic commit failed and the
  screen showed nothing. The GPU-scaled path dropped about a third of its frames.
* `--profile=fast` keeps the GPU's share cheap.
* The screen stays at its first mode (4K): at 1080p the LG puts up its own
  "not the recommended resolution" dialog.

**What mpv reports is not evidence of what is on the screen.** In that failed run
mpv said `hwdec=v4l2m2m` and 0 dropped frames while not one frame was shown. So
an item counts as shown only when its file loaded and none of the failure lines
below arrived while it was up. Those lines come over the same socket as
`log-message` events, so there is no log file to tail.

**This process owns mpv's whole life.** mpv runs in its own process group,
started on first use and stopped on every way out (`close`). Its pid is kept in
`runtime_dir`, so a mpv left behind by a Player that was killed outright is found
and stopped at the next start; a probe that crashed during the measurements left
one running for five minutes, holding the screen. Only one process can be master
of a DRM card, and both of a Pi 4's HDMI connectors are on one card, so one mpv
drives one connector.

**mpv is handed files, never addresses.** Every path is checked to be a file under
`media_root` before it is sent, so mpv can never become a second network client
(`tests/test_plane_isolation.py` guards the Player's network code).

**Blocking, like every `displays.screen.ScreenOutput`.** Each call waits on mpv
with a timeout and is meant for a worker thread; a lock keeps two threads from
interleaving commands on the one socket.
"""

import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

from arrt_player.client import DRM_ROOT, FRAMEBUFFER_KIND, OutputReport, hdmi_outputs
from arrt_player.episodes import ReportOnce
from arrt_player.kms import DEV_ROOT, ScreenAbsent, card_for

log = logging.getLogger(__name__)

#: The options measured on the Pi (module docstring). `--idle` keeps mpv running
#: between items, `--keep-open` holds a clip's last frame rather than closing the
#: video at its end, and an image stays up until it is replaced.
MPV_OPTIONS: Final[tuple[str, ...]] = (
    "--no-config",
    "--really-quiet",
    "--no-audio",
    "--no-input-terminal",
    "--no-osc",
    "--osd-level=0",
    "--idle=yes",
    "--keep-open=yes",
    "--image-display-duration=inf",
    "--vo=gpu",
    "--gpu-context=drm",
    "--profile=fast",
    "--hwdec=v4l2m2m",
    "--gpu-hwdec-interop=drmprime-overlay",
    "--drm-drmprime-video-plane=primary",
    "--drm-draw-plane=overlay",
)

#: Lines that mean the item is not on the screen, matched on the start of the text
#: at any level. `fatal` from mpv means the same whatever it says. Lines mpv logs
#: on every healthy start under systemd (`Can't open TTY for VT control`, `Failed
#: to set up VT switcher`, `Could not find any preferred mode`) are not here.
FAILURE_LINES: Final[tuple[str, ...]] = (
    "Failed to commit atomic request",
    "Failed to commit ModeSetting atomic request",
    "Failed to acquire DRM master",
    "Failed initializing any suitable GPU context",
    "Error opening/initializing the selected video_out",
)

#: How long mpv gets to open its socket, and a file to load, before it is given up on.
START_TIMEOUT_SECONDS: Final[float] = 10.0
LOAD_TIMEOUT_SECONDS: Final[float] = 15.0

#: How long a failed start is waited out before the next one, doubling to the
#: last value. A screen another program holds fails every start, and restarting
#: mpv for every item would spin.
RESTART_BACKOFF_SECONDS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 60.0)


#: The longest socket path every platform the suite runs on accepts: macOS
#: allows 104 bytes in `sun_path` and Linux 108, with the terminating zero.
MAX_SOCKET_PATH: Final[int] = 103


class MpvError(Exception):
    """mpv could not be started, answered nothing, refused a command, or went away."""


class MpvUnavailable(MpvError):
    """mpv answered that a property has no value now: not a failure of mpv, read by `_Ipc.property`."""


@dataclass(frozen=True)
class Loaded:
    """The file is up. `duration` is mpv's: 0.0 for an image, and None when it could not say."""

    path: Path
    duration: float | None


@dataclass(frozen=True)
class Failure:
    """What mpv said went wrong. `screen` is a failure to draw at all; otherwise it was this file."""

    text: str
    screen: bool


@dataclass(frozen=True)
class NotShown:
    """The file is not on the screen, and why, in words for the journal."""

    path: Path | None
    reason: str


def mpv_connector(name: str) -> str:
    """mpv's spelling of a connector this Player calls `hdmi-a-1`."""
    return name.upper()


class _Ipc:
    """One connection to mpv's socket: commands with replies, and the events in between kept for reading."""

    def __init__(self, path: Path, *, timeout: float) -> None:
        self._socket = socket.socket(socket.AF_UNIX)
        self._socket.settimeout(timeout)
        self._socket.connect(str(path))
        self._buffer = b""
        self._next_id = 0
        self.events: list[dict[str, Any]] = []

    def command(self, *arguments: object, timeout: float) -> object:
        """Send a command and wait for its reply. Raises `MpvError` on an error reply or no reply."""
        self._next_id += 1
        request_id = self._next_id
        self._send({"command": list(arguments), "request_id": request_id})
        deadline = time.monotonic() + timeout
        while True:
            reply: dict[str, Any] | None = None
            # **The whole batch, even past the reply.** mpv often sends a reply
            # and the events after it in one write (`loadfile`'s `success`, then
            # `file-loaded`), and an event dropped here never comes again.
            for message in self._read(deadline):
                if reply is None and message.get("request_id") == request_id:
                    reply = message
                else:
                    self.events.append(message)
            if reply is not None:
                error = reply.get("error")
                if error == "property unavailable":
                    raise MpvUnavailable(f"mpv has no {arguments[1] if len(arguments) > 1 else arguments[0]} now")
                if error != "success":
                    raise MpvError(f"mpv refused {arguments[0]}: {error}")
                return reply.get("data")
            if time.monotonic() >= deadline:
                raise MpvError(f"mpv did not answer {arguments[0]} within {timeout:g} s")

    def property(self, name: str, *, timeout: float) -> object:
        """A property's value, or None when mpv says it has none now (`property unavailable`, measured on the Pi).

        Any other refusal, no answer or a closed socket still raises `MpvError`: those
        say something is wrong with mpv, where an unavailable property says only that
        this file does not have one (a still's player has no length to give, nor
        does a stream whose container carries none).
        """
        try:
            return self.command("get_property", name, timeout=timeout)
        except MpvUnavailable:
            return None

    def wait_for(self, done: Callable[[dict[str, Any]], bool], *, timeout: float) -> dict[str, Any] | None:
        """The first event, already held or read until the deadline, that satisfies `done`; None at the deadline.

        **Held events first.** The reply to the command before this wait often
        arrived in the same write as the event waited for, and `command` kept it.
        """
        held = next((message for message in self.events if done(message)), None)
        if held is not None:
            return held
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            batch = self._read(deadline)
            # Every event of the batch is kept, including those after the one
            # waited for: a failure line can share a write with `file-loaded`.
            self.events.extend(batch)
            found = next((message for message in batch if done(message)), None)
            if found is not None:
                return found
        return None

    def drain(self) -> None:
        """Take in whatever has arrived, without waiting."""
        self.events.extend(self._read(time.monotonic()))

    def close(self) -> None:
        self._socket.close()

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._socket.sendall(json.dumps(message).encode() + b"\n")
        except OSError as exc:
            raise MpvError(f"mpv's socket is gone: {exc}") from exc

    def _read(self, deadline: float) -> list[dict[str, Any]]:
        """What has arrived, waiting no later than `deadline` for something to."""
        self._socket.settimeout(max(deadline - time.monotonic(), 0.001))
        try:
            chunk = self._socket.recv(65536)
        except TimeoutError:
            return self._lines()
        except OSError as exc:
            raise MpvError(f"mpv's socket is gone: {exc}") from exc
        if not chunk:
            raise MpvError("mpv closed its socket")
        self._buffer += chunk
        return self._lines()

    def _lines(self) -> list[dict[str, Any]]:
        messages = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if line.strip():
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise MpvError(f"mpv sent a line that is not JSON: {line[:80]!r}") from exc
        return messages


def failure_in(event: dict[str, Any]) -> Failure | None:
    """The failure an mpv event reports, or None for one that reports none.

    **The first failure in a batch decides which kind it is.** A screen mpv cannot
    draw on logs its failure lines and *then* ends the file with an error, so a
    batch led by an `end-file` error with no such line before it is a file mpv
    could not play (missing, or not media), and mpv itself is fine.
    """
    if event.get("event") == "log-message":
        text = str(event.get("text", "")).strip()
        if event.get("level") == "fatal" or text.startswith(FAILURE_LINES):
            return Failure(text, screen=True)
    if event.get("event") == "end-file" and event.get("reason") == "error":
        return Failure(f"mpv could not play the file ({event.get('file_error', 'no reason given')})", screen=False)
    return None


class MpvOutput:
    """One HDMI connector, drawn by an mpv this object starts, watches and stops.

    `load` puts a file up and says whether it is there; `check` says whether it
    is still there; `show` and `refresh` make this a `displays.screen.ScreenOutput`
    for stills, so a wall can be drawn through it unchanged.
    """

    def __init__(
        self,
        name: str,
        *,
        runtime_dir: Path,
        media_root: Path,
        command: Sequence[str] = ("mpv",),
        drm_root: Path = DRM_ROOT,
        dev_root: Path = DEV_ROOT,
        monotonic: Callable[[], float] = time.monotonic,
        start_timeout: float = START_TIMEOUT_SECONDS,
        load_timeout: float = LOAD_TIMEOUT_SECONDS,
    ) -> None:
        self._name = name
        self._runtime_dir = runtime_dir
        self._media_root = media_root.resolve()
        self._command = tuple(command)
        self._drm_root = drm_root
        self._dev_root = dev_root
        self._monotonic = monotonic
        self._start_timeout = start_timeout
        self._load_timeout = load_timeout
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._ipc: _Ipc | None = None
        #: The file that should be up, for `refresh` to put back after a restart.
        self._current: Path | None = None
        #: Consecutive screen failures, and when mpv may next be started.
        self._failures = 0
        self._not_before = 0.0
        self._failing = ReportOnce()

    # -- the slideshow's interface -----------------------------------------------

    def load(self, path: Path) -> Loaded | NotShown:
        """Put `path` on the screen, replacing what is there; a clip loops until the next load."""
        with self._lock:
            return self._load(path)

    def check(self) -> NotShown | None:
        """Whether what `load` put up is still there: a failure since then ends it, and mpv restarts on the next load."""
        with self._lock:
            if self._ipc is None or self._process is None:
                return NotShown(self._current, "mpv is not running")
            if self._process.poll() is not None:
                return self._lost(f"mpv exited with status {self._process.returncode}")
            try:
                self._ipc.drain()
            except MpvError as exc:
                return self._lost(str(exc))
            failure = self._take_failure()
            return None if failure is None else self._failed(failure)

    def close(self) -> None:
        """Stop mpv and everything it started. Safe to call more than once."""
        with self._lock:
            self._stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- displays.screen.ScreenOutput ----------------------------------------------

    def show(self, render: Path) -> None:
        """Put a still up. A failure is logged here and comes back through `refresh`, as the protocol asks."""
        with self._lock:
            self._load(render)

    def refresh(self) -> None:
        """Put the current file back if mpv is gone or failed since it went up."""
        with self._lock:
            if self._current is None:
                return
            alive = self._process is not None and self._process.poll() is None and self._ipc is not None
            failure: Failure | None = None
            if alive and self._ipc is not None:
                try:
                    self._ipc.drain()
                    failure = self._take_failure()
                except MpvError as exc:
                    failure = Failure(str(exc), screen=True)
            if alive and failure is None:
                return
            if failure is not None:
                self._failed(failure)
            self._load(self._current)

    @property
    def listed(self) -> bool:
        return any(report.name == self._name for report in hdmi_outputs(self._drm_root))

    @property
    def connected(self) -> bool:
        return self._report().connected

    @property
    def screen(self) -> tuple[int, int] | None:
        return self._report().screen

    # -- inside --------------------------------------------------------------------

    def _load(self, path: Path) -> Loaded | NotShown:
        refused = self._refusal(path)
        if refused is not None:
            log.error(refused, extra={"event": "mpv.refused", "output": self._name})
            return NotShown(path, refused)
        # The resolved path, which is the one `_refusal` checked: mpv never sees a
        # `..` or a link the check did not follow.
        path = path.resolve()
        # **What arrived since the last `check` belongs to the item going down.** A
        # screen failure in it means the screen is not being drawn, so it is not
        # thrown away with the rest: mpv is restarted before anything else goes up.
        held = self._held_failure()
        if held is not None and held.screen:
            self._current = path
            return self._lost(held.text)
        self._current = path
        try:
            ipc = self._started()
        except MpvError as exc:
            return self._not_shown(path, str(exc))
        try:
            return self._put_up(ipc, path)
        except MpvError as exc:
            return self._lost(str(exc))

    def _put_up(self, ipc: _Ipc, path: Path) -> Loaded | NotShown:
        """Load `path` on a running mpv and read its length. Raises `MpvError` when mpv itself fails."""
        ipc.events.clear()
        ipc.command("loadfile", str(path), "replace", timeout=self._load_timeout)
        outcome = ipc.wait_for(
            lambda event: event.get("event") == "file-loaded" or failure_in(event) is not None, timeout=self._load_timeout
        )
        if outcome is None:
            raise MpvError(f"mpv did not load {path.name} within {self._load_timeout:g} s")
        failure = self._take_failure()
        if failure is not None:
            return self._failed(failure)
        duration = ipc.property("duration", timeout=self._load_timeout)
        seconds = float(duration) if isinstance(duration, int | float) else None
        # A clip loops until the next item replaces it; the dwell rule decides
        # how long that is (`dwell.py`), so whole plays and the cut both
        # come from when the next load arrives.
        ipc.command("set_property", "loop-file", "inf" if seconds else "no", timeout=self._load_timeout)
        self._failures = 0
        self._not_before = 0.0
        if self._failing.end():
            log.info("mpv is drawing on %s again", self._name, extra={"event": "mpv.recovered", "output": self._name})
        return Loaded(path, seconds)

    def _refusal(self, path: Path) -> str | None:
        """Why a path must not reach mpv, or None: only regular files under `media_root` do."""
        resolved = path.resolve()
        if not resolved.is_relative_to(self._media_root):
            return f"refusing to hand mpv {path}: it is not under {self._media_root}"
        if not resolved.is_file():
            return f"refusing to hand mpv {path}: it is not a file"
        return None

    def _started(self) -> _Ipc:
        """The running mpv's connection, starting mpv first if none is running. Raises `MpvError`."""
        if self._ipc is not None and self._process is not None and self._process.poll() is None:
            return self._ipc
        self._stop()
        now = self._monotonic()
        if now < self._not_before:
            raise MpvError(f"mpv is waiting {self._not_before - now:.0f} s before starting again")
        try:
            self._ipc = self._start()
        except MpvError:
            self._back_off()
            self._stop()
            raise
        return self._ipc

    def _start(self) -> _Ipc:
        self._runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        stop_stale(self._runtime_dir)
        socket_path = self._runtime_dir / "mpv.sock"
        if len(str(socket_path)) > MAX_SOCKET_PATH:
            raise MpvError(f"mpv's socket path is too long for a Unix socket ({len(str(socket_path))} characters): {socket_path}")
        socket_path.unlink(missing_ok=True)
        try:
            card = card_for(self._name, self._drm_root, self._dev_root)
        except ScreenAbsent as exc:
            raise MpvError(str(exc)) from exc
        argv = [
            *self._command,
            *MPV_OPTIONS,
            f"--drm-device={card}",
            f"--drm-connector={mpv_connector(self._name)}",
            f"--input-ipc-server={socket_path}",
        ]
        try:
            # Its own session, so the whole group can be stopped together and a
            # signal meant for the Player is not delivered to mpv as well.
            self._process = subprocess.Popen(  # noqa: S603 -- argv is this module's constant options and a checked connector
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise MpvError(f"mpv could not be started ({self._command[0]}): {exc}") from exc
        (self._runtime_dir / "mpv.pid").write_text(str(self._process.pid))
        deadline = time.monotonic() + self._start_timeout
        while not socket_path.exists():
            if self._process.poll() is not None:
                raise MpvError(f"mpv exited with status {self._process.returncode} before opening its socket")
            if time.monotonic() > deadline:
                raise MpvError(f"mpv did not open its socket within {self._start_timeout:g} s")
            time.sleep(0.02)
        try:
            ipc = _Ipc(socket_path, timeout=self._start_timeout)
            # Warnings and up, which is where the failure lines are.
            ipc.command("request_log_messages", "warn", timeout=self._start_timeout)
        except OSError as exc:
            raise MpvError(f"mpv's socket would not connect: {exc}") from exc
        log.info("mpv started on %s", self._name, extra={"event": "mpv.started", "output": self._name, "pid": self._process.pid})
        return ipc

    def _held_failure(self) -> Failure | None:
        """A failure among what a running mpv has sent and nobody has read, or None, including when none is running."""
        if self._ipc is None or self._process is None or self._process.poll() is not None:
            return None
        try:
            self._ipc.drain()
        except MpvError as exc:
            return Failure(str(exc), screen=True)
        return self._take_failure()

    def _take_failure(self) -> Failure | None:
        """The first failure among the events read so far, consuming them all."""
        if self._ipc is None:
            return None
        events = list(self._ipc.events)
        self._ipc.events.clear()
        return next((said for said in map(failure_in, events) if said is not None), None)

    def _failed(self, failure: Failure) -> NotShown:
        """mpv reported a failure: a screen failure is `_lost`; a file it cannot play costs that file only."""
        if failure.screen:
            return self._lost(failure.text)
        log.warning(
            "mpv could not play %s: %s",
            self._current,
            failure.text,
            extra={"event": "mpv.file_failed", "output": self._name, "reason": failure.text},
        )
        return NotShown(self._current, failure.text)

    def _lost(self, reason: str) -> NotShown:
        """The screen is not being drawn: stop mpv, and wait out the backoff before the next start."""
        self._stop()
        self._back_off()
        return self._not_shown(self._current, reason)

    def _back_off(self) -> None:
        """One more screen failure in a row: the next start waits longer, up to the last step."""
        self._failures += 1
        step = RESTART_BACKOFF_SECONDS[min(self._failures, len(RESTART_BACKOFF_SECONDS)) - 1]
        self._not_before = self._monotonic() + step

    def _not_shown(self, path: Path | None, reason: str) -> NotShown:
        if self._failing.begin():
            log.warning(
                "mpv is not drawing on %s: %s",
                self._name,
                reason,
                extra={"event": "mpv.not_shown", "output": self._name, "reason": reason},
            )
        return NotShown(path, reason)

    def _stop(self) -> None:
        if self._ipc is not None:
            with contextlib.suppress(MpvError, OSError):
                self._ipc.command("quit", timeout=1.0)
            self._ipc.close()
            self._ipc = None
        if self._process is not None:
            stop_group(self._process.pid, wait=self._process.wait)
            self._process = None
            # Only files this object's own mpv wrote. A pid file found at start
            # belongs to an mpv a killed Player left behind, and `stop_stale`
            # needs it to find that one.
            (self._runtime_dir / "mpv.pid").unlink(missing_ok=True)
            (self._runtime_dir / "mpv.sock").unlink(missing_ok=True)

    def _report(self) -> OutputReport:
        for report in hdmi_outputs(self._drm_root):
            if report.name == self._name:
                return report
        return OutputReport(name=self._name, kind=FRAMEBUFFER_KIND, connected=False, screen=None)


def stop_group(pid: int, *, wait: Callable[[float], object] | None = None, grace: float = 2.0) -> None:
    """Stop a process group this module started: SIGTERM, then SIGKILL once `grace` has passed."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)
    if wait is not None:
        try:
            wait(grace)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
            wait(grace)
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    if _alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def stop_stale(runtime_dir: Path) -> None:
    """Stop an mpv a previous Player left running, found by the pid it wrote and confirmed by its socket path."""
    pid_file = runtime_dir / "mpv.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return
    socket_path = str(runtime_dir / "mpv.sock")
    # A pid is reused once its process is gone: stop it only if it is the mpv this
    # directory's socket belongs to, never whatever runs under that number now.
    if _alive(pid) and socket_path in _command_line(pid):
        log.warning("stopping an mpv a previous run left behind (pid %d)", pid, extra={"event": "mpv.stale", "pid": pid})
        stop_group(pid)
    pid_file.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_line(pid: int) -> str:
    """A process's command line: from /proc on Linux, from `ps` elsewhere (the suite runs on a Mac too)."""
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.exists():
        with contextlib.suppress(OSError):
            return proc.read_bytes().replace(b"\0", b" ").decode(errors="replace")
    ps = ["ps", "-o", "command=", "-p", str(pid)]
    result = subprocess.run(ps, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, our own pid
    return result.stdout
