"""The mpv renderer, against a fake mpv speaking the real IPC over a real Unix socket (`tests/fake_mpv.py`).

The fake does what mpv 0.40 did on the wall's Pi, failures included. The one this
module exists for is the run where mpv said 0 dropped frames while nothing at all
reached the screen: an item counts as shown only when no failure line arrived
while it was up.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import drm_tree

from arrt_player.mpv import MPV_OPTIONS, Loaded, MpvOutput, NotShown, failure_in, stop_stale

FAKE = Path(__file__).with_name("fake_mpv.py")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Bench:
    """A connector on card1, a media directory, a short runtime directory, and the fake as mpv."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, behaviour: str = "") -> None:
        self.drm = drm_tree(root / "drm", {"card1-HDMI-A-1": ("connected", "3840x2160\n")})
        self.media = root / "media"
        self.media.mkdir()
        self.runtime = root / "rt"
        self.record = root / "record.jsonl"
        self.clock = Clock()
        monkeypatch.setenv("FAKE_MPV_BEHAVIOUR", behaviour)
        monkeypatch.setenv("FAKE_MPV_RECORD", str(self.record))
        self.output = MpvOutput(
            "hdmi-a-1",
            runtime_dir=self.runtime,
            media_root=self.media,
            command=(sys.executable, str(FAKE)),
            drm_root=self.drm,
            dev_root=root / "dev",
            monotonic=self.clock,
            # Room for a busy test runner: these bound waits on a fake that answers
            # in milliseconds, so they only decide how long a real failure takes.
            start_timeout=10.0,
            load_timeout=5.0,
        )

    def file(self, name: str, content: str) -> Path:
        path = self.media / name
        path.write_text(content)
        return path

    def lines(self) -> list[object]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def starts(self) -> list[dict]:
        return [line for line in self.lines() if isinstance(line, dict)]

    def commands(self) -> list[list]:
        return [line for line in self.lines() if isinstance(line, list)]

    def pid(self) -> int:
        return self.starts()[-1]["pid"]


