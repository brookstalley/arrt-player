"""The Player as a client: one process, one worker per wall the server assigns it.

Driven against the server double (`server_double.py`), with the real workers the
entry point runs — the Frame's loop over a television double, and the screen
loop over a recording output — so what is under test is the supervisor and the
composition together, as the process runs them. `clients.md` § The Player and
`player-contract.md` § Transport are the specification.
"""

import ast
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import signal
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from fakes import FakeTv, RecordingOutput, drm_tree
from server_double import CLIENT_HEARTBEAT, CLIENT_ID, ServerDouble

from arrt_player import __main__ as entry
from arrt_player import client as client_module
from arrt_player import logs
from arrt_player.client import (
    Assignment,
    ClientDocument,
    FrameIdentity,
    LabelAssignment,
    LabelOutputReport,
    OutputReport,
    Supervisor,
    client_heartbeat,
    client_outputs,
    hdmi_outputs,
    settings_url,
)
from arrt_player.compose import Geometry, composed_path, mat_mode
from arrt_player.config import COMPOSED_DIRNAME, ClientSettings, UiSettings
from arrt_player.label_renderer import LabelPanel
from arrt_player.pull import ClientPull
from arrt_player.wall import Clock

#: The connectors of a Pi with one screen plugged in and one socket empty — and,
#: beside them, connectors that are not HDMI and must not become outputs.
CONNECTORS = {
    "card1-HDMI-A-1": ("connected", "1920x1080\n1280x720\n"),
    "card1-HDMI-A-2": ("disconnected", ""),
    "card1-Writeback-1": ("unknown", ""),
    "card0-DSI-1": ("connected", "800x480\n"),
}


async def eventually(
    predicate,
    *,
    timeout: float = 5.0,  # noqa: ASYNC109 -- a test helper's own deadline, not a cancellation scope
    what: str = "the condition",
) -> None:
    """Wait for something the running process will make true, or fail saying what."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


@pytest.fixture
async def server():
    double = ServerDouble(wall_id="living-room", output="frame")
    # These tests run the process on the system clock, so its schedules start now.
    double.schedule_starts = datetime.now(UTC).replace(microsecond=0)
    test_server = TestServer(double.app())
    await test_server.start_server()
    double.server = test_server
    double.url = str(test_server.make_url("")).rstrip("/")
    yield double
    await double.server.close()


@pytest.fixture
def drm(tmp_path: Path) -> Path:
    return drm_tree(tmp_path / "drm", CONNECTORS)


@pytest.fixture
def client(client_settings, server):
    """This client, pointed at the double, polling fast enough for a test to watch."""
    return replace(client_settings, server_url=server.url, poll_interval_seconds=0.02, client_poll_seconds=0.05)


@pytest.fixture
def screens(monkeypatch) -> dict[str, RecordingOutput]:
    """Every screen output the entry point builds, by wall, recording what it draws."""
    built: dict[str, RecordingOutput] = {}

    def recording(wall, output):
        built[wall.wall_id] = RecordingOutput()
        return built[wall.wall_id]

    monkeypatch.setattr(entry, "screen_output", recording)
    return built


@pytest.fixture
def television(monkeypatch, tv: FakeTv) -> FakeTv:
    monkeypatch.setattr(entry, "SamsungTv", lambda **kwargs: tv)
    return tv


def supervisor_for(client, drm: Path, *, worker=None, link=None, outputs=None, monotonic=None, **overrides) -> Supervisor:
    clock = Clock.system()
    return Supervisor(
        settings=client,
        link=link if link is not None else ClientPull(client),
        worker=worker if worker is not None else partial(entry.run_wall, client),
        outputs=outputs if outputs is not None else partial(client_outputs, client, drm_root=drm),
        now=clock.now,
        monotonic=monotonic if monotonic is not None else clock.monotonic,
        **overrides,
    )


class Running:
    """A supervisor running as a task, stopped and awaited on the way out of a test."""

    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor
        self.stop = asyncio.Event()

    async def __aenter__(self) -> Supervisor:
        self.task = asyncio.create_task(self.supervisor.run(self.stop))
        return self.supervisor

    async def __aexit__(self, *exc) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=5)


def _pictures(server: ServerDouble, wall_dir: Path, geometry: Geometry) -> dict[Path, str]:
    """Each work of a wall's feed by the file it composes to for `geometry`."""
    feed = server.manifests[wall_dir.name]
    mode = mat_mode((feed.get("settings") or {}).get("mat", {}).get("mode"))
    return {
        composed_path(
            wall_dir / COMPOSED_DIRNAME,
            master_sha256=work["media"]["sha256"],
            mat_color=work["mat_color"],
            mode=mode,
            geometry=geometry,
        ): work_id
        for work_id, work in feed["works"].items()
    }


def _shown_ids(output: RecordingOutput, server: ServerDouble, client: ClientSettings, wall_id: str) -> list[str]:
    """The work ids a recording output drew, read back through the files their masters compose to."""
    by_picture = _pictures(server, client.cache_dir / wall_id, client.wall(wall_id).geometry_for(output.screen))
    return [by_picture.get(path, path.name) for path in output.shown]


# -- walls come and go -------------------------------------------------------------------


async def test_a_wall_assigned_to_this_client_starts_a_worker_that_shows_it(client, server, drm, television):
    server.publish("w1", "w2", wall_id="living-room")

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: television.on_the_wall is not None, what="the Frame to show the wall")

        assert supervisor.running == {"living-room": "frame"}
        assert television.on_the_wall.parent == client.cache_dir / "living-room" / COMPOSED_DIRNAME


async def test_a_wall_assigned_later_is_started_on_the_next_poll(client, server, drm, screens):
    server.unassign("living-room")
    server.publish("h1", wall_id="hall")

    async with Running(supervisor_for(client, drm)) as supervisor:
        await asyncio.sleep(0.15)
        assert supervisor.running == {}, "a client with no walls started one"

        server.assign("hall", "hdmi-a-1")
        await eventually(lambda: "hall" in screens and screens["hall"].shown, what="the new wall to be drawn")
        assert supervisor.running == {"hall": "hdmi-a-1"}


async def test_a_wall_taken_away_stops_its_worker_and_closes_the_art_channel(client, server, drm, television):
    server.publish("w1", wall_id="living-room")

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: television.on_the_wall is not None, what="the Frame to show the wall")
        server.unassign("living-room")

        await eventually(lambda: supervisor.running == {}, what="the worker to stop")
        await eventually(lambda: television.closed >= 1, what="the art channel to be closed")


class SlowToCloseTv(FakeTv):
    """A set whose art channel takes a moment to close, as a real websocket's does."""

    async def close(self) -> None:
        await asyncio.sleep(0.2)
        await super().close()


async def test_a_wall_moved_to_another_output_is_restarted_there_once_the_old_worker_has_closed(monkeypatch, client, server, drm):
    """**One worker per wall at a time**, even across a move: the new one starts
    only once the old one has finished closing, so two pulls never share the
    wall's directory and the Frame's art channel is closed before anything else
    shows the wall."""
    television = SlowToCloseTv()
    monkeypatch.setattr(entry, "SamsungTv", lambda **kwargs: television)
    closed_at_first_draw: list[int] = []

    class Watching(RecordingOutput):
        def show(self, render):
            if not self.shown:
                closed_at_first_draw.append(television.closed)
            super().show(render)

    screens: dict[str, RecordingOutput] = {}
    monkeypatch.setattr(entry, "screen_output", lambda wall, output: screens.setdefault(wall.wall_id, Watching()))
    server.publish("w1", wall_id="living-room")

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: television.on_the_wall is not None, what="the Frame to show the wall")
        server.assign("living-room", "hdmi-a-1")

        await eventually(lambda: supervisor.running == {"living-room": "hdmi-a-1"}, what="the move")
        await eventually(lambda: "living-room" in screens and screens["living-room"].shown, what="the screen to draw")

    assert closed_at_first_draw == [1], "the screen drew the wall while the Frame's worker was still closing"


async def test_two_walls_on_two_outputs_each_follow_their_own_schedule(client, server, drm, television, screens):
    """The Frame and a screen at once, from one process, each with only its own works."""
    server.assign("hall", "hdmi-a-1")
    server.publish("lr-1", "lr-2", wall_id="living-room", interval_seconds=1)
    server.publish("hall-1", "hall-2", wall_id="hall", interval_seconds=1)

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: len(set(television.selected)) >= 2, timeout=8, what="the Frame to rotate")
        await eventually(
            lambda: "hall" in screens and len(set(screens["hall"].shown)) >= 2, timeout=8, what="the screen to rotate"
        )

        assert supervisor.running == {"living-room": "frame", "hall": "hdmi-a-1"}
    on_the_frame = {television.holding[content] for content in television.selected}
    living_room = _pictures(server, client.cache_dir / "living-room", client.frame_wall("living-room").geometry)
    assert on_the_frame <= set(living_room), "the Frame showed another wall's work"
    assert set(_shown_ids(screens["hall"], server, client, "hall")) == {"hall-1", "hall-2"}
    assert all(path.parent == client.cache_dir / "hall" / COMPOSED_DIRNAME for path in screens["hall"].shown)


async def test_a_wall_on_an_output_this_client_lacks_is_reported_once_and_not_started(client, server, drm, screens, caplog):
    server.unassign("living-room")
    server.assign("study", "hdmi-a-3")
    server.assign("hall", "hdmi-a-1")
    server.publish("h1", wall_id="hall")

    with caplog.at_level(logging.ERROR, logger="arrt_player.client"):
        async with Running(supervisor_for(client, drm)) as supervisor:
            await eventually(lambda: "hall" in screens and screens["hall"].shown, what="the placeable wall to start")
            # A change elsewhere in the document, so it is read again and the
            # same impossible assignment met a second time.
            server.assign("porch", "hdmi-a-2")
            await eventually(lambda: "porch" in supervisor.running, what="the second document to be read")

            assert supervisor.running == {"hall": "hdmi-a-1", "porch": "hdmi-a-2"}

    reports = [record for record in caplog.records if record.__dict__.get("event") == "client.wall_unplaceable"]
    assert len(reports) == 1, f"an unplaceable wall was reported {len(reports)} times"
    assert reports[0].wall_id == "study"
    assert reports[0].output == "hdmi-a-3"


async def test_a_client_with_no_frame_does_not_start_a_wall_assigned_to_one(client, server, drm, caplog):
    without_frame = replace(client, frame=None)
    server.publish("w1", wall_id="living-room")

    with caplog.at_level(logging.ERROR, logger="arrt_player.client"):
        async with Running(supervisor_for(without_frame, drm)) as supervisor:
            await asyncio.sleep(0.15)
            assert supervisor.running == {}

    assert [record.output for record in caplog.records if record.__dict__.get("event") == "client.wall_unplaceable"] == ["frame"]


# -- every failure keeps the walls running ------------------------------------------------


async def test_the_server_unreachable_keeps_every_worker_on_its_cache(client, server, drm, screens):
    server.unassign("living-room")
    server.assign("hall", "hdmi-a-1")
    server.publish("h1", "h2", wall_id="hall", interval_seconds=1)

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: "hall" in screens and screens["hall"].shown, what="the wall to be drawn")
        await server.server.close()
        drawn = len(screens["hall"].shown)

        await eventually(lambda: len(screens["hall"].shown) > drawn, timeout=4, what="the wall to rotate from its cache")
        assert supervisor.running == {"hall": "hdmi-a-1"}, "losing the server stopped a wall"
    assert set(_shown_ids(screens["hall"], server, client, "hall")) == {"h1", "h2"}


async def test_a_client_restarted_while_the_server_is_down_starts_the_walls_it_last_knew(client, server, drm, screens):
    server.unassign("living-room")
    server.assign("hall", "hdmi-a-1")
    server.publish("h1", wall_id="hall")
    async with Running(supervisor_for(client, drm)):
        await eventually(lambda: "hall" in screens and screens["hall"].shown, what="the wall to be drawn")
    await server.server.close()
    screens.clear()

    async with Running(supervisor_for(client, drm)) as restarted:
        await eventually(lambda: "hall" in screens and screens["hall"].shown, what="the wall to be drawn from the cache")
        assert restarted.running == {"hall": "hdmi-a-1"}


