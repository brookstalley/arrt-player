"""The Player tells systemd it is up, and pets the watchdog only when it has made progress.

Driven against a real `AF_UNIX` datagram socket each test binds, standing where
systemd's `/run/systemd/notify` would, so what is under test is the datagram
that leaves the process, not a call to a stand-in. The modes are driven through
the entry point (`_run`, `main`), so a mode that never says `READY=1`, or says it
for a start that was refused, fails here rather than as a unit that never
reaches `active` on the Pi. Slideshow mode's are in `test_slideshow.py`.

**No sleep of a watchdog's length.** The rate limit runs on an injected clock;
the supervisor polls every few hundredths of a second; the setup mode's period
is shortened.
"""

import asyncio
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from conftest import events
from server_double import ServerDouble

from arrt_player import __main__ as entry
from arrt_player import notify
from arrt_player.config import ConfigError, FileOutputSettings, SetupRequired, UiSettings
from arrt_player.notify import Notifier, Watchdog, socket_address
from arrt_player.pull import ClientPull
from arrt_player.wall import Clock


class Listener:
    """A datagram socket standing where systemd's notification socket would: every message it is sent, in order."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.bind(str(path))
        self._socket.setblocking(False)
        self._received: list[str] = []

    @property
    def messages(self) -> list[str]:
        while True:
            try:
                self._received.append(self._socket.recv(4096).decode())
            except BlockingIOError:
                return list(self._received)

    def count(self, message: str) -> int:
        return self.messages.count(message)

    def close(self) -> None:
        self._socket.close()


def notification_socket(monkeypatch: pytest.MonkeyPatch) -> Iterator[Listener]:
    """A notification socket at a short path, named in this process's `NOTIFY_SOCKET` as systemd names its own.

    Under `/tmp` because a socket's path has to fit `sun_path` (104 bytes on
    macOS), which a pytest temporary directory there does not.
    """
    directory = Path(tempfile.mkdtemp(prefix="notify-", dir="/tmp"))
    listener = Listener(directory / "notify.sock")
    monkeypatch.setenv(notify.NOTIFY_SOCKET, str(listener.path))
    yield listener
    listener.close()
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def systemd(monkeypatch) -> Iterator[Listener]:
    yield from notification_socket(monkeypatch)


async def eventually(predicate, *, what: str, timeout: float = 5.0) -> None:  # noqa: ASYNC109 -- the helper's own deadline
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


class Ticking:
    """A monotonic clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# -- the protocol --------------------------------------------------------------------------


def test_a_path_is_a_path_and_an_at_sign_is_the_abstract_namespace():
    assert socket_address("/run/systemd/notify") == "/run/systemd/notify"
    assert socket_address("@/org/freedesktop/systemd1/notify") == b"\0/org/freedesktop/systemd1/notify"
    assert socket_address("vsock:2:1234") is None
    assert socket_address("@") is None


