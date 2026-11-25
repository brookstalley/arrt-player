"""A real Player process with no hardware, against the server double: `python -m arrt_player` as systemd runs it.

**The Player's half of the test of a real Player against a real server**, which
lives in Arrt. Arrt's test starts this same command with the same environment
against its own uvicorn; this one proves the invocation against
`tests/server_double.py` first, so the server's test quotes a command already
known to work. The process is the real entry point: real configuration parsing,
the real supervisor, pull, schedule, label renderer and signal handling, with
the file outputs (`FILE_OUTPUT_DIR`) standing where a screen and a panel would.

**The process tests read what the Player drew as an outsider does**: the
layout's paths and keys are spelled there as the server's test spells them,
never imported, so a rename that would break the server's test breaks these
first. The tests below them hold each output to its interface in-process, under
the wall and the label worker that drive it.

**No fixed sleeps.** Every wait is on something the process makes true (a record
on disk, a request at the double, a journal line), with a deadline; the process
gets a private cache, output directory and settings-page port, and is always
reaped, killed if a test failed before it stopped.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image
from server_double import TOKEN, ServerDouble

from arrt_player import __main__ as entry
from arrt_player import logs
from arrt_player.client import LabelAssignment, WorkerStop
from arrt_player.config import FileOutputSettings
from arrt_player.displays.file_output import FileScreenOutput, captioning, file_label_panel
from arrt_player.displays.screen import screen_wall
from arrt_player.kms import fitted
from arrt_player.label_rule import Drawing, Outcome
from arrt_player.manifest import Watcher
from arrt_player.panel import SurfaceUnavailable
from arrt_player.panel.layout import Layout

REPOSITORY = Path(__file__).resolve().parents[1]

#: How long any one thing the process does may take to happen. Generous: the
#: first picture waits on a pull, a download and a composition, on a busy runner.
DEADLINE_SECONDS = 20.0

#: The file screen's size for these runs, small so that composing is cheap.
SCREEN = (320, 180)


def _stop_window_seconds() -> float:
    """The unit's `TimeoutStopSec`: how long systemd waits after SIGTERM before it kills."""
    unit = (REPOSITORY / "deploy" / "display.service").read_text(encoding="utf-8")
    matched = re.search(r"^TimeoutStopSec=(\d+)$", unit, re.MULTILINE)
    assert matched is not None, "the unit states no TimeoutStopSec"
    return float(matched.group(1))


#: SIGTERM must end the process inside the unit's window; this test waits less,
#: so a slow stop fails here in seconds rather than after the whole window.
STOP_SECONDS = min(_stop_window_seconds(), DEADLINE_SECONDS)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _until(predicate: Callable[[], object], *, what: str, journal: "Journal") -> None:
    """Wait for something the process will make true, or fail saying what, with the process's journal."""
    deadline = asyncio.get_running_loop().time() + DEADLINE_SECONDS
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {DEADLINE_SECONDS:g}s waiting for {what}\n{journal.tail()}")
        await asyncio.sleep(0.05)