async def test_the_startup_line_names_the_player_as_its_server_does(client, drm, caplog):
    """The name it goes by is the server's once there is one; the startup line saying the local
    `PLAYER_NAME` instead would contradict the settings page for the same Player."""
    client = replace(client, player_name="Our own name")
    client.cache_dir.mkdir(parents=True, exist_ok=True)
    client.client_document_path.write_text(
        json.dumps({"client_id": CLIENT_ID, "name": "The server's name", "walls": []}), encoding="utf-8"
    )

    with caplog.at_level(logging.INFO, logger="arrt_player.client"):
        async with Running(supervisor_for(client, drm)):
            await eventually(
                lambda: any(record.__dict__.get("event") == "client.started" for record in caplog.records),
                what="the startup line",
            )

    started = next(record for record in caplog.records if record.__dict__.get("event") == "client.started")
    assert started.player_name == "The server's name"


@pytest.mark.parametrize(
    "fault",
    ["token-refused", "document-unreadable", "server-error"],
)
async def test_a_client_document_the_server_will_not_give_keeps_the_walls_and_is_said_once(
    client, server, drm, television, caplog, fault
):
    server.publish("w1", wall_id="living-room")

    with caplog.at_level(logging.WARNING, logger="arrt_player.pull"):
        async with Running(supervisor_for(client, drm)) as supervisor:
            await eventually(lambda: television.on_the_wall is not None, what="the Frame to show the wall")
            if fault == "token-refused":
                server.tokens = {}
            elif fault == "document-unreadable":
                server.client_body = json.dumps({"client_id": CLIENT_ID, "name": "x", "walls": {"living-room": "frame"}}).encode()
            else:
                server.client_status = 503
            await asyncio.sleep(0.3)  # several polls

            assert supervisor.running == {"living-room": "frame"}, "a failed poll stopped a wall"

    events = [record.__dict__.get("event") for record in caplog.records]
    expected = {
        "token-refused": "client.refused",
        "document-unreadable": "client.document_refused",
        "server-error": "client.unreachable",
    }
    assert events.count(expected[fault]) == 1, events


async def test_an_unchanged_client_document_is_asked_for_with_its_etag(client, server):
    link = ClientPull(client)
    try:
        first = await link.fetch()
        second = await link.fetch()
    finally:
        await link.close()

    assert first == ClientDocument(
        client_id=CLIENT_ID, name="The Pi in the hall", walls=(Assignment("living-room", "living-room", "frame"),)
    )
    assert second is None, "an unchanged document was handed over again"
    assert link.cached() == first


async def test_a_document_cached_without_its_etag_leaves_no_older_etag_beside_it(monkeypatch, client, server):
    """The document is replaced, and then the ETag write fails (a full disk, or a
    crash between the two). The ETag left must not be the old document's: sent
    with the new document cached, it would get a 304 for the old one if the
    server went back to it, and the walls would follow a document it no longer
    serves."""
    from arrt_player import pull as pull_module

    link = ClientPull(client)
    etag_path = client.cache_dir / pull_module.CLIENT_ETAG_FILENAME
    try:
        await link.fetch()
        assert etag_path.is_file(), "the first document was not cached with its ETag"

        server.clients[CLIENT_ID]["name"] = "The Pi in the kitchen"
        real_write = pull_module._write_atomically

        def etag_write_fails(path: Path, data: bytes) -> None:
            if path == etag_path:
                raise OSError(28, "No space left on device")
            real_write(path, data)

        monkeypatch.setattr(pull_module, "_write_atomically", etag_write_fails)
        await link.fetch()
    finally:
        await link.close()

    assert json.loads(client.client_document_path.read_text())["name"] == "The Pi in the kitchen"
    assert not etag_path.exists()


def _etag_not_utf8(path: Path) -> None:
    path.write_bytes(b'\xff\xfe"stale"')


def _etag_unreadable(path: Path) -> None:
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize("spoil", [_etag_not_utf8, _etag_unreadable], ids=["not-utf8", "unreadable"])
async def test_a_spoiled_etag_is_not_sent_and_the_whole_document_comes_back(client, server, spoil):
    from arrt_player import pull as pull_module

    link = ClientPull(client)
    try:
        first = await link.fetch()
        spoil(client.cache_dir / pull_module.CLIENT_ETAG_FILENAME)
        sent_before = len(server.requests)
        second = await link.fetch()
    finally:
        await link.close()

    assert second == first, "a spoiled ETag cost the client its document"
    assert len(server.requests) == sent_before + 1


async def test_a_client_document_that_cannot_be_cached_still_starts_its_walls_and_is_said_once(
    monkeypatch, client, server, drm, television, caplog
):
    """A full disk costs the cache, not the walls: the supervisor gets the
    document all the same, and the failure is said once over several polls."""
    from arrt_player import pull as pull_module

    cache_files = {client.client_document_path, client.cache_dir / pull_module.CLIENT_ETAG_FILENAME}
    write = pull_module._write_atomically

    def disk_full_for_the_client_cache(path: Path, data: bytes) -> None:
        if path in cache_files:
            raise OSError(28, "No space left on device")
        write(path, data)

    monkeypatch.setattr(pull_module, "_write_atomically", disk_full_for_the_client_cache)
    server.publish("w1", wall_id="living-room")

    with caplog.at_level(logging.INFO, logger="arrt_player.pull"):
        async with Running(supervisor_for(client, drm)) as supervisor:
            await eventually(lambda: television.on_the_wall is not None, what="the Frame to show the wall")
            await asyncio.sleep(0.3)  # several polls
            assert supervisor.running == {"living-room": "frame"}

    events = [record.__dict__.get("event") for record in caplog.records]
    assert events.count("client.cache_unwritable") == 1, events
    assert "client.cache_writable" not in events, "the cache was said to recover while every write failed"
    assert not client.client_document_path.exists()


# -- a worker that fails ---------------------------------------------------------------------


async def test_a_crashed_worker_is_logged_and_started_again(client, server, drm, caplog):
    calls: list[str] = []

    async def fails_once(wall, output, stop):
        calls.append(wall.wall_id)
        if len(calls) == 1:
            raise RuntimeError("something nobody predicted")
        await stop.wait()

    supervisor = supervisor_for(client, drm, worker=fails_once, restart_min_seconds=0.01, restart_max_seconds=0.05)
    with caplog.at_level(logging.ERROR, logger="arrt_player.client"):
        async with Running(supervisor):
            await eventually(lambda: len(calls) >= 2, what="the worker to be started again")
            await asyncio.sleep(0.1)
            assert len(calls) == 2, "a worker running normally was restarted"

    crashes = [record for record in caplog.records if record.__dict__.get("event") == "client.worker_crashed"]
    assert len(crashes) == 1
    assert crashes[0].exc_info is not None, "the crash was logged without its traceback"


async def test_a_worker_that_ends_without_being_asked_is_started_again(client, server, drm, caplog):
    calls: list[str] = []

    async def returns_once(wall, output, stop):
        calls.append(wall.wall_id)
        if len(calls) > 1:
            await stop.wait()

    supervisor = supervisor_for(client, drm, worker=returns_once, restart_min_seconds=0.01)
    with caplog.at_level(logging.ERROR, logger="arrt_player.client"):
        async with Running(supervisor):
            await eventually(lambda: len(calls) >= 2, what="the worker to be started again")

    assert [record.__dict__.get("event") for record in caplog.records].count("client.worker_ended") == 1


async def test_a_stopped_wall_is_not_restarted(client, server, drm):
    calls: list[str] = []

    async def runs(wall, output, stop):
        calls.append(wall.wall_id)
        await stop.wait()

    async with Running(supervisor_for(client, drm, worker=runs, restart_min_seconds=0.01)) as supervisor:
        await eventually(lambda: calls == ["living-room"], what="the worker to start")
        server.unassign("living-room")
        await eventually(lambda: supervisor.running == {}, what="the worker to stop")
        await asyncio.sleep(0.1)

    assert calls == ["living-room"]


# -- outputs that come and go under one document ---------------------------------------------


class Answers:
    """The real client link, recording what each fetch answered: a document, or None for a 304."""

    def __init__(self, link: ClientPull) -> None:
        self._link = link
        self.answers: list[ClientDocument | None] = []

    def cached(self) -> ClientDocument | None:
        return self._link.cached()

    async def fetch(self) -> ClientDocument | None:
        answer = await self._link.fetch()
        self.answers.append(answer)
        return answer

    async def report(self, heartbeat) -> bool:
        return await self._link.report(heartbeat)

    async def close(self) -> None:
        await self._link.close()


async def test_a_connector_listed_after_the_document_gets_its_wall_while_the_server_answers_304(client, server, tmp_path):
    """**A card the kernel enumerates after the Player starts.** The document placed
    the wall before its output existed here, and the server has nothing new to say,
    so only the outputs changing can start it."""
    without_frame = replace(client, frame=None)
    server.unassign("living-room")
    server.assign("hall", "hdmi-a-1")
    drm = drm_tree(tmp_path / "late", {})
    link = Answers(ClientPull(without_frame))
    supervisor = supervisor_for(without_frame, drm, worker=_idle, link=link)
    try:
        await supervisor.cycle()
        assert supervisor.running == {}, "a wall started on an output this client did not list"

        drm_tree(drm, {"card1-HDMI-A-1": ("connected", "1920x1080\n")})
        for _ in range(3):
            await supervisor.cycle()

        assert link.answers[0] is not None
        assert link.answers[1:] == [None, None, None], "the server sent a new document, so this proves nothing"
        assert supervisor.running == {"hall": "hdmi-a-1"}
    finally:
        await supervisor.stop_all()
        await link.close()


async def test_a_connector_the_kernel_stops_listing_stops_its_wall_unretired_and_its_return_starts_it(
    client, server, drm, connectors
):
    """A connector gone from sysfs is a driver going away, not the curator moving the
    wall: the worker stops, the screen is not drawn black, and the wall is back on
    the connector once it is listed again, with the server sending nothing new."""
    server.unassign("living-room")
    server.assign("hall", "hdmi-a-1")
    server.publish("h1", wall_id="hall")
    hidden = drm.parent / "hidden-HDMI-A-1"

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: "hdmi-a-1" in connectors and connectors["hdmi-a-1"].shown, what="the wall to be drawn")
        drawn = len(connectors["hdmi-a-1"].shown)

        (drm / "card1-HDMI-A-1").rename(hidden)
        await eventually(lambda: supervisor.running == {}, what="the wall on the vanished connector to stop")
        hidden.rename(drm / "card1-HDMI-A-1")
        await eventually(lambda: supervisor.running == {"hall": "hdmi-a-1"}, what="the wall to start again")
        await eventually(lambda: len(connectors["hdmi-a-1"].shown) > drawn, what="the wall to draw again")

    assert connectors["hdmi-a-1"].blanked == [], "a connector the driver dropped was drawn black as if its wall had left"


# -- a screen its wall has left goes dark ----------------------------------------------------


@pytest.fixture
def connectors(monkeypatch) -> dict[str, RecordingOutput]:
    """Every screen output the entry point builds, by connector, one per connector as the card is one per process."""
    built: dict[str, RecordingOutput] = {}
    monkeypatch.setattr(entry, "screen_output", lambda wall, output: built.setdefault(output, RecordingOutput()))
    return built


def _on_hdmi_a_1(server: ServerDouble) -> None:
    server.unassign("living-room")
    server.assign("hall", "hdmi-a-1")
    server.publish("h1", "h2", wall_id="hall")


async def test_a_screen_wall_taken_away_draws_its_screen_black_once(client, server, drm, connectors):
    _on_hdmi_a_1(server)

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: "hdmi-a-1" in connectors and connectors["hdmi-a-1"].shown, what="the wall to be drawn")
        server.unassign("hall")
        await eventually(lambda: supervisor.running == {}, what="the worker to stop")
        await eventually(lambda: connectors["hdmi-a-1"].blanked, what="the screen to be drawn black")
        await asyncio.sleep(0.15)  # several more polls of the same document

    assert connectors["hdmi-a-1"].blanked == [len(connectors["hdmi-a-1"].shown)], "black not drawn once, after the art"