def test_ready_and_a_pet_reach_the_socket_as_systemd_reads_them(systemd):
    watchdog = Watchdog(Notifier())

    watchdog.ready()
    watchdog.progress()

    assert systemd.messages == ["READY=1", "WATCHDOG=1"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the abstract namespace is Linux's")
def test_an_abstract_socket_is_reached_by_its_name(monkeypatch):
    name = f"arrt-notify-test-{os.getpid()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.bind(b"\0" + name.encode())
        listener.settimeout(2)
        Notifier(environ={notify.NOTIFY_SOCKET: f"@{name}"}).send("READY=1")
        assert listener.recv(4096) == b"READY=1"


def test_with_no_notify_socket_nothing_is_sent_and_it_is_said_once_at_debug(monkeypatch, caplog):
    monkeypatch.delenv(notify.NOTIFY_SOCKET, raising=False)
    with caplog.at_level(logging.DEBUG, logger="arrt_player.notify"):
        watchdog = Watchdog.from_environment()
        watchdog.ready()
        watchdog.progress()

    said = [(record.levelno, record.event) for record in caplog.records]
    assert said == [(logging.DEBUG, "notify.absent")]


def test_a_notify_socket_in_a_form_not_spoken_is_said_and_nothing_is_sent(caplog):
    with caplog.at_level(logging.DEBUG, logger="arrt_player.notify"):
        notifier = Notifier(environ={notify.NOTIFY_SOCKET: "vsock:2:1234"})
        sent = notifier.send("READY=1")

    assert sent is False
    assert [record.event for record in events(caplog, "notify.unusable")] == ["notify.unusable"]


def test_a_socket_that_refuses_is_said_once_until_it_takes_messages_again(systemd, caplog):
    systemd.close()
    systemd.path.unlink()
    notifier = Notifier()
    with caplog.at_level(logging.INFO, logger="arrt_player.notify"):
        assert notifier.send("WATCHDOG=1") is False
        assert notifier.send("WATCHDOG=1") is False
        back = Listener(systemd.path)
        try:
            assert notifier.send("WATCHDOG=1") is True
            assert back.messages == ["WATCHDOG=1"]
        finally:
            back.close()

    assert [record.event for record in caplog.records] == ["notify.unsent", "notify.sent"]


# -- the progress rule ---------------------------------------------------------------------


def test_nothing_but_progress_pets_and_ready_is_said_once(systemd):
    """`ready` twice is one `READY=1`, and only `progress` ever sends `WATCHDOG=1`."""
    watchdog = Watchdog(Notifier(), monotonic=Ticking(), pet_min_seconds=0.0)

    watchdog.ready()
    watchdog.ready()
    assert systemd.messages == ["READY=1"]

    watchdog.progress()
    watchdog.progress()
    assert systemd.messages == ["READY=1", "WATCHDOG=1", "WATCHDOG=1"]


def test_progress_pets_at_most_once_per_interval_on_the_clock_it_is_given(systemd):
    clock = Ticking()
    watchdog = Watchdog(Notifier(), monotonic=clock, pet_min_seconds=5.0)

    watchdog.progress()
    clock.now += 4.9
    watchdog.progress()
    assert systemd.count("WATCHDOG=1") == 1, "a pet inside the interval was sent"

    clock.now += 0.1
    watchdog.progress()
    assert systemd.count("WATCHDOG=1") == 2, "progress after the interval was not a pet"


def test_a_ready_the_socket_refused_is_sent_again_at_the_next_progress(systemd):
    """systemd fails a start that never says `READY=1`, so one refused message must not be the last word."""
    path = systemd.path
    systemd.close()
    path.unlink()
    watchdog = Watchdog(Notifier(), monotonic=Ticking(), pet_min_seconds=0.0)
    watchdog.ready()

    back = Listener(path)
    try:
        watchdog.progress()
        watchdog.progress()
        assert back.messages == ["READY=1", "WATCHDOG=1", "WATCHDOG=1"]
    finally:
        back.close()


async def test_a_task_still_running_on_a_turning_loop_pets_and_one_that_ended_does_not(systemd):
    watchdog = Watchdog(Notifier(), monotonic=Ticking(), pet_min_seconds=0.0)
    release = asyncio.Event()
    page = asyncio.create_task(release.wait())

    petting = asyncio.create_task(watchdog.while_running(page, every=0.01))
    await eventually(lambda: systemd.count("WATCHDOG=1") >= 3, what="three pets while the page runs")
    release.set()
    await asyncio.wait_for(petting, timeout=1)
    ended_at = systemd.count("WATCHDOG=1")
    await asyncio.sleep(0.05)

    assert systemd.count("WATCHDOG=1") == ended_at, "a pet was sent after the page's task ended"


# -- the modes, through the entry point -----------------------------------------------------


@pytest.fixture
async def server():
    double = ServerDouble(wall_id="living-room", output="file-screen-0")
    double.schedule_starts = datetime.now(UTC).replace(microsecond=0)
    test_server = TestServer(double.app())
    await test_server.start_server()
    double.url = str(test_server.make_url("")).rstrip("/")
    yield double
    await test_server.close()


@pytest.fixture
def page(monkeypatch, cache_dir: Path) -> UiSettings:
    """The settings page on loopback, on a port nothing else holds, so `_run` reads nothing from the real environment."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ui = UiSettings(cache_dir=cache_dir, bind="127.0.0.1", port=port, pinned=frozenset())
    monkeypatch.setattr(entry, "load_ui", lambda: ui)
    return ui


async def _until_sigterm(running: "asyncio.Task[int]") -> int:
    os.kill(os.getpid(), signal.SIGTERM)
    return await asyncio.wait_for(running, timeout=10)


async def test_arrt_mode_says_ready_once_it_runs_and_pets_after_the_supervisors_first_cycle(
    monkeypatch, systemd, server, page, client_settings
):
    """The client, with no wall to draw (no output it holds is assigned), so the supervisor's cycle is all that runs."""
    client = replace(client_settings, server_url=server.url, frame=None, client_poll_seconds=0.05)
    monkeypatch.setattr(entry, "load", lambda: client)
    monkeypatch.setattr(entry, "client_outputs", lambda *_args, **_kwargs: [])

    running = asyncio.create_task(entry._run())
    await eventually(lambda: "WATCHDOG=1" in systemd.messages, what="a pet after the first cycle")

    assert await _until_sigterm(running) == 0
    assert systemd.messages[0] == "READY=1"
    assert systemd.count("READY=1") == 1


class _Stalled(ClientPull):
    """The client's link, with a client document that never comes: the supervisor's cycle never finishes."""

    async def fetch(self):
        await asyncio.Event().wait()


async def test_a_supervisor_whose_cycle_never_finishes_never_pets(systemd, server, client_settings):
    """**The value of the watchdog**: the loop turns, the heartbeat goes, and no cycle finishes, so no pet is sent.

    Beside it the same supervisor with a link that answers, which pets in the
    same time, so a pet that does not follow the cycle shows as a difference.
    """
    client = replace(client_settings, server_url=server.url, frame=None, client_poll_seconds=0.01)

    async def pets_in(link: ClientPull) -> int:
        watchdog = Watchdog(Notifier(), pet_min_seconds=0.0)
        clock = Clock.system()
        supervisor = entry.PettingSupervisor(
            watchdog=watchdog,
            settings=client,
            link=link,
            worker=partial(entry.run_wall, client),
            outputs=list,
            now=clock.now,
            monotonic=clock.monotonic,
        )
        before = systemd.count("WATCHDOG=1")
        stop = asyncio.Event()
        running = asyncio.create_task(supervisor.run(stop))
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat, so the cycle has begun")
        await asyncio.sleep(0.3)
        stop.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await link.close()
        return systemd.count("WATCHDOG=1") - before

    assert await pets_in(_Stalled(client)) == 0, "a supervisor stuck in its cycle petted the watchdog"
    assert await pets_in(ClientPull(client)) >= 3, "a supervisor whose cycles finish did not pet"


async def test_setup_mode_says_ready_and_pets_while_its_page_is_served(monkeypatch, systemd, page):
    def setup_required():
        raise SetupRequired(("SERVER_URL", "CLIENT_TOKEN"))

    monkeypatch.setattr(entry, "load", setup_required)
    monkeypatch.setattr(notify, "SETUP_PET_SECONDS", 0.01)

    running = asyncio.create_task(entry._run())
    await eventually(lambda: "WATCHDOG=1" in systemd.messages, what="a pet while the page is served")

    assert await _until_sigterm(running) == 0
    assert systemd.messages[0] == "READY=1"


def test_a_start_refused_by_configuration_says_nothing_to_systemd_and_exits_2(monkeypatch, systemd, page):
    """A start systemd should count as failed: no `READY=1`, and the exit status the unit restarts on."""

    def refused():
        raise ConfigError("MANIFEST_POLL_SECONDS is 0, and it must be a number of seconds above zero.")

    monkeypatch.setattr(entry.logs, "configure", lambda: None)
    monkeypatch.setattr(entry, "load", refused)

    assert entry.main() == 2
    assert systemd.messages == []


async def test_with_files_drawn_the_player_is_arrt_mode_to_systemd(monkeypatch, systemd, server, page, client_settings, tmp_path):
    """The hardware-less Player: the same supervisor, so the same `READY=1` and the same pet."""
    files = FileOutputSettings(directory=tmp_path / "drawn", screen=(320, 180), label=False)
    client = replace(client_settings, server_url=server.url, frame=None, files=files, client_poll_seconds=0.05)
    monkeypatch.setattr(entry, "load", lambda: client)

    running = asyncio.create_task(entry._run())
    await eventually(lambda: "WATCHDOG=1" in systemd.messages, what="a pet after the first cycle")

    assert await _until_sigterm(running) == 0
    assert systemd.messages[0] == "READY=1"
    assert entry.player_outputs(client) == [entry.file_output.output_report(files)], "this ran as a Player with hardware"