def _read(path: Path) -> dict | None:
    """A record as the server's test reads one: absent, or a whole JSON document (it is replaced atomically)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


class Journal:
    """The process's stderr, one JSON line per record, as `journalctl` holds it."""

    def __init__(self) -> None:
        self.lines: list[dict] = []
        self.raw: list[str] = []

    async def follow(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            text = line.decode("utf-8", "replace").rstrip("\n")
            self.raw.append(text)
            try:
                self.lines.append(json.loads(text))
            except ValueError:
                continue

    def events(self, name: str) -> list[dict]:
        return [line for line in self.lines if line.get("event") == name]

    def tail(self) -> str:
        return "\n".join(self.raw[-40:])


class Running:
    """One Player process, its journal followed, reaped whatever happens."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.journal = Journal()
        self._followers = [
            asyncio.create_task(self.journal.follow(process.stderr)),
            asyncio.create_task(_drain(process.stdout)),
        ]

    async def terminate(self) -> tuple[int, float]:
        """SIGTERM, as `systemctl stop` sends it, and the exit code and how long the stop took."""
        started = asyncio.get_running_loop().time()
        self.process.send_signal(signal.SIGTERM)
        code = await asyncio.wait_for(self.process.wait(), timeout=STOP_SECONDS)
        return code, asyncio.get_running_loop().time() - started

    async def reap(self) -> None:
        if self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        await asyncio.gather(*self._followers, return_exceptions=True)


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.readline():
        pass


def player_environment(tmp_path: Path, server_url: str, token: str) -> dict[str, str]:
    """The whole environment the Player is started with: nothing inherited but `PATH`.

    `PYTHON_DOTENV_DISABLED` keeps any `.env` above the checkout from filling the
    keys left out here (`config.load` reads one from its own directory upward).
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "PYTHON_DOTENV_DISABLED": "1",
        "CACHE_DIR": str(tmp_path / "cache"),
        "SERVER_URL": server_url,
        "CLIENT_TOKEN": token,
        "FILE_OUTPUT_DIR": str(tmp_path / "drawn"),
        "FILE_OUTPUT_SCREEN": f"{SCREEN[0]}x{SCREEN[1]}",
        "FILE_OUTPUT_LABEL": "on",
        "MANIFEST_POLL_SECONDS": "0.2",
        "UI_BIND": "127.0.0.1",
        "UI_PORT": str(_free_port()),
    }


async def start_player(environment: dict[str, str]) -> Running:
    """`python -m arrt_player`, on the suite's own interpreter, from outside the checkout."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "arrt_player",
        env=environment,
        cwd=environment["HOME"],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return Running(process)


@pytest.fixture
async def server():
    """The double, on a port of its own, recording the status of every answer it gives."""
    double = ServerDouble(wall_id="living-room", output="file-screen-0")
    # The process runs on the system clock, so the wall's schedule starts now.
    double.schedule_starts = datetime.now(UTC).replace(microsecond=0)
    double.answered = []

    @web.middleware
    async def answered(request: web.Request, handler):
        response = await handler(request)
        double.answered.append((request.method, request.path, response.status))
        return response

    application = double.app()
    application.middlewares.append(answered)
    test_server = TestServer(application, host="127.0.0.1")
    await test_server.start_server()
    double.url = str(test_server.make_url("")).rstrip("/")
    yield double
    await test_server.close()


@pytest.fixture
async def player(tmp_path: Path):
    """Starts Players and reaps every one, a test's failure included."""
    (tmp_path / "home").mkdir()
    started: list[Running] = []

    async def start(server_url: str, token: str) -> Running:
        running = await start_player(player_environment(tmp_path, server_url, token))
        started.append(running)
        return running

    yield start
    for running in started:
        await running.reap()