async def test_a_screen_wall_moved_to_another_connector_draws_the_old_one_black(client, server, drm, connectors):
    _on_hdmi_a_1(server)

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: "hdmi-a-1" in connectors and connectors["hdmi-a-1"].shown, what="the wall to be drawn")
        server.assign("hall", "hdmi-a-2")
        await eventually(lambda: supervisor.running == {"hall": "hdmi-a-2"}, what="the move")
        await eventually(lambda: "hdmi-a-2" in connectors and connectors["hdmi-a-2"].shown, what="the new screen to draw")

    assert len(connectors["hdmi-a-1"].blanked) == 1, "the connector the wall left kept its picture"
    assert connectors["hdmi-a-2"].blanked == [], "the client stopping drew the wall's new screen black"


async def test_a_connector_handed_to_another_wall_is_black_before_the_new_walls_first_picture(client, server, drm, connectors):
    _on_hdmi_a_1(server)
    server.publish("p1", wall_id="porch")

    async with Running(supervisor_for(client, drm)) as supervisor:
        await eventually(lambda: "hdmi-a-1" in connectors and connectors["hdmi-a-1"].shown, what="the wall to be drawn")
        by_hall = len(connectors["hdmi-a-1"].shown)
        server.unassign("hall")
        server.assign("porch", "hdmi-a-1")
        await eventually(lambda: supervisor.running == {"porch": "hdmi-a-1"}, what="the handover")
        await eventually(lambda: len(connectors["hdmi-a-1"].shown) > by_hall, what="the new wall to draw")

    output = connectors["hdmi-a-1"]
    assert output.blanked == [by_hall], "black was not drawn between the two walls' pictures"
    assert output.shown[by_hall].parent == client.cache_dir / "porch" / COMPOSED_DIRNAME


async def test_a_client_stopping_leaves_its_screens_as_they_are(client, server, drm, connectors):
    """The S5 decision: a Player merely stopping is no wall leaving its screen."""
    _on_hdmi_a_1(server)

    async with Running(supervisor_for(client, drm)):
        await eventually(lambda: "hdmi-a-1" in connectors and connectors["hdmi-a-1"].shown, what="the wall to be drawn")

    assert connectors["hdmi-a-1"].blanked == []


async def test_a_wall_taken_away_while_its_worker_waits_to_restart_still_draws_its_screen_black(client, server, drm, connectors):
    """**The gap a worker's own tidy-up cannot reach.** The worker drew, then crashed,
    and the wall is taken away during the wait before its restart: no worker is
    running to see the stop, so it is called once more, already stopped, to draw
    black and do nothing else."""
    _on_hdmi_a_1(server)
    stops_at_entry: list[bool] = []
    crashed: list[str] = []

    async def draws_then_crashes(wall, output, stop):
        stops_at_entry.append(stop.is_set())
        if len(stops_at_entry) > 1:
            await entry.run_wall(client, wall, output, stop)
            return
        ending = asyncio.Event()
        worker = asyncio.create_task(entry.run_wall(client, wall, output, ending))
        await eventually(lambda: "hdmi-a-1" in connectors and connectors["hdmi-a-1"].shown, what="the wall to be drawn")
        ending.set()
        await worker
        # Nothing between this and the supervisor's wait yields, so a test that
        # sees it sees the supervisor waiting.
        crashed.append(wall.wall_id)
        raise RuntimeError("the pull died on a full disk")

    # A restart wait far longer than the test, so the stop lands inside it.
    supervisor = supervisor_for(client, drm, worker=draws_then_crashes, restart_min_seconds=60, restart_max_seconds=60)
    async with Running(supervisor):
        await eventually(lambda: crashed, what="the worker to draw and crash")
        server.unassign("hall")
        await eventually(lambda: connectors["hdmi-a-1"].blanked, what="the screen to be drawn black")

    assert stops_at_entry == [False, True], "the worker was not called once more, already stopped"
    assert len(connectors["hdmi-a-1"].blanked) == 1


# -- the outputs ---------------------------------------------------------------------------


async def test_the_client_heartbeat_reports_the_frame_and_each_hdmi_connector(client, server, drm):
    async with Running(supervisor_for(client, drm)):
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")

    assert server.client_heartbeats[0]["outputs"] == [
        {"name": "frame", "kind": "frame", "connected": True, "screen": None},
        {"name": "hdmi-a-1", "kind": "framebuffer", "connected": True, "screen": [1920, 1080]},
        {"name": "hdmi-a-2", "kind": "framebuffer", "connected": False, "screen": None},
    ]


async def test_a_screen_plugged_in_is_reported_at_once_and_an_unchanged_one_is_not_repeated(client, server, drm):
    async with Running(supervisor_for(client, drm)):
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")
        await asyncio.sleep(0.2)
        assert len(server.client_heartbeats) == 1, "an unchanged report was sent on every poll"

        (drm / "card1-HDMI-A-2" / "status").write_text("connected\n")
        (drm / "card1-HDMI-A-2" / "modes").write_text("3840x2160\n")
        await eventually(lambda: len(server.client_heartbeats) == 2, what="the change to be reported")

    assert server.client_heartbeats[1]["outputs"][2] == {
        "name": "hdmi-a-2",
        "kind": "framebuffer",
        "connected": True,
        "screen": [3840, 2160],
    }


def _client_heartbeat_posts(server) -> int:
    return server.requests.count(("POST", "/client/heartbeat"))


@pytest.mark.parametrize("status", [400, 413])
async def test_a_client_heartbeat_the_server_refuses_is_sent_once_and_again_only_when_an_output_changes(
    client, server, drm, caplog, status
):
    """The same report gets the same answer: the server's log takes it once, and a screen plugged in is news."""
    server.client_heartbeat_status = status
    with caplog.at_level(logging.WARNING, logger="arrt_player.pull"):
        async with Running(supervisor_for(client, drm)):
            await eventually(lambda: _client_heartbeat_posts(server) == 1, what="the first report")
            await asyncio.sleep(0.3)
            assert _client_heartbeat_posts(server) == 1, "a refused, unchanged report was sent again"

            (drm / "card1-HDMI-A-2" / "status").write_text("connected\n")
            (drm / "card1-HDMI-A-2" / "modes").write_text("3840x2160\n")
            await eventually(lambda: _client_heartbeat_posts(server) == 2, what="the changed report")
            await asyncio.sleep(0.3)
            assert _client_heartbeat_posts(server) == 2, "a refused, changed report was sent again unchanged"

    refused = [record for record in caplog.records if record.__dict__.get("event") == "client.heartbeat_refused"]
    assert [record.status for record in refused] == [status], "the refusal was not said once"


async def test_a_client_heartbeat_whose_token_is_refused_is_sent_again_while_the_token_stays_refused(client, server, drm, caplog):
    """A 401 is the token, not the report: the client keeps reporting, and says so once."""
    server.tokens = {}
    with caplog.at_level(logging.WARNING, logger="arrt_player.pull"):
        async with Running(supervisor_for(client, drm)):
            await eventually(lambda: _client_heartbeat_posts(server) >= 3, what="the report to go on being sent")

    refused = [record for record in caplog.records if record.__dict__.get("event") == "client.heartbeat_refused"]
    assert [record.status for record in refused] == [401]


# -- where the settings page is ------------------------------------------------------------------


#: `UI_BIND` for a page on every IPv4 interface: a setting's value under test; nothing here binds it.
EVERY_INTERFACE = "0.0.0.0"  # noqa: S104 -- a value under test, never bound


def _page(cache_dir: Path, bind: str, *, allowed: str | None = None) -> UiSettings:
    environment = {"UI_ALLOWED_HOSTS": allowed} if allowed is not None else {}
    return UiSettings(cache_dir=cache_dir, bind=bind, port=8771, pinned=frozenset(), environment=environment)


@pytest.mark.parametrize(
    ("bind", "allowed", "local", "expected"),
    [
        (EVERY_INTERFACE, None, "192.0.2.10", "http://192.0.2.10:8771/"),
        (EVERY_INTERFACE, "Wall.LAN., other.example", "192.0.2.10", "http://wall.lan:8771/"),
        (EVERY_INTERFACE, " , wall.lan", None, "http://wall.lan:8771/"),
        (EVERY_INTERFACE, "2001:db8::7", "192.0.2.10", "http://[2001:db8::7]:8771/"),
        ("::", None, "2001:db8::5", "http://[2001:db8::5]:8771/"),
        ("::", None, "::ffff:192.0.2.10", "http://192.0.2.10:8771/"),
        ("192.0.2.20", None, "192.0.2.10", "http://192.0.2.20:8771/"),
        ("127.0.0.1", "wall.lan", "192.0.2.10", None),
        ("::1", None, "192.0.2.10", None),
        ("localhost", None, "192.0.2.10", None),
        (EVERY_INTERFACE, None, None, None),
        (EVERY_INTERFACE, None, "2001:db8::5", None),
        ("::", None, "fe80::1%eth0", None),
        ("::", None, "fe80::1", None),
        ("wall.lan", None, "192.0.2.10", None),
    ],
    ids=[
        "every-interface",
        "first-allowed-name",
        "first-non-empty-name",
        "allowed-ipv6-literal",
        "ipv6",
        "ipv4-mapped",
        "bound-address",
        "loopback-beats-a-name",
        "ipv6-loopback",
        "localhost",
        "no-connection-yet",
        "ipv6-to-an-ipv4-page",
        "link-local-scoped",
        "link-local",
        "bound-to-a-name",
    ],
)
def test_the_settings_address_is_the_operators_name_the_bound_address_or_the_pulls(cache_dir, bind, allowed, local, expected):
    address = settings_url(_page(cache_dir, bind, allowed=allowed), local)

    assert address == expected
    if address is not None:
        heartbeat = client_heartbeat([], reported_at=datetime.now(UTC), settings_url=address)
        assert [error.message for error in CLIENT_HEARTBEAT.iter_errors(heartbeat)] == []


def test_an_unknown_settings_address_leaves_the_key_out():
    """The schema allows it absent and never null."""
    assert "settings_url" not in client_heartbeat([], reported_at=datetime.now(UTC), settings_url=None)


@pytest.mark.parametrize(
    ("bind", "allowed", "expected"),
    [
        (EVERY_INTERFACE, None, "http://127.0.0.1:8771/"),
        (EVERY_INTERFACE, "wall.lan", "http://wall.lan:8771/"),
        ("127.0.0.1", None, None),
    ],
    ids=["the-pulls-address", "the-first-allowed-name", "loopback"],
)
async def test_the_server_hears_where_the_settings_page_is(client, server, drm, cache_dir, bind, allowed, expected):
    """Through the real link, against the double, which keeps only a report the schema calls valid."""
    elapsed = Elapsed()
    link = ClientPull(client, monotonic=elapsed)
    page = _page(cache_dir, bind, allowed=allowed)
    supervisor = supervisor_for(
        client, drm, link=link, worker=_idle, monotonic=elapsed, settings_url=lambda: settings_url(page, link.local_address)
    )

    for _ in range(3):
        await supervisor.cycle()
        elapsed.now += 10
    await supervisor.stop_all()
    await link.close()

    assert server.client_heartbeats, "no report was taken"
    assert server.client_heartbeats[-1].get("settings_url") == expected
    # The pull's own address is learned on its first request, so the first report
    # goes without it and the next, at once, with it; a name or loopback needs none.
    assert len(server.client_heartbeats) == (2 if allowed is None and expected is not None else 1)


async def test_a_new_local_address_alone_is_reported_at_once(client, server, drm, cache_dir):
    """The Player's address changes and nothing else: the server hears a new report, without waiting for the interval."""
    elapsed = Elapsed()
    address = ["192.0.2.10"]
    page = _page(cache_dir, EVERY_INTERFACE)
    supervisor = supervisor_for(
        client,
        drm,
        link=ClientPull(client, monotonic=elapsed),
        worker=_idle,
        monotonic=elapsed,
        settings_url=lambda: settings_url(page, address[0]),
    )

    await supervisor.cycle()
    elapsed.now += 10
    await supervisor.cycle()
    assert len(server.client_heartbeats) == 1, "an unchanged report was sent before its interval"
    address[0] = "192.0.2.77"
    elapsed.now += 10
    await supervisor.cycle()
    await supervisor.stop_all()

    assert [beat["settings_url"] for beat in server.client_heartbeats] == ["http://192.0.2.10:8771/", "http://192.0.2.77:8771/"]
    assert server.client_heartbeats[0]["outputs"] == server.client_heartbeats[1]["outputs"]