@pytest.fixture
def root() -> Iterator[Path]:
    # Short on purpose: a Unix socket path must fit in 104 bytes on macOS, and
    # pytest's own tmp_path there is longer than that on its own.
    path = Path(tempfile.mkdtemp(prefix="mpv-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def bench(root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Bench]:
    made = Bench(root, monkeypatch)
    yield made
    made.output.close()


def make(root: Path, monkeypatch: pytest.MonkeyPatch, behaviour: str) -> Bench:
    return Bench(root, monkeypatch, behaviour)


def alive(pid: int) -> bool:
    """Whether a process is running. A zombie still answers kill(0), and is gone for every purpose here."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        # Linux, where the runners may have no `ps`: the state follows the name's closing parenthesis.
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    ps = ["ps", "-o", "stat=", "-p", str(pid)]
    status = subprocess.run(ps, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, our own pid
    return bool(status.stdout.strip()) and not status.stdout.strip().startswith("Z")


def wait_until(condition, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# -- what mpv is asked to do -----------------------------------------------------


def test_mpv_runs_with_the_options_measured_on_the_pi_on_the_connectors_card(bench: Bench) -> None:
    bench.output.load(bench.file("a.png", "still"))
    argv = bench.starts()[0]["argv"]
    for option in MPV_OPTIONS:
        assert option in argv
    assert "--drm-drmprime-video-plane=primary" in argv
    assert "--drm-draw-plane=overlay" in argv
    assert f"--drm-device={bench.runtime.parent / 'dev' / 'card1'}" in argv
    assert "--drm-connector=HDMI-A-1" in argv


def test_a_still_is_loaded_its_zero_length_read_and_not_looped(bench: Bench) -> None:
    still = bench.file("a.png", "still")
    assert bench.output.load(still) == Loaded(still.resolve(), 0.0)
    assert bench.commands() == [
        ["request_log_messages", "warn"],
        ["loadfile", str(still.resolve()), "replace"],
        ["get_property", "duration"],
        ["set_property", "loop-file", "no"],
    ]


def test_a_clip_is_loaded_with_its_length_and_loops_until_the_next_load(bench: Bench) -> None:
    clip = bench.file("b.mp4", "18.0")
    assert bench.output.load(clip) == Loaded(clip.resolve(), 18.0)
    assert ["set_property", "loop-file", "inf"] in bench.commands()


def test_the_error_line_every_healthy_start_logs_is_not_a_failure(bench: Bench) -> None:
    """mpv under systemd logs `Can't open TTY for VT control` at error level on every start (measured)."""
    still = bench.file("a.png", "still")
    assert isinstance(bench.output.load(still), Loaded)
    assert bench.output.check() is None


def test_one_mpv_serves_item_after_item(bench: Bench) -> None:
    for index in range(3):
        assert isinstance(bench.output.load(bench.file(f"{index}.png", "still")), Loaded)
    assert len(bench.starts()) == 1


# -- the failure this renderer exists for -----------------------------------------


def test_failed_commits_with_no_dropped_frames_count_as_not_shown(root: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The measured case: every atomic commit failed, mpv reported 0 drops, and the screen showed nothing."""
    bench = make(root, monkeypatch, "commit_fail")
    try:
        caplog.set_level(logging.WARNING, logger="arrt_player.mpv")
        clip = bench.file("b.mp4", "34.0")
        bench.output.load(clip)
        pid = bench.pid()
        said = None
        deadline = time.monotonic() + 3
        while said is None and time.monotonic() < deadline:
            said = bench.output.check()
            time.sleep(0.05)
        assert isinstance(said, NotShown)
        assert "Failed to commit atomic request" in said.reason
        assert wait_until(lambda: not alive(pid)), "mpv is stopped, so the next load starts it fresh"
        assert any(record.__dict__.get("event") == "mpv.not_shown" for record in caplog.records)
    finally:
        bench.output.close()


def test_a_screen_mpv_cannot_open_is_not_shown_and_restarts_back_off(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make(root, monkeypatch, "vo_fail")
    try:
        still = bench.file("a.png", "still")
        first = bench.output.load(still)
        assert isinstance(first, NotShown)
        # The first failure line mpv logs in this case (measured on the Pi).
        assert "Failed to acquire DRM master" in first.reason
        assert len(bench.starts()) == 1

        # Within the first step (1 s) no new mpv is started for the next item.
        bench.clock.now += 0.7
        waiting = bench.output.load(still)
        assert isinstance(waiting, NotShown)
        assert "waiting" in waiting.reason
        assert len(bench.starts()) == 1

        # Past it, one try; it fails again, and the next step is 2 s.
        bench.clock.now += 0.6
        assert isinstance(bench.output.load(still), NotShown)
        assert len(bench.starts()) == 2
        bench.clock.now += 1.7
        assert "waiting" in bench.output.load(still).reason
        assert len(bench.starts()) == 2
        bench.clock.now += 0.5
        bench.output.load(still)
        assert len(bench.starts()) == 3
    finally:
        bench.output.close()


def test_a_file_mpv_cannot_play_costs_that_file_and_not_mpv(bench: Bench) -> None:
    broken = bench.file("broken.mp4", "corrupt")
    said = bench.output.load(broken)
    assert isinstance(said, NotShown)
    assert "could not play the file" in said.reason
    pid = bench.pid()
    assert alive(pid)
    assert isinstance(bench.output.load(bench.file("a.png", "still")), Loaded)
    assert len(bench.starts()) == 1


def test_mpv_dying_mid_load_is_not_shown_and_the_next_load_starts_another(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make(root, monkeypatch, "die_on_load")
    try:
        still = bench.file("a.png", "still")
        assert isinstance(bench.output.load(still), NotShown)
        bench.clock.now += 1.1
        bench.output.load(still)
        assert len(bench.starts()) == 2
    finally:
        bench.output.close()


def test_check_with_nothing_loaded_says_so(root: Path) -> None:
    output = MpvOutput("hdmi-a-1", runtime_dir=root / "rt", media_root=root)
    said = output.check()
    assert isinstance(said, NotShown)
    assert "not running" in said.reason


# -- what may reach mpv -------------------------------------------------------------


@pytest.mark.parametrize("name", ["outside", "url", "directory"])
def test_only_files_under_the_media_root_ever_reach_mpv(bench: Bench, root: Path, name: str) -> None:
    outside = root / "elsewhere.png"
    outside.write_text("still")
    (bench.media / "dir").mkdir()
    path = {"outside": outside, "url": Path("https://example.com/a.mp4"), "directory": bench.media / "dir"}[name]
    said = bench.output.load(path)
    assert isinstance(said, NotShown)
    assert "refusing" in said.reason
    assert bench.lines() == [], "mpv was never started, let alone handed the path"


def test_a_path_that_climbs_out_of_the_media_root_is_refused(bench: Bench, root: Path) -> None:
    (root / "secret.png").write_text("still")
    said = bench.output.load(bench.media / ".." / "secret.png")
    assert isinstance(said, NotShown)
    assert "refusing" in said.reason


def test_a_connector_the_kernel_does_not_list_is_named(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make(root, monkeypatch, "")
    output = MpvOutput(
        "hdmi-a-2",
        runtime_dir=bench.runtime,
        media_root=bench.media,
        command=(sys.executable, str(FAKE)),
        drm_root=bench.drm,
        dev_root=root / "dev",
    )
    said = output.load(bench.file("a.png", "still"))
    assert isinstance(said, NotShown)
    assert "hdmi-a-2" in said.reason


def test_a_socket_path_too_long_for_unix_sockets_is_named(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make(root, monkeypatch, "")
    output = MpvOutput(
        "hdmi-a-1", runtime_dir=root / ("x" * 120), media_root=bench.media, drm_root=bench.drm, dev_root=root / "dev"
    )
    said = output.load(bench.file("a.png", "still"))
    assert isinstance(said, NotShown)
    assert "too long" in said.reason


# -- mpv's life ----------------------------------------------------------------------


def test_close_leaves_no_mpv_running(bench: Bench) -> None:
    bench.output.load(bench.file("a.png", "still"))
    pid = bench.pid()
    bench.output.close()
    assert wait_until(lambda: not alive(pid))
    assert not (bench.runtime / "mpv.pid").exists()
    assert not (bench.runtime / "mpv.sock").exists()


def test_an_mpv_that_ignores_quit_and_sigterm_is_killed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = make(root, monkeypatch, "stubborn")
    bench.output.load(bench.file("a.png", "still"))
    pid = bench.pid()
    bench.output.close()
    assert wait_until(lambda: not alive(pid), timeout=5)


def test_leaving_the_with_block_on_an_exception_stops_mpv(bench: Bench) -> None:
    def boom() -> None:
        with bench.output:
            bench.output.load(bench.file("a.png", "still"))
            raise RuntimeError

    with pytest.raises(RuntimeError):
        boom()
    assert wait_until(lambda: not alive(bench.pid()))


def test_an_mpv_a_killed_player_left_behind_is_stopped_at_the_next_start(bench: Bench, root: Path) -> None:
    bench.runtime.mkdir(mode=0o700)
    socket_path = bench.runtime / "mpv.sock"
    left = subprocess.Popen(  # noqa: S603 -- the fake mpv this suite ships, by its own path
        [sys.executable, str(FAKE), f"--input-ipc-server={socket_path}"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        (bench.runtime / "mpv.pid").write_text(str(left.pid))
        assert wait_until(socket_path.exists)
        assert isinstance(bench.output.load(bench.file("a.png", "still")), Loaded)
        left.wait(timeout=5)
        assert bench.pid() != left.pid
    finally:
        if left.poll() is None:
            left.kill()


def test_a_reused_pid_that_is_not_our_mpv_is_left_alone(root: Path) -> None:
    runtime = root / "rt"
    runtime.mkdir()
    bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)  # noqa: S607 -- a stand-in process, by name on PATH
    try:
        (runtime / "mpv.pid").write_text(str(bystander.pid))
        stop_stale(runtime)
        assert bystander.poll() is None
        assert not (runtime / "mpv.pid").exists()
    finally:
        bystander.send_signal(signal.SIGKILL)
        bystander.wait()


# -- as a ScreenOutput -----------------------------------------------------------------


def test_refresh_with_nothing_wrong_sends_mpv_nothing(bench: Bench) -> None:
    bench.output.show(bench.file("a.png", "still"))
    before = len(bench.commands())
    bench.output.refresh()
    assert len(bench.commands()) == before


def test_refresh_puts_the_still_back_after_mpv_died(bench: Bench) -> None:
    still = bench.file("a.png", "still")
    # `load` rather than `show`, so a first draw that failed is named here and
    # not read later as a refresh that did nothing.
    assert isinstance(bench.output.load(still), Loaded)
    first = bench.pid()
    os.killpg(first, signal.SIGKILL)
    assert wait_until(lambda: not alive(first))
    # **On Linux a killed multithreaded process shows as a zombie before its
    # parent can reap it**, while its other threads finish exiting, so the first
    # refresh may still see mpv as running, read its closed socket, and count a
    # screen failure with its 1 s backoff. Real mpv is multithreaded, so this is
    # the Pi's behaviour too. Past the backoff, the next refresh starts it again.
    bench.output.refresh()
    bench.clock.now += 1.1
    bench.output.refresh()
    assert bench.pid() != first
    assert bench.commands()[-3][:2] == ["loadfile", str(still.resolve())]


def test_the_output_reports_the_screen_as_the_kernel_lists_it(bench: Bench) -> None:
    assert bench.output.listed
    assert bench.output.connected
    assert bench.output.screen == (3840, 2160)


# -- reading mpv's events ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "screen"),
    [
        (
            {
                "event": "log-message",
                "level": "warn",
                "prefix": "vo/gpu",
                "text": "Failed to commit atomic request: Error number 22 occurred\n",
            },
            True,
        ),
        ({"event": "log-message", "level": "fatal", "prefix": "cplayer", "text": "anything at all\n"}, True),
        ({"event": "end-file", "reason": "error", "file_error": "loading failed"}, False),
    ],
)
def test_failures_are_read_from_mpvs_own_words(event: dict, screen: bool) -> None:
    said = failure_in(event)
    assert said is not None
    assert said.screen is screen


@pytest.mark.parametrize(
    "event",
    [
        {
            "event": "log-message",
            "level": "error",
            "prefix": "vo/gpu/drm",
            "text": "Can't open TTY for VT control: No such device or address\n",
        },
        {
            "event": "log-message",
            "level": "warn",
            "prefix": "vo/gpu/drm",
            "text": "Failed to set up VT switcher. Terminal switching will be unavailable.\n",
        },
        {
            "event": "log-message",
            "level": "warn",
            "prefix": "vo/gpu/drm",
            "text": "Could not find any preferred mode. Picking the first mode.\n",
        },
        {"event": "end-file", "reason": "stop"},
        {"event": "end-file", "reason": "quit"},
        {"event": "file-loaded"},
    ],
)
def test_what_a_healthy_mpv_says_is_not_a_failure(event: dict) -> None:
    assert failure_in(event) is None


# -- the socket's reading, pinned without timing luck --------------------------------------


class OneWrite:
    """A server that answers the first command with a reply and the events after it in one write, then says nothing."""

    def __init__(self, path: Path, *, answer: bool = True) -> None:
        import socket
        import threading

        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen(1)
        self.answer = answer
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        connection, _ = self.server.accept()
        request = json.loads(connection.recv(65536).split(b"\n")[0])
        if self.answer:
            batch = [
                {"data": {"playlist_entry_id": 1}, "request_id": request["request_id"], "error": "success"},
                {"event": "start-file", "playlist_entry_id": 1},
                {"event": "file-loaded"},
            ]
            connection.sendall(b"".join(json.dumps(message).encode() + b"\n" for message in batch))
        time.sleep(3)
        connection.close()


def test_events_sharing_a_write_with_the_reply_are_kept_and_found(root: Path) -> None:
    """mpv sends `loadfile`'s reply and `file-loaded` in one write at times; dropping or not seeing either loses the load."""
    from arrt_player.mpv import _Ipc

    path = root / "s"
    OneWrite(path)
    ipc = _Ipc(path, timeout=1.0)
    assert ipc.command("loadfile", "x", "replace", timeout=1.0) == {"playlist_entry_id": 1}
    started = time.monotonic()
    found = ipc.wait_for(lambda event: event.get("event") == "file-loaded", timeout=1.0)
    assert found == {"event": "file-loaded"}
    assert time.monotonic() - started < 0.2, "found among the events already held, not by waiting"
    ipc.close()


def test_a_command_mpv_never_answers_raises_at_its_timeout(root: Path) -> None:
    from arrt_player.mpv import MpvError, _Ipc

    path = root / "s"
    OneWrite(path, answer=False)
    ipc = _Ipc(path, timeout=1.0)
    started = time.monotonic()
    with pytest.raises(MpvError, match="did not answer"):
        ipc.command("loadfile", "x", "replace", timeout=0.5)
    assert time.monotonic() - started < 1.5
    ipc.close()


# -- what the review of this chunk found -----------------------------------------------------


def test_a_clip_whose_length_mpv_cannot_give_is_shown_as_a_still(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mpv answers `property unavailable` for a length it does not have (measured on the Pi): no length, not a failure."""
    bench = make(root, monkeypatch, "no_duration")
    try:
        clip = bench.file("b.mp4", "34.0")
        assert bench.output.load(clip) == Loaded(clip.resolve(), None)
        assert alive(bench.pid())
        assert ["set_property", "loop-file", "no"] in bench.commands()
        assert isinstance(bench.output.load(bench.file("a.png", "still")), Loaded)
        assert len(bench.starts()) == 1, "the same mpv carries on"
    finally:
        bench.output.close()


def test_a_screen_failure_that_arrived_after_the_last_check_is_not_lost(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit failure that came in after the last `check` must not vanish when the next item is loaded."""
    bench = make(root, monkeypatch, "commit_fail")
    try:
        bench.output.load(bench.file("b.mp4", "34.0"))
        first = bench.pid()
        time.sleep(0.3)  # the fake fails a commit every 30 ms once a file is up
        said = bench.output.load(bench.file("a.png", "still"))
        assert isinstance(said, NotShown)
        assert "Failed to commit atomic request" in said.reason
        assert wait_until(lambda: not alive(first))
    finally:
        bench.output.close()


def test_mpv_is_handed_the_resolved_path_the_check_approved(bench: Bench) -> None:
    (bench.media / "sub").mkdir()
    still = bench.file("a.png", "still")
    bench.output.load(bench.media / "sub" / ".." / "a.png")
    assert ["loadfile", str(still.resolve()), "replace"] in bench.commands()


def test_a_line_from_mpv_that_is_not_json_is_an_mpv_error(root: Path) -> None:
    import socket
    import threading

    from arrt_player.mpv import MpvError, _Ipc

    path = root / "s"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)

    def garble() -> None:
        connection, _ = server.accept()
        connection.recv(65536)
        connection.sendall(b"this is not json\n")
        time.sleep(1)
        connection.close()

    threading.Thread(target=garble, daemon=True).start()
    ipc = _Ipc(path, timeout=1.0)
    with pytest.raises(MpvError, match="not JSON"):
        ipc.command("get_property", "duration", timeout=0.5)
    ipc.close()