def _label_document(work_id: str) -> dict:
    return {
        "schema": {"major": 1, "minor": 0},
        "wall_id": "living-room",
        "wall_name": "Living room",
        "display_state": {
            "state": "showing_art",
            "work_id": work_id,
            "since": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
        "label": {"title": f"Title of {work_id}"},
    }


async def test_a_player_with_no_hardware_pulls_draws_captions_reports_and_stops_on_sigterm(server, player, tmp_path):
    server.publish("w-1", "w-2", interval_seconds=600)
    server.map_label("lab-1", "file-label-0", "living-room", _label_document("w-1"))
    drawn = tmp_path / "drawn"

    running = await player(server.url, TOKEN)
    journal = running.journal

    # It says once, at start, that it draws to files.
    await _until(lambda: journal.events("files.drawing"), what="the start line saying it draws to files", journal=journal)
    assert journal.events("files.drawing")[0]["level"] == "WARNING"

    # The client document is polled.
    await _until(lambda: ("GET", "/client", 200) in server.answered, what="the client document to be polled", journal=journal)

    # The wall's feed is pulled, adopted and drawn to the file screen.
    screen = drawn / "file-screen-0"
    await _until(
        lambda: (_read(screen / "shown.json") or {}).get("work_id") == "w-1",
        what="the wall's first work to be drawn to the file screen",
        journal=journal,
    )
    record = _read(screen / "shown.json")
    assert record["format"] == 1
    assert (record["output"], record["wall_id"], record["outcome"]) == ("file-screen-0", "living-room", "picture")
    assert record["screen"] == list(SCREEN)
    datetime.fromisoformat(record["shown_at"])
    picture = (screen / record["picture"]).read_bytes()
    assert hashlib.sha256(picture).hexdigest() == record["picture_sha256"]
    with Image.open(screen / record["picture"]) as image:
        assert image.size == SCREEN

    # A client heartbeat listing the file outputs, and a wall heartbeat, reach the double and validate.
    await _until(lambda: server.client_heartbeats, what="a client heartbeat", journal=journal)
    beat = server.client_heartbeats[0]
    assert beat["outputs"] == [{"name": "file-screen-0", "kind": "framebuffer", "connected": True, "screen": list(SCREEN)}]
    assert [output["name"] for output in beat["label_outputs"]] == ["file-label-0"]
    await _until(lambda: server.heartbeats_by_wall.get("living-room"), what="a wall heartbeat", journal=journal)
    capabilities = server.heartbeats_by_wall["living-room"][0]["capabilities"]
    assert capabilities["backend"] == "framebuffer"
    assert ("POST", "/client/heartbeat", 400) not in server.answered
    assert ("POST", "/walls/living-room/heartbeat", 400) not in server.answered

    # The label mapped to the file label output is polled and its decision written.
    label = drawn / "file-label-0"
    await _until(lambda: _read(label / "shown.json"), what="the label's decision to be written", journal=journal)
    decided = _read(label / "shown.json")
    assert ("GET", "/labels/lab-1", 200) in server.answered
    assert decided["format"] == 1
    assert (decided["output"], decided["label_id"], decided["wall_id"]) == ("file-label-0", "lab-1", "living-room")
    assert (decided["outcome"], decided["state"], decided["work_id"]) == ("caption", "showing_art", "w-1")
    assert "Title of w-1" in decided["lines"]

    # The next manifest poll, with nothing republished, is a 304.
    await _until(
        lambda: ("GET", "/walls/living-room/manifest/v2", 304) in server.answered,
        what="a manifest poll answered 304",
        journal=journal,
    )

    # SIGTERM ends it cleanly, inside the unit's stop window.
    code, took = await running.terminate()
    assert code == 0, journal.tail()
    assert took < STOP_SECONDS
    await running.reap()
    assert journal.events("client.stopped"), journal.tail()
    assert len(journal.events("files.drawing")) == 1, "the start line saying it draws to files was not said once"
    assert not [line for line in journal.lines if line.get("level") in ("ERROR", "CRITICAL")], journal.tail()


async def test_a_player_given_a_token_the_server_does_not_know_says_so_and_starts_no_wall(server, player, tmp_path):
    server.publish("w-1", interval_seconds=600)
    server.map_label("lab-1", "file-label-0", "living-room", _label_document("w-1"))

    running = await player(server.url, "not-a-token-this-server-issued")
    journal = running.journal

    await _until(
        lambda: [line for line in journal.events("client.refused") if line.get("status") == 401],
        what="the journal to say the server refused the token",
        journal=journal,
    )
    assert "CLIENT_TOKEN" in journal.events("client.refused")[0]["message"]

    code, _took = await running.terminate()
    assert code == 0, journal.tail()
    await running.reap()
    assert not journal.events("client.wall_started"), journal.tail()
    assert not journal.events("client.label_started"), journal.tail()
    assert {path for _method, path in server.requests} <= {"/client", "/client/heartbeat"}
    assert not (tmp_path / "drawn" / "file-screen-0" / "shown.json").exists()
    assert not (tmp_path / "drawn" / "file-label-0" / "shown.json").exists()


# -- the file screen, under the wall that draws on it ----------------------------------------


@pytest.fixture
def files(tmp_path: Path):
    return FileOutputSettings(directory=tmp_path / "drawn", screen=SCREEN, label=True)


class _Ticking:
    """A clock for records: each reading a second after the last, so a rewrite is visible in `shown_at`."""

    def __init__(self) -> None:
        self.at = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


@pytest.fixture
def drawn_on(files):
    return FileScreenOutput(files, wall_id="living-room", now=_Ticking())


def _a_render(path: Path, colour=(200, 30, 30), size=(160, 90)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, format="JPEG", quality=95)
    return path


async def test_a_wall_on_the_file_screen_draws_its_scheduled_work_and_names_it(
    client_settings, wall_dir, drawn_on, publish, clock
):
    """Through the wall: the schedule composes for the file screen's size and binds the work it shows."""
    wall = client_settings.wall("living-room")
    publish.geometry = wall.geometry_for(SCREEN)
    publish(["w1", "w2"], interval_seconds=60)
    screen = screen_wall(wall=wall, output=drawn_on, watcher=Watcher(wall.manifest_path), clock=clock.as_clock())

    await screen.tick()

    record = _read(drawn_on.directory / "shown.json")
    assert (record["wall_id"], record["work_id"], record["outcome"]) == ("living-room", "w1", "picture")
    assert record["render"] == str(publish.picture("w1"))
    with Image.open(drawn_on.directory / "picture.png") as image:
        assert image.size == SCREEN
        assert image.convert("RGB").tobytes() == fitted(publish.picture("w1"), SCREEN).tobytes()


def test_a_picture_drawn_outside_a_works_binding_names_no_work(drawn_on, tmp_path):
    drawn_on.show(_a_render(tmp_path / "render.jpg"))

    assert _read(drawn_on.directory / "shown.json")["work_id"] is None


def test_refresh_redraws_a_picture_gone_from_the_directory_and_otherwise_writes_nothing(drawn_on, tmp_path):
    drawn_on.refresh()
    assert not drawn_on.directory.exists(), "a refresh with nothing shown yet drew something"

    with logs.work_context("w-7"):
        drawn_on.show(_a_render(tmp_path / "render.jpg"))
    first = _read(drawn_on.directory / "shown.json")
    drawn_on.refresh()
    assert _read(drawn_on.directory / "shown.json") == first, "a refresh with the picture in place rewrote it"

    (drawn_on.directory / "picture.png").unlink()
    drawn_on.refresh()
    again = _read(drawn_on.directory / "shown.json")
    assert (drawn_on.directory / "picture.png").is_file()
    assert again["work_id"] == "w-7", "the redraw forgot which work it is"
    assert again["shown_at"] > first["shown_at"]


def test_blank_draws_black_says_blank_and_forgets_the_render(drawn_on, tmp_path):
    with logs.work_context("w-7"):
        drawn_on.show(_a_render(tmp_path / "render.jpg"))
    drawn_on.blank()

    record = _read(drawn_on.directory / "shown.json")
    assert (record["outcome"], record["work_id"], record["render"]) == ("blank", None, None)
    with Image.open(drawn_on.directory / "picture.png") as image:
        assert image.size == SCREEN
        assert image.convert("RGB").getextrema() == ((0, 0), (0, 0), (0, 0))
    (drawn_on.directory / "picture.png").unlink()
    drawn_on.refresh()
    assert not (drawn_on.directory / "picture.png").exists(), "a refresh after a blank drew the old render again"


def test_a_record_names_the_picture_it_describes(drawn_on, tmp_path):
    drawn_on.show(_a_render(tmp_path / "render.jpg"))

    record = _read(drawn_on.directory / "shown.json")
    data = (drawn_on.directory / record["picture"]).read_bytes()
    assert record["picture_sha256"] == hashlib.sha256(data).hexdigest()
    assert sorted(path.name for path in drawn_on.directory.iterdir()) == ["picture.png", "shown.json"]


def test_a_write_that_fails_leaves_the_last_record_whole_and_no_temporary_file(drawn_on, tmp_path, monkeypatch):
    drawn_on.show(_a_render(tmp_path / "render.jpg"))
    before = (drawn_on.directory / "shown.json").read_bytes()

    def no_disk(_descriptor):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", no_disk)
    with pytest.raises(OSError, match="No space"):
        drawn_on.show(_a_render(tmp_path / "other.jpg", colour=(0, 200, 0)))

    assert (drawn_on.directory / "shown.json").read_bytes() == before
    assert sorted(path.name for path in drawn_on.directory.iterdir()) == ["picture.png", "shown.json"]


def _a_disk_that_refuses(monkeypatch, *, naming: str):
    """Refuse the open of each staged file whose name contains `naming`, as a full card does; returns the switch that heals it."""
    real_open, refusing = os.open, {"on": True}

    def open_(name, *rest, **kwargs):
        if refusing["on"] and naming in Path(name).name:
            raise OSError(28, "No space left on device")
        return real_open(name, *rest, **kwargs)

    monkeypatch.setattr(os, "open", open_)
    return lambda: refusing.update(on=False)


def _agree(output: FileScreenOutput) -> dict:
    """The record the output's directory holds, checked against the picture beside it; returns the record."""
    record = _read(output.directory / "shown.json")
    data = (output.directory / record["picture"]).read_bytes()
    assert record["picture_sha256"] == hashlib.sha256(data).hexdigest(), "the record describes another picture than the one beside it"
    return record


def test_a_picture_whose_write_failed_is_on_the_disk_after_the_next_refresh(drawn_on, tmp_path, monkeypatch):
    """What someone reading the directory sees: the picture that was meant to be shown, not the old one for good."""
    with logs.work_context("w-1"):
        drawn_on.show(_a_render(tmp_path / "one.jpg"))
    heal = _a_disk_that_refuses(monkeypatch, naming="picture.png")

    with logs.work_context("w-2"), pytest.raises(OSError, match="No space"):
        drawn_on.show(_a_render(tmp_path / "two.jpg", colour=(0, 200, 0)))

    assert _agree(drawn_on)["work_id"] == "w-1", "the record named a work whose picture was not written"
    with pytest.raises(OSError, match="No space"):
        drawn_on.refresh()
    heal()
    drawn_on.refresh()

    record = _agree(drawn_on)
    assert (record["work_id"], record["render"]) == ("w-2", str(tmp_path / "two.jpg"))
    with Image.open(drawn_on.directory / "picture.png") as image:
        assert image.convert("RGB").tobytes() == fitted(tmp_path / "two.jpg", SCREEN).tobytes()
    assert sorted(path.name for path in drawn_on.directory.iterdir()) == ["picture.png", "shown.json"]


def test_a_picture_written_beside_a_record_that_could_not_be_is_told_apart_by_its_hash_and_made_whole_by_the_next_refresh(
    drawn_on, tmp_path, monkeypatch
):
    """The picture goes first, so a record never names a picture that is not there; the cost is a window with a new picture beside the old record."""
    with logs.work_context("w-1"):
        drawn_on.show(_a_render(tmp_path / "one.jpg"))
    old_record = (drawn_on.directory / "shown.json").read_bytes()
    heal = _a_disk_that_refuses(monkeypatch, naming="shown.json")

    with logs.work_context("w-2"), pytest.raises(OSError, match="No space"):
        drawn_on.show(_a_render(tmp_path / "two.jpg", colour=(0, 200, 0)))

    # On the disk now: the record as it was, and a picture that is no longer the one it hashes.
    assert (drawn_on.directory / "shown.json").read_bytes() == old_record
    stale = _read(drawn_on.directory / "shown.json")
    assert stale["picture_sha256"] != hashlib.sha256((drawn_on.directory / "picture.png").read_bytes()).hexdigest()
    assert sorted(path.name for path in drawn_on.directory.iterdir()) == ["picture.png", "shown.json"]

    heal()
    drawn_on.refresh()

    assert _agree(drawn_on)["work_id"] == "w-2"


def test_a_blank_whose_write_failed_is_drawn_by_the_next_refresh(drawn_on, tmp_path, monkeypatch):
    with logs.work_context("w-1"):
        drawn_on.show(_a_render(tmp_path / "one.jpg"))
    heal = _a_disk_that_refuses(monkeypatch, naming="picture.png")

    with pytest.raises(OSError, match="No space"):
        drawn_on.blank()
    assert _agree(drawn_on)["outcome"] == "picture", "the record said blank for a screen still showing the picture"
    heal()
    drawn_on.refresh()

    record = _agree(drawn_on)
    assert (record["outcome"], record["work_id"]) == ("blank", None)
    drawn_on.refresh()
    assert _read(drawn_on.directory / "shown.json") == record, "a refresh after the blank was drawn rewrote it"


async def test_a_file_screen_that_cannot_be_written_costs_the_picture_and_says_so(
    client_settings, wall_dir, files, publish, clock, caplog
):
    """The screen display's own answer to a refused picture, reached by a real write failing."""
    files.directory.mkdir(parents=True)
    (files.directory / "file-screen-0").write_text("a file where the output's directory should be")
    wall = client_settings.wall("living-room")
    publish.geometry = wall.geometry_for(SCREEN)
    publish(["w1"], interval_seconds=60)
    screen = screen_wall(
        wall=wall,
        output=FileScreenOutput(files, wall_id="living-room"),
        watcher=Watcher(wall.manifest_path),
        clock=clock.as_clock(),
    )

    with caplog.at_level(logging.WARNING):
        await screen.tick()

    assert [record for record in caplog.records if getattr(record, "event", None) == "screen.draw_failed"]


# -- the file label, under the label worker that draws on it ---------------------------------


def _assignment():
    return LabelAssignment(label_id="lab-1", output="file-label-0", wall_id="living-room")


async def test_each_decision_is_written_with_its_label_wall_and_work(files):
    panel = file_label_panel(files, now=_Ticking())
    record = files.directory / "file-label-0" / "shown.json"

    with captioning(_assignment()):
        await panel.draw(
            Drawing(Outcome.CAPTION, label={"title": "The Great Wave"}, work_id="w-1", state="showing_art"), label_id="lab-1"
        )
        caption = _read(record)
        await panel.draw(Drawing(Outcome.CARD, wall_name="Living room", state="silent"), label_id="lab-1")
        card = _read(record)
        await panel.draw(Drawing(Outcome.BLANK, state="in_use"), label_id="lab-1")
        blank = _read(record)

    assert caption["format"] == 1
    assert (caption["output"], caption["label_id"], caption["wall_id"]) == ("file-label-0", "lab-1", "living-room")
    assert (caption["outcome"], caption["state"], caption["work_id"]) == ("caption", "showing_art", "w-1")
    assert caption["label"] == {"title": "The Great Wave"}
    assert "The Great Wave" in caption["lines"]
    assert (card["outcome"], card["wall_name"], card["lines"]) == ("card", "Living room", ["Living room"])
    assert (blank["outcome"], blank["state"], blank["lines"]) == ("blank", "in_use", [])
    assert panel.report().connected
    datetime.fromisoformat(blank["shown_at"])


async def test_the_label_worker_binds_its_mapping_so_the_blank_it_leaves_names_its_wall(client_settings, files):
    """Through `run_label`: a renderer retired before it started leaves the panel blank, as `unmapped`."""
    panel = file_label_panel(files)
    stop = WorkerStop()
    stop.retire()

    await entry.run_label(client_settings, {panel.name: panel}, _assignment(), stop)

    record = _read(files.directory / "file-label-0" / "shown.json")
    assert (record["outcome"], record["state"], record["label_id"], record["wall_id"]) == (
        "blank",
        "unmapped",
        "lab-1",
        "living-room",
    )


async def test_a_file_label_that_cannot_be_written_is_a_label_output_not_connected(files, caplog):
    files.directory.mkdir(parents=True)
    (files.directory / "file-label-0").write_text("a file where the output's directory should be")
    panel = file_label_panel(files)

    with caplog.at_level(logging.WARNING), captioning(_assignment()):
        await panel.draw(Drawing(Outcome.BLANK, state="dark"), label_id="lab-1")

    assert not panel.report().connected
    assert [record for record in caplog.records if getattr(record, "event", None) == "label.failed"]


def test_a_layout_with_no_drawing_bound_is_refused_rather_than_recorded_as_nothing(files):
    surface = file_label_panel(files).surface

    with pytest.raises(SurfaceUnavailable, match="no drawing"):
        surface.show(Layout(surface=surface.geometry, blocks=(), dropped=()))
    assert not (files.directory / "file-label-0").exists()