def test_a_machine_with_no_drm_directory_has_no_hdmi_outputs(tmp_path):
    assert hdmi_outputs(tmp_path / "absent") == []


def test_a_connector_whose_status_is_not_connected_is_not_claimed(tmp_path):
    drm = drm_tree(tmp_path / "drm", {"card0-HDMI-A-1": ("unknown", "1920x1080\n")})

    assert hdmi_outputs(drm) == [OutputReport(name="hdmi-a-1", kind="framebuffer", connected=False, screen=None)]


def test_one_connector_on_two_cards_is_one_output(tmp_path):
    """No two outputs in one report may share a name, so the server can place a wall by it."""
    drm = drm_tree(
        tmp_path / "drm",
        {"card0-HDMI-A-1": ("connected", "1920x1080\n"), "card1-HDMI-A-1": ("connected", "1280x720\n")},
    )

    assert hdmi_outputs(drm) == [OutputReport(name="hdmi-a-1", kind="framebuffer", connected=True, screen=(1920, 1080))]


def test_a_connected_screen_with_no_mode_listed_has_no_size(tmp_path):
    drm = drm_tree(tmp_path / "drm", {"card1-HDMI-A-1": ("connected", "")})

    assert hdmi_outputs(drm)[0].screen is None


# -- the process ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def settings_page(monkeypatch, cache_dir: Path) -> UiSettings:
    """Every `_run` here serves its settings page on loopback, on a port nothing else holds.

    Without it `_run` would resolve the page from the real environment, which has
    no `CACHE_DIR` in the suite, and refuse before installing its SIGTERM handler,
    so a test's own SIGTERM would end pytest.
    """
    page = UiSettings(cache_dir=cache_dir, bind="127.0.0.1", port=free_port(), pinned=frozenset())
    monkeypatch.setattr(entry, "load_ui", lambda: page)
    return page


async def test_sigterm_stops_every_worker_and_closes_the_art_channel(monkeypatch, client, server, drm, screens, settings_page):
    """The whole process as systemd runs it: `_run` until SIGTERM, then a clean exit.

    **The art channel is closed before the process returns, not merely asked
    to.** The set's close takes a moment here, so a supervisor that signalled its
    workers and returned without waiting would exit with the channel still open
    — which is what SIGKILL after a timeout would then find.
    """
    television = SlowToCloseTv()
    monkeypatch.setattr(entry, "SamsungTv", lambda **kwargs: television)
    server.assign("hall", "hdmi-a-1")
    server.publish("w1", wall_id="living-room")
    server.publish("h1", wall_id="hall")
    monkeypatch.setattr(entry, "load", lambda: client)
    port = settings_page.port
    monkeypatch.setattr(entry, "client_outputs", partial(client_outputs, drm_root=drm))

    running = asyncio.create_task(entry._run())
    await eventually(lambda: television.on_the_wall is not None, what="the Frame to show its wall")
    await eventually(lambda: "hall" in screens and screens["hall"].shown, what="the screen to draw its wall")
    assert await page_answers(port), "the settings page is not listening beside the walls"

    os.kill(os.getpid(), signal.SIGTERM)

    assert await asyncio.wait_for(running, timeout=5) == 0
    assert television.closed >= 1, "the art channel was left open at the set"
    assert not listening(port), "the settings page outlived the process's own stop"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


async def page_answers(port: int) -> bool:
    """Whether the page answers a request, asked until it does or a few seconds pass."""
    async with aiohttp.ClientSession() as session:
        for _ in range(100):
            try:
                async with session.get(f"http://127.0.0.1:{port}/", allow_redirects=False) as response:
                    return response.status in {200, 303}
            except aiohttp.ClientConnectionError:
                await asyncio.sleep(0.05)
    return False


# -- the Frame's identity ----------------------------------------------------------------------


def identified(client, tv: FakeTv) -> FrameIdentity:
    return FrameIdentity(tv.read_identity, budget_seconds=1.0)


def supervisor_with_identity(client, drm: Path, identity: FrameIdentity, **overrides) -> Supervisor:
    return supervisor_for(
        client,
        drm,
        outputs=lambda: client_outputs(client, drm_root=drm, frame_identity=identity.value),
        frame_identity=identity,
        **overrides,
    )


async def test_a_frame_with_no_wall_on_it_still_reports_its_identity(client, server, drm, tv):
    """**The case the identity exists for.** A Frame moved to a new client has no
    wall there yet; it must say who it is for its walls to follow it. Read through
    the television double, with no worker running and no art channel opened."""
    server.unassign("living-room")
    identity = identified(client, tv)

    async with Running(supervisor_with_identity(client, drm, identity)) as supervisor:
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")
        assert supervisor.running == {}, "a worker ran, so this is not the no-wall case"

    frame = server.client_heartbeats[0]["outputs"][0]
    assert frame == {"name": "frame", "kind": "frame", "connected": True, "screen": None, "identity": tv.device_id}
    assert tv.connects == 0, "the identity read opened the art channel"


async def test_hdmi_outputs_report_no_identity(client, server, drm, tv):
    identity = identified(client, tv)

    async with Running(supervisor_with_identity(client, drm, identity, worker=_idle)):
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")

    hdmi = [output for output in server.client_heartbeats[0]["outputs"] if output["kind"] == "framebuffer"]
    assert hdmi, "no HDMI output was reported, so this proves nothing"
    assert all("identity" not in output for output in hdmi)


async def test_a_frame_whose_id_cannot_be_read_is_reported_without_one_and_said_once(client, server, drm, tv, caplog):
    tv.unavailable = True
    identity = identified(client, tv)
    supervisor = supervisor_with_identity(client, drm, identity, worker=_idle)

    with caplog.at_level(logging.WARNING, logger="arrt_player.client"):
        for _ in range(3):
            await supervisor.cycle()
    await supervisor.stop_all()

    assert tv.identity_reads == 3, "the read was not tried again on each report"
    assert "identity" not in server.client_heartbeats[0]["outputs"][0]
    unreadable = [record for record in caplog.records if record.__dict__.get("event") == "client.identity_unreadable"]
    assert len(unreadable) == 1, "an unreadable identity was said on every report"


class Elapsed:
    """Elapsed time that moves only when the test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def supervisor_on(client, drm: Path, identity: FrameIdentity, elapsed: Elapsed, **overrides) -> Supervisor:
    """A supervisor reporting the Frame's identity, on a clock only the test moves."""
    return Supervisor(
        settings=client,
        link=overrides.pop("link", None) or ClientPull(client, monotonic=elapsed),
        worker=_idle,
        outputs=lambda: client_outputs(client, drm_root=drm, frame_identity=identity.value),
        now=lambda: datetime.now(UTC),
        monotonic=elapsed,
        frame_identity=identity,
        **overrides,
    )


def identified_on(tv: FakeTv, elapsed: Elapsed) -> FrameIdentity:
    return FrameIdentity(tv.read_identity, budget_seconds=1.0, monotonic=elapsed)


def _identities(server) -> list[str | None]:
    return [beat["outputs"][0].get("identity") for beat in server.client_heartbeats]


async def test_an_identity_read_late_is_reported_at_once_and_kept_through_a_failed_read(client, server, drm, tv):
    """A set asleep at boot is identified once it answers; once read, the
    identity is kept through a later failed read, so it does not flap away
    while the set sleeps."""
    elapsed = Elapsed()
    tv.unavailable = True
    identity = identified_on(tv, elapsed)
    supervisor = supervisor_on(client, drm, identity, elapsed)

    await supervisor.cycle()
    tv.unavailable = False
    elapsed.now += 30
    await supervisor.cycle()
    tv.unavailable = True
    elapsed.now += 60
    await supervisor.cycle()
    await supervisor.stop_all()

    assert _identities(server) == [None, tv.device_id, tv.device_id], "a failed read cost the report its identity"
    assert tv.identity_reads == 3, "the identity was not asked again once its interval came round"


async def test_another_set_at_the_frames_address_is_reported_within_one_interval_and_said_once(client, server, drm, tv, caplog):
    """One REST read a minute: a set swapped at the Frame's address, without a restart, is the next report."""
    elapsed = Elapsed()
    identity = identified_on(tv, elapsed)
    supervisor = supervisor_on(client, drm, identity, elapsed)
    first = tv.device_id

    with caplog.at_level(logging.INFO, logger="arrt_player.client"):
        await supervisor.cycle()
        tv.device_id = "uuid:the-set-that-replaced-it"
        elapsed.now += 30
        await supervisor.cycle()
        assert _identities(server) == [first], "the identity was read again before its interval"
        elapsed.now += 30
        await supervisor.cycle()
        elapsed.now += 60
        await supervisor.cycle()
    await supervisor.stop_all()

    assert _identities(server) == [first, tv.device_id, tv.device_id]
    assert tv.identity_reads == 3, "the identity was read other than once a minute"
    changed = [record for record in caplog.records if record.__dict__.get("event") == "client.identity_changed"]
    assert len(changed) == 1, "the new set was not said, or said on every read"
    line = logs.JsonFormatter().format(changed[0])
    for device_id in (first, tv.device_id):
        assert device_id not in line, "the journal named a set by its own id, where every other line names it by a hash"
        assert f"set-{hashlib.sha256(device_id.encode()).hexdigest()[:8]}" in line, "the line does not say which set"
    said = json.loads(line)
    assert said["set_now"] == f"set-{hashlib.sha256(tv.device_id.encode()).hexdigest()[:8]}"
    assert said["set_was"] == f"set-{hashlib.sha256(first.encode()).hexdigest()[:8]}"


async def test_an_identity_read_that_hangs_costs_the_report_its_identity_and_nothing_else(client, server, drm):
    async def hangs() -> str:
        await asyncio.Event().wait()
        return "never"

    identity = FrameIdentity(hangs, budget_seconds=0.05)
    supervisor = supervisor_with_identity(client, drm, identity, worker=_idle)

    await asyncio.wait_for(supervisor.cycle(), timeout=5)
    await supervisor.stop_all()

    assert server.client_heartbeats, "a hung read held the report back"
    assert "identity" not in server.client_heartbeats[0]["outputs"][0]


async def test_the_entry_point_reads_the_identity_from_the_frames_rest_description(monkeypatch, client, server, drm, tv):
    """The composition root's wiring: a `FrameIdentity` over the Frame's own client."""
    monkeypatch.setattr(entry, "SamsungTv", lambda **kwargs: tv)
    monkeypatch.setattr(entry, "load", lambda: client)
    monkeypatch.setattr(entry, "client_outputs", partial(_outputs_beside, drm))
    server.unassign("living-room")

    running = asyncio.create_task(entry._run())
    try:
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(running, timeout=5)

    assert server.client_heartbeats[0]["outputs"][0]["identity"] == tv.device_id


async def test_the_entry_point_reports_the_settings_page_at_the_address_its_pull_reached_the_server_from(
    monkeypatch, client, server, drm, tv, cache_dir
):
    """The composition root's wiring: the page's settings and the client link's own address, into the report."""
    monkeypatch.setattr(entry, "SamsungTv", lambda **kwargs: tv)
    monkeypatch.setattr(entry, "load", lambda: client)
    monkeypatch.setattr(entry, "client_outputs", partial(_outputs_beside, drm))
    monkeypatch.setattr(entry, "load_ui", lambda: _page(cache_dir, EVERY_INTERFACE))

    async def no_page(state, stop) -> bool:
        # The page on every interface is not this test's to open.
        await stop.wait()
        return True

    monkeypatch.setattr(entry, "run_settings_page", no_page)

    running = asyncio.create_task(entry._run())
    try:
        await eventually(lambda: any("settings_url" in beat for beat in server.client_heartbeats), what="the settings address")
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(running, timeout=5)

    assert next(beat["settings_url"] for beat in server.client_heartbeats if "settings_url" in beat) == "http://127.0.0.1:8771/"


def _outputs_beside(drm: Path, settings, *, frame_identity=None):
    return client_outputs(settings, drm_root=drm, frame_identity=frame_identity)


async def _idle(wall, output, stop):
    await stop.wait()


# -- label outputs -----------------------------------------------------------------------------

LABEL_DOCUMENT = {
    "schema": {"major": 1, "minor": 0},
    "wall_id": "living-room",
    "wall_name": "Living room",
    "display_state": {"state": "showing_art", "work_id": "w1", "since": "2026-10-08T16:00:00Z"},
    "label": {"title": "Cat Litter"},
}


class RecordingLabels:
    """A label worker that records what it was started for, and how it was stopped."""

    def __init__(self) -> None:
        self.started: list[LabelAssignment] = []
        self.stopped: list[tuple[LabelAssignment, bool]] = []

    async def __call__(self, assignment, stop) -> None:
        self.started.append(assignment)
        await stop.wait()
        self.stopped.append((assignment, stop.retired))


def with_a_panel(client, drm, labels, **overrides) -> Supervisor:
    report = LabelOutputReport(name="epd-0", kind="epaper", connected=True, size=(1448, 1072))
    return supervisor_for(client, drm, worker=_idle, label_worker=labels, label_outputs=lambda: [report], **overrides)


async def test_a_mapped_label_output_starts_a_renderer_and_an_unmapped_one_retires_it(client, server, drm):
    labels = RecordingLabels()
    server.map_label("label-1", "epd-0", "a-wall-on-another-client", LABEL_DOCUMENT)

    async with Running(with_a_panel(client, drm, labels)) as supervisor:
        await eventually(lambda: labels.started, what="the renderer to start")
        assert supervisor.labels == {"epd-0": LabelAssignment("label-1", "epd-0", "a-wall-on-another-client")}

        server.unmap_label("label-1")
        await eventually(lambda: labels.stopped, what="the renderer to stop")
        assert supervisor.labels == {}

    assert labels.stopped == [(labels.started[0], True)], "a label that captions no wall was not retired"


async def test_a_label_moved_to_another_wall_is_restarted_not_retired(client, server, drm):
    labels = RecordingLabels()
    server.map_label("label-1", "epd-0", "living-room", LABEL_DOCUMENT)

    async with Running(with_a_panel(client, drm, labels)):
        await eventually(lambda: labels.started, what="the renderer to start")
        server.map_label("label-2", "epd-0", "study", {**LABEL_DOCUMENT, "wall_id": "study"})
        await eventually(lambda: len(labels.started) == 2, what="the renderer to start again")

    assert labels.stopped[0] == (LabelAssignment("label-1", "epd-0", "living-room"), False)
    assert labels.started[1] == LabelAssignment("label-2", "epd-0", "study")


async def test_a_client_going_down_stops_its_renderers_without_retiring_them(client, server, drm):
    """The panel keeps what it last showed through a restart; blanking it would flash every restart."""
    labels = RecordingLabels()
    server.map_label("label-1", "epd-0", "living-room", LABEL_DOCUMENT)

    async with Running(with_a_panel(client, drm, labels)):
        await eventually(lambda: labels.started, what="the renderer to start")

    assert labels.stopped == [(labels.started[0], False)]


async def test_a_label_on_an_output_this_client_lacks_is_reported_once_and_not_started(client, server, drm, caplog):
    labels = RecordingLabels()
    server.map_label("label-1", "epd-7", "living-room", LABEL_DOCUMENT)

    with caplog.at_level(logging.ERROR, logger="arrt_player.client"):
        async with Running(with_a_panel(client, drm, labels)):
            await eventually(lambda: len(server.client_heartbeats) >= 1, what="a poll")
            await asyncio.sleep(0.2)

    assert labels.started == []
    unplaceable = [record for record in caplog.records if record.__dict__.get("event") == "client.label_unplaceable"]
    assert len(unplaceable) == 1


async def test_two_labels_on_one_output_start_one_renderer(client, server, drm, caplog):
    labels = RecordingLabels()
    server.map_label("label-1", "epd-0", "living-room", LABEL_DOCUMENT)
    server.clients[CLIENT_ID]["labels"].append({"label_id": "label-2", "output": "epd-0", "wall_id": "study"})

    with caplog.at_level(logging.ERROR, logger="arrt_player.client"):
        async with Running(with_a_panel(client, drm, labels)):
            await eventually(lambda: labels.started, what="the renderer to start")
            await asyncio.sleep(0.2)

    assert [assignment.label_id for assignment in labels.started] == ["label-1"]
    assert [record.__dict__.get("event") for record in caplog.records].count("client.label_unplaceable") == 1


async def test_a_crashed_renderer_is_logged_and_started_again(client, server, drm, caplog):
    calls: list[str] = []

    async def fails_once(assignment, stop):
        calls.append(assignment.label_id)
        if len(calls) == 1:
            raise RuntimeError("something nobody predicted")
        await stop.wait()

    server.map_label("label-1", "epd-0", "living-room", LABEL_DOCUMENT)
    supervisor = with_a_panel(client, drm, fails_once, restart_min_seconds=0.01, restart_max_seconds=0.05)
    with caplog.at_level(logging.ERROR, logger="arrt_player.client"):
        async with Running(supervisor):
            await eventually(lambda: len(calls) >= 2, what="the renderer to be started again")

    crashes = [record for record in caplog.records if record.__dict__.get("event") == "client.label_crashed"]
    assert len(crashes) == 1
    assert crashes[0].__dict__.get("label_id") == "label-1"


async def test_a_label_unmapped_while_its_renderer_waits_to_restart_is_called_once_more_retired(client, server, drm):
    """The walls' gap, for a label: no renderer is running to see the mapping go, so
    it is called once more, already stopped and retired, to leave its panel blank."""
    calls: list[tuple[bool, bool]] = []
    crashed: list[str] = []

    async def crashes_first(assignment, stop):
        calls.append((stop.is_set(), stop.retired))
        if len(calls) == 1:
            crashed.append(assignment.label_id)
            raise RuntimeError("the panel's driver raised something nobody predicted")

    server.map_label("label-1", "epd-0", "living-room", LABEL_DOCUMENT)
    supervisor = with_a_panel(client, drm, crashes_first, restart_min_seconds=60, restart_max_seconds=60)
    async with Running(supervisor):
        await eventually(lambda: crashed, what="the renderer to crash")
        server.unmap_label("label-1")
        await eventually(lambda: len(calls) == 2, what="the renderer to be called once more")

    assert calls == [(False, False), (True, True)]


async def test_a_label_unmapped_while_its_renderer_waits_to_restart_is_left_blank_without_saying_it_started(
    client, server, drm, caplog
):
    """The once-more call through the real worker: the panel is left blank, and
    the journal says the renderer left it so, not that one started."""
    from fakes import FakeSurface

    surface = FakeSurface()
    panels = {"epd-0": LabelPanel(name="epd-0", surface=surface, error=None, size=(1448, 1072))}
    calls: list[bool] = []

    async def crashes_first(assignment, stop):
        calls.append(stop.is_set())
        if len(calls) == 1:
            raise RuntimeError("the panel's driver raised something nobody predicted")
        await entry.run_label(client, panels, assignment, stop)

    server.map_label("label-1", "epd-0", "living-room", LABEL_DOCUMENT)
    supervisor = with_a_panel(client, drm, crashes_first, restart_min_seconds=60, restart_max_seconds=60)
    try:
        with caplog.at_level(logging.INFO):
            async with Running(supervisor):
                await eventually(lambda: calls, what="the renderer to crash")
                server.unmap_label("label-1")
                await eventually(lambda: len(calls) == 2 and surface.shown, what="the once-more call to blank the panel")
    finally:
        surface.release.set()

    assert calls == [False, True]
    assert surface.last_text == []
    events = [record.__dict__.get("event") for record in caplog.records]
    assert "label.renderer_started" not in events, "a retired renderer's last call said it started"
    assert events.count("label.renderer_retired") == 1


async def test_a_new_renderer_waits_for_the_one_still_stopping_on_its_panel(client, server, drm):
    """Two renderers drawing on one panel at once is what the wait prevents."""
    order: list[str] = []

    async def slow_to_stop(assignment, stop):
        order.append(f"start {assignment.label_id}")
        await stop.wait()
        await asyncio.sleep(0.2)
        order.append(f"end {assignment.label_id}")

    server.map_label("label-1", "epd-0", "living-room", LABEL_DOCUMENT)
    async with Running(with_a_panel(client, drm, slow_to_stop)):
        await eventually(lambda: order == ["start label-1"], what="the first renderer to start")
        server.map_label("label-2", "epd-0", "study", {**LABEL_DOCUMENT, "wall_id": "study"})
        await eventually(lambda: "start label-2" in order, what="the second renderer to start")

    assert order[:3] == ["start label-1", "end label-1", "start label-2"], "a renderer started on a panel still being drawn"


async def test_a_crashed_renderer_is_started_again_after_the_ladder_s_first_wait_and_not_before(client, server, drm):
    started: list[float] = []

    async def fails_once(assignment, stop):
        started.append(asyncio.get_running_loop().time())
        if len(started) == 1:
            raise RuntimeError("something nobody predicted")
        await stop.wait()

    server.map_label("label-1", "epd-0", "living-room", LABEL_DOCUMENT)
    supervisor = with_a_panel(client, drm, fails_once, restart_min_seconds=0.4, restart_max_seconds=5)
    async with Running(supervisor):
        await eventually(lambda: len(started) == 2, what="the renderer to be started again")

    assert 0.4 <= started[1] - started[0] < 1.5, "the restart did not wait the ladder's first step"


async def test_the_client_heartbeat_reports_the_panel_and_a_client_without_one_reports_no_label_outputs(client, server, drm):
    async with Running(with_a_panel(client, drm, RecordingLabels())):
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")
    with_panel = server.client_heartbeats[-1]

    server.client_heartbeats.clear()
    async with Running(supervisor_for(client, drm, worker=_idle)):
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")
    without = server.client_heartbeats[-1]

    assert with_panel["label_outputs"] == [{"name": "epd-0", "kind": "epaper", "connected": True, "size": [1448, 1072]}]
    assert "label_outputs" not in without


async def test_a_panel_whose_health_changes_is_reported_at_once(client, server, drm):
    connected = {"now": True}

    def label_outputs():
        return [LabelOutputReport(name="epd-0", kind="epaper", connected=connected["now"], size=(1448, 1072))]

    supervisor = supervisor_for(client, drm, worker=_idle, label_worker=RecordingLabels(), label_outputs=label_outputs)
    async with Running(supervisor):
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")
        connected["now"] = False
        await eventually(lambda: len(server.client_heartbeats) == 2, what="the panel's failure to be reported")

    assert server.client_heartbeats[1]["label_outputs"][0]["connected"] is False


async def test_the_entry_point_draws_a_mapped_label_on_its_panel(monkeypatch, client, server, drm, tv):
    """**Through the composition root**: the panel opened once, a renderer per
    mapping polling the label route, and the panel closed on the way out."""
    from fakes import FakeSurface

    surface = FakeSurface()
    monkeypatch.setattr(entry, "SamsungTv", lambda **kwargs: tv)
    monkeypatch.setattr(entry, "label_surface", lambda _settings: surface)
    monkeypatch.setattr(entry, "client_outputs", partial(_outputs_beside, drm))
    with_panel = replace(client, frame=None, panel=replace(client.panel, epd_device="omni_epd.mock"), label_poll_seconds=0.02)
    monkeypatch.setattr(entry, "load", lambda: with_panel)
    server.unassign("living-room")
    server.map_label("label-1", "epd-0", "a-wall-on-another-client", LABEL_DOCUMENT)

    running = asyncio.create_task(entry._run())
    try:
        await eventually(lambda: surface.shown, what="the label to be drawn")
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(running, timeout=5)
        surface.release.set()

    assert surface.last_text == ["Cat Litter"]
    assert all(output["kind"] != "frame" for output in server.client_heartbeats[0]["outputs"]), "a panel needs no Frame"
    assert server.client_heartbeats[0]["label_outputs"][0]["name"] == "epd-0"
    assert surface.closed == 1, "the panel was not powered down on the way out"


# -- every line a worker logs names its wall --------------------------------------------------

SOURCE = Path(entry.__file__).parent
RECORD = SOURCE.parents[1] / ".prawduct" / "artifacts" / "observability-strategy.md"

#: Emitted inside a wall's worker: its pull, its loop, its display and whatever
#: they start, its tasks and its threads. Each carries the wall's `wall_id`,
#: bound by the supervisor around everything the worker runs.
WALL_EVENTS = frozenset(
    {
        "client.worker_crashed",
        "client.worker_ended",
        "client.worker_retire_failed",
        "pull.started",
        "pull.unreachable",
        "pull.reachable",
        "pull.refused",
        "pull.accepted",
        "pull.manifest_refused",
        "pull.manifest_readable",
        "pull.media_failing",
        "pull.media_ok",
        "pull.hold_ended",
        "pull.work_skipped",
        "pull.adopted",
        "pull.cache_unwritable",
        "pull.cache_writable",
        "pull.heartbeat_refused",
        "pull.heartbeat_ok",
        "pull.heartbeat_failed",
        "pull.crashed",
        "frame.started",
        "frame.stopped",
        "frame.crashed",
        "daemon.state_too_new",
        "screen.started",
        "screen.stopped",
        "screen.crashed",
        "display.state",
        "heartbeat.failed",
        "heartbeat.recovered",
        "manifest.adopted",
        "manifest.absent",
        "manifest.statable",
        "manifest.unstatable",
        "manifest.unreadable",
        "manifest.not_text",
        "manifest.refused",
        "manifest.version_refused",
        "schedule.media_missing",
        "schedule.uncomposable",
        "schedule.compose_read_failed",
        "schedule.compose_write_failed",
        "schedule.compose_failed",
        "schedule.composed_damaged",
        "schedule.composed_removed",
        "schedule.tidy_failed",
        "schedule.tidy_recovered",
        "compose.done",
        "tv.unavailable",
        "tv.recovered",
        "store.recovered",
        "store.busy",
        "store.failed",
        "rotation.wall_not_ours",
        "rotation.wall_returned",
        "tv.writes_held",
        "tv.writes_resumed",
        "display.power_unreadable",
        "display.power_readable",
        "rotation.selected",
        "rotation.wall_unchanged",
        "rotation.wall_recovered",
        "binding.uploaded",
        "binding.upload_failed",
        "binding.orphaned",
        # The Frame's retirement, in the same pass as its orphan sweep,
        # and the store's feed count, read by the store the wall's worker opens.
        "binding.retired",
        "binding.retirement_unconfirmed",
        "store.feed_time_unreadable",
        # The Frame's reading of which set it reaches, and what the store found
        # the set to be, both on the wall's own task before it uses a binding.
        "tv.unidentified",
        "tv.identified",
        "tv.unidentified_going_on",
        "binding.set_adopted",
        "binding.set_changed",
        "tv.orphans_removed",
        "tv.orphan_removal_unconfirmed",
        "tv.removal_incomplete",
        "tv.others_left_alone",
        "tv.upload_misreported",
        "tv.selection_unreadable",
        "tv.selection_observer_failed",
        "tv.native_slideshow_disabled",
        "tv.token_file_open",
        "tv.close_failed",
        "brightness.set",
        "screen.absent",
        "screen.returned",
        "screen.draw_failed",
        "screen.draw_recovered",
        "screen.blanked",
        "screen.blank_failed",
        "screen.refresh_failed",
        "screen.refresh_recovered",
        "screen.brightness_set",
        "screen.brightness_unsupported",
        "screen.brightness_no_answer",
        "screen.brightness_answering",
        "screen.bus_close_failed",
        "screen.brightness_failed",
        "screen.brightness_recovered",
    }
)

#: Emitted inside a label's renderer: its pull, its panel's drawing and its
#: thread. Each carries the label's `label_id` and the `wall_id` it captions.
LABEL_EVENTS = frozenset(
    {
        "client.label_crashed",
        "client.label_ended",
        "client.label_retire_failed",
        "label.no_panel",
        "label.renderer_started",
        "label.renderer_retired",
        "label.server_unreachable",
        "label.server_reachable",
        "label.id_unusable",
        "label.cache_unreadable",
        "label.cache_unwritable",
        "label.cache_writable",
        "label.cache_seeded",
        "label.cache_other_wall",
        "label.refused",
        "label.accepted",
        "label.document_refused",
        "label.document_readable",
        "label.drawn",
        "label.blanked",
        "label.card",
        "label.absent",
        "label.failed",
        "label.recovered",
        "label.unusable",
        "label.shrunk",
        "label.name_wrapped",
        "label.truncated",
    }
)

#: Emitted by the supervisor about one wall or label, outside any worker. No
#: context names the wall there, so each call site passes `wall_id` itself.
NAMES_ITS_WALL = frozenset(
    {
        "client.wall_started",
        "client.wall_stopped",
        "client.wall_unplaceable",
        "client.label_started",
        "client.label_stopped",
        "client.label_unplaceable",
        "client.wall_cache_stamped",
        "client.wall_cache_returned",
        "client.wall_cache_removed",
    }
)

#: About the process, or the client as a whole, or slideshow mode (one
#: programme and no walls): no wall to name, and none named.
PROCESS_EVENTS = frozenset(
    {
        "daemon.misconfigured",
        "daemon.setup_mode",
        # Said once at start, before the supervisor runs any wall.
        "files.drawing",
        "client.started",
        "client.stopped",
        "client.identity_unreadable",
        "client.identity_read",
        "client.identity_changed",
        "client.unreachable",
        "client.reachable",
        "client.refused",
        "client.accepted",
        "client.document_refused",
        "client.document_readable",
        "client.cache_unwritable",
        "client.cache_writable",
        "client.cache_unreadable",
        "client.heartbeat_refused",
        "client.heartbeat_ok",
        "client.cache_entry_unknown",
        "client.wall_cache_unsweepable",
        "client.wall_cache_sweepable",
        "panel.type_scale",
        "panel.unavailable",
        "panel.size_disagrees",
        "panel.close_failed",
        "label.close_abandoned",
        "label.cache_forgotten",
        "label.cache_unsweepable",
        "label.cache_sweepable",
        "settings_page.listening",
        "settings_page.unavailable",
        "settings_page.password_set",
        "settings_page.password_changed",
        "settings_page.login_refused",
        "settings_page.password_refused",
        "settings_page.host_refused",
        "settings_page.saved",
        "settings_page.write_failed",
        # The page forgetting the Frame's pairing when its address changes.
        "settings_page.pairing_forgotten",
        "settings_page.pairing_not_forgotten",
        # systemd's notification socket, from _run, the supervisor's
        # own cycle (PettingSupervisor) and the slideshow's wait, never a worker.
        "notify.absent",
        "notify.unusable",
        "notify.unsent",
        "notify.sent",
        "slideshow.starting",
        "slideshow.renderer_unavailable",
        "slideshow.saved_listing",
        "slideshow.saved_unreadable",
        "slideshow.listed",
        "slideshow.listed_nothing",
        "slideshow.refresh_failed",
        "slideshow.save_failed",
        "slideshow.filled",
        "slideshow.nothing_ready",
        "slideshow.shown",
        "slideshow.not_shown",
        "slideshow.reddit.listed",
        "slideshow.reddit.not_listed",
        "slideshow.reddit.post_unreadable",
        "slideshow.resolver.fallback",
        "slideshow.resolver.redgifs.token",
        "slideshow.resolver.redgifs.token_refused",
        "slideshow.resolver.redgifs.gone",
        "slideshow.resolver.redgifs.lookup_failed",
        "slideshow.cache.downloaded",
        "slideshow.cache.fell_back",
        "slideshow.cache.download_failed",
        "slideshow.cache.cap_reached",
        "slideshow.cache.network_down",
        "slideshow.cache.evicted",
        "slideshow.cache.partial_cleared",
        "mpv.started",
        "mpv.stale",
        "mpv.refused",
        "mpv.file_failed",
        "mpv.not_shown",
        "mpv.recovered",
    }
)

_SCOPES = {"wall": WALL_EVENTS, "label": LABEL_EVENTS, "names its wall": NAMES_ITS_WALL, "process": PROCESS_EVENTS}
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
_EVENT_NAME = re.compile(r"[a-z_]+(\.[a-z_]+)+")


def _log_calls() -> list[tuple[Path, str, ast.Call]]:
    """Every `log.<level>(…)` call in the source, with its module and enclosing function."""
    # A call inside a nested function is found from both functions; the innermost, walked last, names it.
    unique: dict[int, tuple[Path, str, ast.Call]] = {}
    for module in sorted(SOURCE.rglob("*.py")):
        for function in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _LOG_METHODS
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "log"
                ):
                    unique[id(node)] = (module, function.name, node)
    return list(unique.values())


def _extra(call: ast.Call) -> ast.Dict | None:
    return next((k.value for k in call.keywords if k.arg == "extra" and isinstance(k.value, ast.Dict)), None)


def _event_of(call: ast.Call) -> ast.expr | None:
    extra = _extra(call)
    if extra is None:
        return None
    return next(
        (v for k, v in zip(extra.keys, extra.values, strict=True) if isinstance(k, ast.Constant) and k.value == "event"), None
    )


def _built_names() -> dict[tuple[str, str], set[str]]:
    """The events whose names are built rather than written, by (module, function), derived where the code allows."""
    from arrt_player.displays.frame import FrameDisplay
    from arrt_player.displays.screen import ScreenDisplay

    schedule = ast.parse((SOURCE / "programmes" / "schedule.py").read_text(encoding="utf-8"))
    collect = next(n for n in ast.walk(schedule) if isinstance(n, ast.FunctionDef) and n.name == "_collect")
    collected = {
        n.value
        for n in ast.walk(collect)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("schedule.")
    }
    supervised = {f"client.{kind}_{end}" for kind in ("worker", "label") for end in ("crashed", "ended", "retire_failed")}
    walls = {
        f"{display.journal_name}.{end}" for display in (FrameDisplay, ScreenDisplay) for end in ("started", "stopped", "crashed")
    }
    return {
        ("client.py", "_keep_running"): supervised,
        ("client.py", "_restart_until_stopped"): supervised,
        ("wall.py", "run"): walls,
        ("programmes/schedule.py", "_collect"): collected,
    }


def code_events() -> set[str]:
    """Every event name the source logs: each literal, and each built name by the site that builds it."""
    built = _built_names()
    names: set[str] = set()
    for module, function, call in _log_calls():
        event = _event_of(call)
        if isinstance(event, ast.Constant):
            names.add(event.value)
            continue
        site = (str(module.relative_to(SOURCE)), function)
        assert site in built, f"{site[0]}:{call.lineno} in {function} builds its event name; list the names it builds here"
        names |= built[site]
    return names


def record_events() -> set[str]:
    """Every event name the observability record's inventory lists (`…name` is the name before it, last part replaced)."""
    text = RECORD.read_text(encoding="utf-8")
    inventory = text[text.index("**The event inventory.**") : text.index("**Destinations and retention.**")]
    names: set[str] = set()
    for line in inventory.splitlines():
        if not line.startswith("| ") or line.startswith(("| **", "|---")):
            continue
        previous = None
        for written in re.findall(r"`([^`]+)`", line.split("|")[1]):
            name = previous.rsplit(".", 1)[0] + "." + written[1:] if written.startswith("…") and previous else written
            if _EVENT_NAME.fullmatch(name):
                names.add(name)
                previous = name
    return names


INVENTORY = sorted(code_events() | record_events())


def test_the_inventory_reaches_the_source_and_the_record():
    """Both walks pass vacuously over nothing, so each is seen reaching what it reads."""
    code, record = code_events(), record_events()
    assert {"pull.unreachable", "compose.done", "screen.brightness_set", "client.worker_crashed"} <= code
    assert {"pull.unreachable", "label.drawn", "slideshow.resolver.redgifs.token_refused"} <= record


@pytest.mark.parametrize("event", INVENTORY)
def test_every_event_is_declared_as_a_walls_a_labels_or_the_processs(event):
    """**An event added without saying whose it is fails here, by name.** A wall's
    or a label's carries its wall by the context its worker runs in; one the
    supervisor logs about a wall passes it itself; a process-level one has none."""
    scopes = [scope for scope, events in _SCOPES.items() if event in events]

    assert len(scopes) == 1, f"{event} is declared in {scopes or 'no list'}: declare it in exactly one of {list(_SCOPES)}"


def test_every_declared_event_is_one_the_code_or_the_record_names():
    """So the lists cannot keep a name nothing logs any more."""
    named = code_events() | record_events()

    assert sorted(set().union(*_SCOPES.values()) - named) == []


def test_every_line_the_supervisor_logs_about_a_wall_passes_the_wall_itself():
    """No worker's context surrounds these, so the call site is the only way the wall gets on the line."""
    sites = [
        (module, call)
        for module, _f, call in _log_calls()
        if isinstance(e := _event_of(call), ast.Constant) and e.value in NAMES_ITS_WALL
    ]
    missing = [
        f"{module.relative_to(SOURCE)}:{call.lineno} ({_event_of(call).value})"
        for module, call in sites
        if not any(isinstance(key, ast.Constant) and key.value == "wall_id" for key in _extra(call).keys)
    ]

    assert {_event_of(call).value for _m, call in sites} == NAMES_ITS_WALL, "a declared event has no call site"
    assert missing == []


class Journal:
    """What `logs.configure` writes, captured as the parsed lines, from every task and thread."""

    def __init__(self) -> None:
        logs.configure()
        self._stream = io.StringIO()
        installed = [handler for handler in logging.getLogger().handlers if getattr(handler, "_display_log_handler", False)]
        assert len(installed) == 1
        installed[0].setStream(self._stream)

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self._stream.getvalue().splitlines() if line]

    def events(self, event: str) -> list[dict]:
        return [line for line in self.lines() if line.get("event") == event]


def _faults(line: dict, walls: set[str], cache_dir: Path) -> list[tuple]:
    """What is wrong with one journal line of the drive below: its wall, its label, or its list."""
    event = line.get("event")
    faults: list[tuple] = []
    if event in WALL_EVENTS | NAMES_ITS_WALL:
        if line.get("wall_id") not in walls:
            faults.append((event, "no wall", line.get("wall_id")))
    elif event in LABEL_EVENTS:
        if (line.get("label_id"), line.get("wall_id")) != ("label-1", "hall"):
            faults.append((event, "not its label and wall", line.get("label_id"), line.get("wall_id")))
    elif event in PROCESS_EVENTS:
        if "wall_id" in line or "label_id" in line:
            faults.append((event, "a process line naming a wall or label", line.get("wall_id"), line.get("label_id")))
    else:
        faults.append((event, "in no list"))
    root = str(cache_dir) + os.sep
    for value in line.values():
        if isinstance(value, str) and value.startswith(root):
            directory = value[len(root) :].split(os.sep, 1)[0]
            if directory in walls and line.get("wall_id") != directory:
                faults.append((event, f"names {directory}'s path", line.get("wall_id")))
    return faults


async def test_every_line_a_worker_logs_names_its_wall(monkeypatch, client, server, drm, television, connectors):
    """**Each kind of worker, through the suite's doubles, as the process runs them.**

    A Frame wall (living-room), a screen wall (hall, on hdmi-a-1), a screen wall
    the server has published nothing for (porch, on hdmi-a-2, so its pull is
    refused), and a label renderer captioning hall. Every line logged inside a
    wall's worker must carry that wall, every line inside the label's renderer
    the label and its wall, and none of the process's lines a wall; a line whose
    event is in no list fails too. Where a line names a path in a wall's
    directory, the wall it carries must be that directory's, which is what
    tells "the right wall" from "a wall".
    """
    from fakes import FakeSurface

    surface = FakeSurface()
    monkeypatch.setattr(entry, "label_surface", lambda _settings: surface)
    settings = replace(client, panel=replace(client.panel, epd_device="omni_epd.mock"), label_poll_seconds=0.02)
    panels = entry.label_panels(settings)
    server.publish("w1", "w2", wall_id="living-room")
    server.assign("hall", "hdmi-a-1")
    server.publish("h1", "h2", wall_id="hall")
    server.assign("porch", "hdmi-a-2")
    server.map_label("label-1", "epd-0", "hall", {**LABEL_DOCUMENT, "wall_id": "hall"})
    journal = Journal()

    supervisor = supervisor_for(
        settings,
        drm,
        label_worker=partial(entry.run_label, settings, panels, clock=Clock.system()),
        label_outputs=lambda: [panel.report() for panel in panels.values()],
        forget_labels=entry.LabelCacheSweep(settings),
    )
    try:
        async with Running(supervisor):
            # Waited for by what the doubles saw, never by the field under test.
            await eventually(lambda: journal.events("pull.refused"), what="porch's refusal")
            await eventually(lambda: television.on_the_wall is not None, what="the Frame to show living-room")
            await eventually(lambda: "hdmi-a-1" in connectors and connectors["hdmi-a-1"].shown, what="hall to be drawn")
            await eventually(lambda: journal.events("label.drawn"), what="the label to be drawn")
    finally:
        surface.release.set()

    # The Player's own lines: the double's access log goes through the same root handler.
    lines = [line for line in journal.lines() if line["logger"].startswith("arrt_player")]
    wrong = [fault for line in lines for fault in _faults(line, {"living-room", "hall", "porch"}, settings.cache_dir)]

    assert wrong == [], "\n".join(sorted({" ".join(map(str, fault)) for fault in wrong}))
    # What makes the check above more than vacuous: each kind of worker logged,
    # and from more than the call sites that already passed their wall.
    seen = {(line.get("event"), line.get("wall_id")) for line in lines}
    assert {
        ("pull.refused", "porch"),
        ("pull.adopted", "living-room"),
        ("pull.adopted", "hall"),
        ("compose.done", "hall"),
        ("screen.started", "hall"),
        ("frame.started", "living-room"),
        ("label.drawn", "hall"),
        ("client.started", None),
    } <= seen


async def test_every_line_a_player_drawing_to_files_logs_names_its_wall(client, server, drm, tmp_path):
    """**The file screen and the file label, the kinds of worker a hardware-less Player adds, held to the same rule.**

    A Player with no hardware (`FILE_OUTPUT_DIR`): a wall (hall) on the file
    screen, and the file label captioning it. Each line is judged by `_faults`
    exactly as the hardware drive above judges it.
    """
    from arrt_player.config import FILE_LABEL_OUTPUT, FILE_SCREEN_OUTPUT, FileOutputSettings
    from arrt_player.displays.file_output import RECORD_FILENAME

    files = FileOutputSettings(directory=tmp_path / "drawn", screen=(640, 360), label=True)
    settings = replace(client, frame=None, panel=replace(client.panel, epd_device=""), files=files, label_poll_seconds=0.02)
    panels = entry.label_panels(settings)
    assert set(panels) == {FILE_LABEL_OUTPUT}
    # The double's own wall is on a Frame, which a Player drawing to files has not.
    server.unassign("living-room")
    server.publish("h1", "h2", wall_id="hall")
    server.assign("hall", FILE_SCREEN_OUTPUT)
    server.map_label("label-1", FILE_LABEL_OUTPUT, "hall", {**LABEL_DOCUMENT, "wall_id": "hall"})
    journal = Journal()

    def recorded(output: str) -> dict:
        path = files.directory / output / RECORD_FILENAME
        return json.loads(path.read_text()) if path.is_file() else {}

    supervisor = supervisor_for(
        settings,
        drm,
        outputs=partial(entry.player_outputs, settings),
        label_worker=partial(entry.run_label, settings, panels, clock=Clock.system()),
        label_outputs=lambda: [panel.report() for panel in panels.values()],
        forget_labels=entry.LabelCacheSweep(settings),
    )
    async with Running(supervisor):
        # Waited for by what the outputs wrote, never by the field under test.
        await eventually(lambda: recorded(FILE_SCREEN_OUTPUT).get("outcome") == "picture", what="hall to be drawn")
        await eventually(lambda: recorded(FILE_LABEL_OUTPUT).get("wall_id") == "hall", what="the label to be drawn")

    lines = [line for line in journal.lines() if line["logger"].startswith("arrt_player")]
    wrong = [fault for line in lines for fault in _faults(line, {"hall"}, settings.cache_dir)]

    assert wrong == [], "\n".join(sorted({" ".join(map(str, fault)) for fault in wrong}))
    seen = {(line.get("event"), line.get("wall_id"), line.get("label_id")) for line in lines}
    assert {
        ("pull.adopted", "hall", None),
        ("compose.done", "hall", None),
        ("screen.started", "hall", None),
        ("label.drawn", "hall", "label-1"),
        ("client.started", None, None),
    } <= seen


# -- a wall's directory goes a week after the wall does ------------------------------------------

DAY = 24 * 60 * 60
#: What one poll's sweep is credited with at most; the tests step by it.
STEP = 10 * 60


class Days:
    """The wall clock and the monotonic clock, moved by hand; `jump` moves the wall clock alone."""

    def __init__(self) -> None:
        self.wall = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        self.mono = 5000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def jump(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)


class Documents:
    """A client link whose answers the test sets: the cached document at start, and what each poll fetches."""

    def __init__(self, cached: ClientDocument | None = None) -> None:
        self.at_start = cached
        #: What every fetch answers until changed: a document, or None (304, unreachable, refused, unreadable).
        self.answer: ClientDocument | None = None

    def cached(self) -> ClientDocument | None:
        return self.at_start

    async def fetch(self) -> ClientDocument | None:
        return self.answer

    async def report(self, heartbeat) -> bool:
        return True

    async def close(self) -> None:
        return None


HDMI = [OutputReport(name=f"hdmi-a-{n}", kind="framebuffer", connected=True, screen=(1920, 1080)) for n in (1, 2, 3, 4)]


def placing(*walls: str, on: dict[str, str] | None = None) -> ClientDocument:
    """A document placing each wall here, on an HDMI connector of its own unless `on` says otherwise."""
    outputs = on or {}
    return ClientDocument(
        client_id=CLIENT_ID,
        name="The Pi in the hall",
        walls=tuple(
            Assignment(wall_id=wall, name=wall, output=outputs.get(wall, f"hdmi-a-{n}")) for n, wall in enumerate(walls, 1)
        ),
    )


def a_wall_directory(cache_dir: Path, wall_id: str, *, master: bytes = b"a master") -> Path:
    """A wall's directory as its worker and pull leave it: a feed, a master, a composition, a heartbeat."""
    root = cache_dir / wall_id
    (root / "media").mkdir(parents=True)
    (root / "media" / "sha256-feed").write_bytes(master)
    (root / COMPOSED_DIRNAME).mkdir()
    (root / COMPOSED_DIRNAME / "picture.jpg").write_bytes(b"composed")
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    (root / f"display-heartbeat-{wall_id}.json").write_text("{}", encoding="utf-8")
    return root


def snapshot(root: Path) -> dict[str, bytes | str]:
    """Every path under `root`, without following a link, with a file's bytes or a link's target."""
    found: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            found[key] = f"-> {path.readlink()}"
        elif path.is_file():
            found[key] = path.read_bytes()
        else:
            found[key] = "dir"
    return found


class Player:
    """A supervisor polled by hand on `Days`, as one process: `restart` is a new one on the same disk."""

    def __init__(self, client_settings: ClientSettings, days: Days, link: Documents) -> None:
        self.settings, self.days, self.link = client_settings, days, link
        self.supervisor = self._new()

    def _new(self) -> Supervisor:
        return Supervisor(
            settings=self.settings,
            link=self.link,
            worker=_idle,
            outputs=lambda: HDMI,
            now=self.days.now,
            monotonic=self.days.monotonic,
        )

    async def start(self) -> None:
        cached = self.link.cached()
        if cached is not None:
            self.supervisor.reconcile(cached)
        await self.supervisor.cycle()

    async def run_for(self, seconds: float) -> None:
        """Poll through `seconds` of both clocks, a sweep every `STEP`."""
        for _ in range(round(seconds / STEP)):
            self.days.wall += timedelta(seconds=STEP)
            self.days.mono += STEP
            await self.supervisor.cycle()
            # The poll's waits, which let the workers run: a fetch and a report that answer at once never yield.
            await asyncio.sleep(0)

    async def restart(self, link: Documents) -> None:
        await self.supervisor.stop_all()
        self.link = link
        self.supervisor = self._new()
        await self.start()

    async def stop(self) -> None:
        await self.supervisor.stop_all()


def stamped(directory: Path) -> bool:
    return (directory / client_module.LEFT_STAMP_FILENAME).exists()


@pytest.mark.parametrize(
    ("walls", "leaving"),
    [(("east", "west"), {"west"}), (("x1", "x2", "x3"), {"x1", "x3"})],
    ids=["one of two leaves", "two of three leave"],
)
async def test_a_wall_that_left_keeps_its_directory_for_six_days_and_loses_it_by_the_eighth(
    client_settings, caplog, walls, leaving
):
    caplog.set_level(logging.INFO)
    cache = client_settings.cache_dir
    for wall in walls:
        a_wall_directory(cache, wall)
    link = Documents(cached=placing(*walls))
    player = Player(client_settings, Days(), link)
    await player.start()
    staying = [wall for wall in walls if wall not in leaving]
    link.answer = placing(*staying)

    await player.run_for(6 * DAY)
    assert all((cache / wall).is_dir() for wall in walls), "a directory went before its week"
    assert {wall for wall in walls if stamped(cache / wall)} == leaving

    await player.run_for(2 * DAY)
    await player.stop()

    assert sorted(path.name for path in cache.iterdir() if not path.name.startswith(".")) == sorted(staying)
    assert not any(path.name.startswith(".removing-") for path in cache.iterdir()), "a deletion was left half done"
    removed = [record for record in caplog.records if getattr(record, "event", None) == "client.wall_cache_removed"]
    assert sorted(record.wall_id for record in removed) == sorted(leaving)
    said = [record for record in caplog.records if getattr(record, "event", None) == "client.wall_cache_stamped"]
    assert sorted(record.wall_id for record in said) == sorted(leaving), "a stamp was said other than once per wall"


async def test_a_wall_that_comes_back_is_unstamped_and_loses_nothing(client_settings):
    """Gone three days and back: nothing removed, the stamp gone, and a later leaving counts its week from then."""
    cache = client_settings.cache_dir
    for wall in ("east", "west"):
        a_wall_directory(cache, wall)
    before = snapshot(cache / "west")
    link = Documents(cached=placing("east", "west"))
    player = Player(client_settings, Days(), link)
    await player.start()

    link.answer = placing("east")
    await player.run_for(3 * DAY)
    assert stamped(cache / "west")
    link.answer = placing("east", "west")
    await player.run_for(STEP)
    assert not stamped(cache / "west")
    await player.run_for(8 * DAY)
    assert snapshot(cache / "west") == before, "a wall that came back lost something"

    link.answer = placing("east")
    await player.run_for(6 * DAY)
    assert (cache / "west").is_dir(), "the three days before it came back were counted again"
    await player.run_for(1 * DAY + 60 * 60)
    await player.stop()
    assert not (cache / "west").exists()


async def test_a_stamp_reaches_the_disk_before_it_is_renamed_into_place(client_settings, monkeypatch):
    """The stamp is the only record of when a wall left, so it is forced to the card as the pull's files are."""
    cache = client_settings.cache_dir
    for wall in ("east", "west"):
        a_wall_directory(cache, wall)
    synced: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    link = Documents(cached=placing("east", "west"))
    player = Player(client_settings, Days(), link)
    await player.start()
    link.answer = placing("east")
    await player.run_for(DAY)
    await player.stop()

    stamp = cache / "west" / client_module.LEFT_STAMP_FILENAME
    assert stamp.exists()
    assert stamp.stat().st_ino in synced, "the stamp was renamed into place without being forced to the disk"


async def test_a_stamp_that_cannot_be_renamed_into_place_leaves_no_temporary_file(client_settings, monkeypatch):
    cache = client_settings.cache_dir
    for wall in ("east", "west"):
        a_wall_directory(cache, wall)
    real_replace = Path.replace

    def refusing_replace(self: Path, target):
        if Path(target).name == client_module.LEFT_STAMP_FILENAME:
            raise OSError(28, "No space left on device")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", refusing_replace)
    link = Documents(cached=placing("east", "west"))
    player = Player(client_settings, Days(), link)
    await player.start()
    link.answer = placing("east")
    await player.run_for(DAY)
    await player.stop()

    left_behind = [path.name for path in (cache / "west").iterdir() if path.name.endswith(".tmp")]
    assert left_behind == [], f"a failed stamp left {left_behind} in the wall's directory"
    assert not (cache / "west" / client_module.LEFT_STAMP_FILENAME).exists()


async def test_nothing_is_removed_or_stamped_while_no_client_document_can_be_read(client_settings):
    """**The server down at boot, and no cached document that can be read.** This
    client does not know which walls it drives, and "none" would be every wall's
    directory. A stamp an earlier process left a minute short of its week is not
    pushed over it either."""
    cache = client_settings.cache_dir
    for wall in ("east", "west", "north"):
        a_wall_directory(cache, wall)
    (cache / "north" / client_module.LEFT_STAMP_FILENAME).write_text(
        json.dumps({"left_at": "2026-10-01T00:00:00+00:00", "absent_seconds": 7 * DAY - 60}), encoding="utf-8"
    )
    before = snapshot(cache)
    player = Player(client_settings, Days(), Documents(cached=None))
    await player.start()

    await player.run_for(30 * DAY)
    await player.stop()

    assert snapshot(cache) == before


async def test_a_document_unreadable_on_day_three_adds_nothing_to_the_week(client_settings):
    """Three days gone, then a restart with the server down and the cached document
    spoiled, for two days; then the server answers. The two days count for nothing,
    and the three before the restart are kept by the stamp."""
    cache = client_settings.cache_dir
    for wall in ("east", "west"):
        a_wall_directory(cache, wall)
    link = Documents(cached=placing("east", "west"))
    player = Player(client_settings, Days(), link)
    await player.start()
    link.answer = placing("east")
    await player.run_for(3 * DAY)

    await player.restart(Documents(cached=None))
    await player.run_for(2 * DAY)
    assert stamped(cache / "west")
    back = Documents(cached=None)
    player.link = back
    player.supervisor._link = back
    back.answer = placing("east")
    await player.run_for(3 * DAY + 12 * 60 * 60)
    assert (cache / "west").is_dir(), "the days with no document were counted"
    await player.run_for(1 * DAY)
    await player.stop()

    assert not (cache / "west").exists()
    assert (cache / "east").is_dir()


async def test_the_week_survives_a_restart(client_settings):
    """Four days, a restart that reads its cached document, three days more: gone, losing at most a rewrite's worth."""
    cache = client_settings.cache_dir
    for wall in ("east", "west"):
        a_wall_directory(cache, wall)
    link = Documents(cached=placing("east", "west"))
    player = Player(client_settings, Days(), link)
    await player.start()
    link.answer = placing("east")
    await player.run_for(4 * DAY)

    restarted = Documents(cached=placing("east"))
    await player.restart(restarted)
    await player.run_for(2 * DAY + 12 * 60 * 60)
    assert (cache / "west").is_dir()
    await player.run_for(12 * 60 * 60 + client_module.LEFT_STAMP_REWRITE_SECONDS + 60 * 60)
    await player.stop()

    assert not (cache / "west").exists(), "the four days before the restart were lost"


async def test_a_clock_that_jumps_forward_expires_no_stamp_early(client_settings):
    """The wall clock a month ahead in one step, then the monotonic clock eight days in one: neither is a week of polls."""
    cache = client_settings.cache_dir
    for wall in ("east", "west"):
        a_wall_directory(cache, wall)
    link = Documents(cached=placing("east", "west"))
    days = Days()
    player = Player(client_settings, days, link)
    await player.start()
    link.answer = placing("east")
    await player.run_for(DAY)

    days.jump(30 * DAY)
    await player.run_for(STEP)
    assert (cache / "west").is_dir(), "a wall clock a month ahead expired the stamp"
    days.mono += 8 * DAY
    await player.supervisor.cycle()
    assert (cache / "west").is_dir(), "one sweep after a leap was credited with the whole leap"
    await player.run_for(6 * DAY)
    await player.stop()

    assert not (cache / "west").exists(), "the week of polls did not end in removal"


async def test_a_master_a_driven_wall_also_holds_is_never_removed(client_settings):
    """The same work on two walls: each wall's master is its own file, and the one still shown keeps its copy."""
    cache = client_settings.cache_dir
    a_wall_directory(cache, "east", master=b"the same work")
    a_wall_directory(cache, "west", master=b"the same work")
    kept = snapshot(cache / "east")
    link = Documents(cached=placing("east", "west"))
    player = Player(client_settings, Days(), link)
    await player.start()
    link.answer = placing("east")

    await player.run_for(8 * DAY)
    await player.stop()

    assert not (cache / "west").exists()
    assert snapshot(cache / "east") == kept


async def test_nothing_but_a_departed_walls_own_directory_is_ever_removed(client_settings, tmp_path, caplog):
    """**Everything else the cache root can hold is left as it is**: the Player's
    own dotted files and directories, the television's token, a plain name that
    is not a wall's directory, a plain file, a symlink to a wall-shaped directory
    outside, and a directory differing only in case from a wall still placed.
    Inside the removed directory, a link out is removed and what it points at is
    not. Each unknown entry is said once."""
    caplog.set_level(logging.INFO)
    cache = client_settings.cache_dir
    a_wall_directory(cache, "east")
    gone = a_wall_directory(cache, "west")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_bytes(b"not the Player's")
    (gone / "link-out").symlink_to(outside / "precious")
    (cache / ".labels").mkdir()
    (cache / ".labels" / "label-1.json").write_text("{}", encoding="utf-8")
    for name in (".client.json", ".client.etag", ".settings.json", ".slideshow-listing.json", "token_file", "readme"):
        (cache / name).write_text(name, encoding="utf-8")
    (cache / ".slideshow").mkdir()
    (cache / ".slideshow" / "clip.mp4").write_bytes(b"clip")
    (cache / ".mpv").mkdir()
    (cache / "notes").mkdir()
    (cache / "notes" / "todo.txt").write_text("keep", encoding="utf-8")
    a_wall_directory(outside, "elsewhere")
    (cache / "linked").symlink_to(outside / "elsewhere")
    a_wall_directory(cache, "Hall")
    outside_before = snapshot(outside)
    before = {key: value for key, value in snapshot(cache).items() if not key.startswith("west")}
    link = Documents(cached=placing("east", "west", "hall"))
    player = Player(client_settings, Days(), link)
    await player.start()
    link.answer = placing("east", "hall")

    await player.run_for(8 * DAY)
    await player.stop()

    after = snapshot(cache)
    assert not (cache / "west").exists()
    assert after == before
    assert snapshot(outside) == outside_before
    unknown = sorted(
        Path(record.path).name for record in caplog.records if getattr(record, "event", None) == "client.cache_entry_unknown"
    )
    assert unknown == ["linked", "notes", "readme"]


async def test_a_name_a_wall_may_not_take_is_never_removed_even_dressed_as_a_wall(client_settings):
    """`token_file` in another case, holding a wall's files: reserved without case, as a wall id is refused."""
    cache = client_settings.cache_dir
    a_wall_directory(cache, "Token_File")
    before = snapshot(cache)
    link = Documents(cached=placing())
    player = Player(client_settings, Days(), link)
    await player.start()

    await player.run_for(8 * DAY)
    await player.stop()

    assert snapshot(cache) == before


async def test_a_wall_moved_between_this_clients_outputs_has_not_left(client_settings):
    cache = client_settings.cache_dir
    a_wall_directory(cache, "east")
    link = Documents(cached=placing("east", on={"east": "hdmi-a-1"}))
    player = Player(client_settings, Days(), link)
    await player.start()

    link.answer = placing("east", on={"east": "hdmi-a-2"})
    await player.run_for(8 * DAY)
    await player.stop()

    assert player.supervisor.running == {}
    assert (cache / "east").is_dir()
    assert not stamped(cache / "east")


async def test_a_wall_placed_on_an_output_this_client_lacks_has_not_left(client_settings):
    """Placed here and not drawable is still this client's wall: its screen may be plugged in tomorrow."""
    cache = client_settings.cache_dir
    a_wall_directory(cache, "east")
    link = Documents(cached=placing("east", on={"east": "hdmi-a-9"}))
    player = Player(client_settings, Days(), link)
    await player.start()

    await player.run_for(8 * DAY)
    await player.stop()

    assert (cache / "east").is_dir()
    assert not stamped(cache / "east")


async def test_stopping_the_client_stamps_nothing(client_settings):
    """A Player going down has not seen any wall leave."""
    cache = client_settings.cache_dir
    for wall in ("east", "west"):
        a_wall_directory(cache, wall)
    before = snapshot(cache)
    player = Player(client_settings, Days(), Documents(cached=placing("east", "west")))
    await player.start()
    assert set(player.supervisor.running) == {"east", "west"}

    await player.stop()

    assert player.supervisor.running == {}
    assert snapshot(cache) == before
