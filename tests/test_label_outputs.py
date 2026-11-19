"""Labels are outputs a Player has zero or more of, as displays are.

**The owner's ruling of 2026-11-19**: "I'm fine recording one label panel per
player right now, but we shouldn't design anything that precludes having multiple
labels, just as we don't prevent having multiple displays per player. In the
future, there may be players that only do labels and might have multiple." So the
configuration records one panel as its limit (`config.PANEL_OUTPUT`, and
`test_config.py` for the refusal of a second), and everything past it holds the
panels by output name. These tests pin the two shapes the ruling names — two
labels on one Player, and a Player with labels and no display — through the
composition root (`__main__._run`), each accepted or refused by name and never
collapsed onto one panel.

**And what a label leaves on disk when it goes.** A label's document is cached
under `CACHE_DIR` by its id (`pull.LabelPull`); a label the client document no
longer maps leaves that file behind unless something removes it, and the cache
then grows with every label that ever left. It is removed once a document has
been read, never while the document is unreadable or absent.
"""

import asyncio
import logging
import os
import signal
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Self

import pytest
from aiohttp.test_utils import TestServer
from fakes import FakeSurface, drm_tree
from server_double import CLIENT_ID, ServerDouble

from arrt_player import __main__ as entry
from arrt_player.client import LabelAssignment, WorkerStop, client_outputs
from arrt_player.config import UiSettings
from arrt_player.label_renderer import LabelPanel
from arrt_player.label_rule import HOLD
from arrt_player.pull import LABEL_CACHE_DIRNAME, LabelPull


def a_label_document(title: str, *, wall_id: str = "living-room", since: datetime | None = None) -> dict:
    """A label document showing art, captioned `title`, since `since` (an hour before now by default)."""
    at = since if since is not None else datetime.now(UTC) - timedelta(hours=1)
    return {
        "schema": {"major": 1, "minor": 0},
        "wall_id": wall_id,
        "wall_name": wall_id,
        "display_state": {"state": "showing_art", "work_id": f"work-{title}", "since": at.isoformat()},
        "label": {"title": title},
    }


async def eventually(
    predicate,
    *,
    what: str,
    timeout: float = 5.0,  # noqa: ASYNC109 -- a test helper's own deadline, not a cancellation scope
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(autouse=True)
def _settings_page(monkeypatch, cache_dir: Path) -> None:
    """`_run` serves its page on loopback, so it never reads the real environment and refuses before its SIGTERM handler."""
    page = UiSettings(cache_dir=cache_dir, bind="127.0.0.1", port=_free_port(), pinned=frozenset())
    monkeypatch.setattr(entry, "load_ui", lambda: page)


@pytest.fixture
async def server():
    """A server whose client has no walls: these Players are given labels only."""
    double = ServerDouble()
    double.unassign("living-room")
    test_server = TestServer(double.app())
    await test_server.start_server()
    double.server = test_server
    double.url = str(test_server.make_url("")).rstrip("/")
    yield double
    await test_server.close()


@pytest.fixture
def labels_only(client_settings, server):
    """A Player with no Frame and no screen: a panel configured, polling fast enough to watch."""
    return replace(
        client_settings,
        server_url=server.url,
        frame=None,
        panel=replace(client_settings.panel, epd_device="omni_epd.mock"),
        client_poll_seconds=0.05,
        label_poll_seconds=0.02,
    )


@pytest.fixture
def no_screens(tmp_path: Path) -> Path:
    """A kernel listing no display connectors at all."""
    return drm_tree(tmp_path / "drm", {})


def _panel(name: str, surface: FakeSurface) -> LabelPanel:
    return LabelPanel(name=name, surface=surface, error=None, size=(1448, 1072))


class Player:
    """`__main__._run` as systemd runs it, with these panels, until SIGTERM on the way out."""

    def __init__(self, monkeypatch, settings, drm: Path, panels: dict[str, FakeSurface] | None = None) -> None:
        monkeypatch.setattr(entry, "load", lambda: settings)
        monkeypatch.setattr(entry, "client_outputs", lambda s, **kw: client_outputs(s, drm_root=drm, **kw))
        if panels is not None:
            monkeypatch.setattr(entry, "label_panels", lambda _s: {name: _panel(name, sf) for name, sf in panels.items()})
        self._surfaces = list((panels or {}).values())
        self._running: asyncio.Task[int] | None = None

    async def __aenter__(self) -> Self:
        self._running = asyncio.create_task(entry._run())
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._running is not None
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(self._running, timeout=5)
        finally:
            for surface in self._surfaces:
                surface.release.set()


def _texts(surface: FakeSurface) -> list[list[str]]:
    return [[block.text for block in layout.blocks] for layout in surface.shown]


# -- the ruling's two shapes -----------------------------------------------------------------


async def test_a_player_with_a_panel_and_no_display_draws_its_label_and_reports_no_outputs(
    monkeypatch, labels_only, server, no_screens
):
    """A label-only Player: no Frame, no connector, no wall, one label on its panel."""
    surface = FakeSurface()
    monkeypatch.setattr(entry, "label_surface", lambda _settings: surface)
    server.map_label("label-1", "epd-0", "a-wall-on-another-client", a_label_document("Cat Litter"))

    async with Player(monkeypatch, labels_only, no_screens):
        await eventually(lambda: surface.shown, what="the label to be drawn")
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")

    assert surface.last_text == ["Cat Litter"]
    heartbeat = server.client_heartbeats[0]
    assert heartbeat["outputs"] == [], "a Player with no display reported one"
    assert [output["name"] for output in heartbeat["label_outputs"]] == ["epd-0"]


async def test_two_labels_on_two_panels_are_each_drawn_on_their_own(monkeypatch, labels_only, server, no_screens):
    """Two panels, as a configuration may one day describe: each label on the panel it names."""
    hall, study = FakeSurface(), FakeSurface()
    server.map_label("label-hall", "epd-0", "hall", a_label_document("Cat Litter", wall_id="hall"))
    server.map_label("label-study", "epd-1", "study", a_label_document("Silver Sun", wall_id="study"))

    async with Player(monkeypatch, labels_only, no_screens, {"epd-0": hall, "epd-1": study}):
        await eventually(lambda: hall.shown and study.shown, what="both labels to be drawn")
        await eventually(lambda: server.client_heartbeats, what="the client heartbeat")
        await asyncio.sleep(0.1)

    assert _texts(hall) == [["Cat Litter"]], "the hall's panel drew another label's caption"
    assert _texts(study) == [["Silver Sun"]], "the study's panel drew another label's caption"
    assert [output["name"] for output in server.client_heartbeats[0]["label_outputs"]] == ["epd-0", "epd-1"]
    assert (hall.closed, study.closed) == (1, 1), "not every panel was powered down on the way out"


async def test_a_label_on_a_panel_this_player_does_not_hold_is_said_once_and_never_drawn_on_the_one_it_does(
    monkeypatch, labels_only, server, no_screens, caplog
):
    held = FakeSurface()
    server.map_label("label-held", "epd-0", "hall", a_label_document("Cat Litter", wall_id="hall"))
    server.map_label("label-elsewhere", "epd-1", "study", a_label_document("Silver Sun", wall_id="study"))

    with caplog.at_level(logging.INFO):
        async with Player(monkeypatch, labels_only, no_screens, {"epd-0": held}):
            await eventually(lambda: held.shown, what="the held label to be drawn")
            await eventually(lambda: len(server.client_heartbeats) >= 1, what="a poll")
            await asyncio.sleep(0.2)

    assert all("Silver Sun" not in texts for texts in _texts(held)), "a label for epd-1 was drawn on epd-0"
    assert held.last_text == ["Cat Litter"]
    refused = [record for record in caplog.records if record.__dict__.get("event") == "client.label_unplaceable"]
    assert [(record.__dict__["label_id"], record.__dict__["output"]) for record in refused] == [("label-elsewhere", "epd-1")]
    assert "epd-1" in refused[0].getMessage()


async def test_a_label_worker_given_an_output_with_no_panel_draws_nothing_and_says_so_by_name(labels_only, server, caplog):
    """The worker's own refusal, for a supervisor and its panels that disagree: never the panel it holds."""
    held = FakeSurface()
    server.map_label("label-elsewhere", "epd-1", "study", a_label_document("Silver Sun", wall_id="study"))
    stop = WorkerStop()

    with caplog.at_level(logging.ERROR):
        worker = asyncio.create_task(
            entry.run_label(
                labels_only, {"epd-0": _panel("epd-0", held)}, LabelAssignment("label-elsewhere", "epd-1", "study"), stop
            )
        )
        await asyncio.sleep(0.2)
        assert not worker.done(), "the worker ended, which the supervisor answers with a restart every backoff step"
        stop.set()
        await asyncio.wait_for(worker, timeout=5)

    assert held.shown == [], "a label for epd-1 was drawn on epd-0"
    said = [record for record in caplog.records if record.__dict__.get("event") == "label.no_panel"]
    assert len(said) == 1
    assert said[0].__dict__["output"] == "epd-1"
    assert "epd-1" in said[0].getMessage()
    assert "epd-0" in said[0].getMessage(), "the refusal does not say which panels this client holds"


# -- the renderer's clocks -------------------------------------------------------------------


async def test_run_label_holds_the_caption_on_the_injected_clock_s_monotonic(labels_only, server, clock):
    """`run_label` hands its clock to the renderer whole: the offline hold is an
    interval, so it runs on the injected monotonic, not the process's."""
    surface = FakeSurface()
    at = clock.as_clock()
    server.map_label(
        "label-1", "epd-0", "hall", a_label_document("Cat Litter", wall_id="hall", since=at.now() - timedelta(hours=1))
    )
    stop = WorkerStop()
    worker = asyncio.create_task(
        entry.run_label(
            labels_only, {"epd-0": _panel("epd-0", surface)}, LabelAssignment("label-1", "epd-0", "hall"), stop, clock=at
        )
    )
    try:
        await eventually(lambda: surface.last_text == ["Cat Litter"], what="the caption to be drawn")
        server.label_status = 503
        await asyncio.sleep(0.1)
        clock.advance(HOLD.total_seconds() - 7.3)
        await asyncio.sleep(0.1)
        assert surface.last_text == ["Cat Litter"], "the caption was blanked inside the hold"

        clock.advance(11.9)
        await eventually(lambda: surface.last_text == [], what="the caption to blank once the hold ran out on the injected clock")
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
        surface.release.set()


# -- what a label leaves on disk -------------------------------------------------------------


async def _cache_through_a_real_pull(settings, server, label_id: str) -> Path:
    """The file a real `LabelPull` fetch writes for `label_id`, learned from the run rather than derived."""
    directory = settings.cache_dir / LABEL_CACHE_DIRNAME
    before = set(directory.iterdir()) if directory.is_dir() else set()
    link = LabelPull(settings, label_id)
    try:
        answer = await link.fetch()
    finally:
        await link.close()
    assert answer.document is not None, "the double did not serve the label"
    written = set(directory.iterdir()) - before
    assert len(written) == 1, f"one fetch wrote {sorted(path.name for path in written)}"
    return written.pop()


async def test_a_retired_label_s_cached_document_is_removed(monkeypatch, labels_only, server, no_screens):
    surface = FakeSurface()
    server.map_label("label-1", "epd-0", "hall", a_label_document("Cat Litter", wall_id="hall"))
    directory = labels_only.cache_dir / LABEL_CACHE_DIRNAME

    async with Player(monkeypatch, labels_only, no_screens, {"epd-0": surface}):
        await eventually(lambda: surface.shown, what="the label to be drawn")
        cached = list(directory.iterdir())
        assert len(cached) == 1, "the renderer's pull cached nothing, so this test proves nothing"

        server.unmap_label("label-1")
        await eventually(lambda: surface.last_text == [], what="the retired label to be left blank")
        await eventually(lambda: not cached[0].exists(), what="the retired label's cached document to be removed")


async def test_a_stale_document_for_an_unmapped_label_is_removed_and_a_mapped_one_kept(
    monkeypatch, labels_only, server, no_screens
):
    server.map_label("label-gone", "epd-0", "hall", a_label_document("Cat Litter", wall_id="hall"))
    stale = await _cache_through_a_real_pull(labels_only, server, "label-gone")
    server.unmap_label("label-gone")
    server.map_label("label-1", "epd-0", "study", a_label_document("Silver Sun", wall_id="study"))
    surface = FakeSurface()

    async with Player(monkeypatch, labels_only, no_screens, {"epd-0": surface}):
        await eventually(lambda: surface.shown, what="the mapped label to be drawn")
        await eventually(lambda: not stale.exists(), what="the unmapped label's document to be removed")
        kept = list((labels_only.cache_dir / LABEL_CACHE_DIRNAME).iterdir())

    assert [path.name for path in kept] == [stale.name.replace("label-gone", "label-1")], "the mapped label lost its file"


async def test_a_label_moved_between_this_player_s_panels_keeps_its_cached_document(monkeypatch, labels_only, server, no_screens):
    """Retiring a panel is not forgetting its label: the label is still this client's, on another output.

    The label route is down for the move, so the renderer on epd-1 can draw only
    from what epd-0's renderer cached: a document removed with epd-0's
    retirement would leave epd-1 blank."""
    hall, study = FakeSurface(), FakeSurface()
    server.map_label("label-1", "epd-0", "hall", a_label_document("Cat Litter", wall_id="hall"))

    async with Player(monkeypatch, labels_only, no_screens, {"epd-0": hall, "epd-1": study}):
        await eventually(lambda: hall.shown, what="the label to be drawn on epd-0")
        cached = list((labels_only.cache_dir / LABEL_CACHE_DIRNAME).iterdir())
        assert len(cached) == 1

        server.label_status = 503
        server.clients[CLIENT_ID]["labels"] = [{"label_id": "label-1", "output": "epd-1", "wall_id": "hall"}]
        await eventually(lambda: hall.last_text == [], what="epd-0 to be left blank")
        await eventually(lambda: study.shown, what="the label to be drawn on epd-1")
        await asyncio.sleep(0.2)

    assert study.last_text == ["Cat Litter"], "the moved label did not start from the document it cached"
    assert cached[0].exists(), "the label's document was removed while it is still drawn here"


@pytest.mark.parametrize("unusable", ["unreadable", "unanswered"])
async def test_nothing_is_removed_while_the_client_document_is_unreadable_or_absent(
    monkeypatch, labels_only, server, no_screens, unusable
):
    server.map_label("label-gone", "epd-0", "hall", a_label_document("Cat Litter", wall_id="hall"))
    stale = await _cache_through_a_real_pull(labels_only, server, "label-gone")
    server.unmap_label("label-gone")
    if unusable == "unreadable":
        server.client_body = b"this is not a client document"
    else:
        server.client_status = 503

    async with Player(monkeypatch, labels_only, no_screens, {"epd-0": FakeSurface()}):
        await eventually(lambda: sum(path == "/client" for _method, path in server.requests) >= 3, what="three client polls")

    assert stale.exists(), "a label's document was removed before any client document was read"


# -- the label cache sweep itself ------------------------------------------------------------


def test_the_sweep_keeps_what_a_mapped_label_reads_and_removes_the_rest(labels_only):
    directory = labels_only.cache_dir / LABEL_CACHE_DIRNAME
    directory.mkdir(parents=True)
    mapped = LabelPull(labels_only, "label-1").cache_path
    assert mapped is not None
    mapped.write_text("{}")
    other = directory / "label-2.json"
    other.write_text("{}")
    leftover = directory / ".label-1.json.tmp"
    leftover.write_text("{}")

    entry.LabelCacheSweep(labels_only)(frozenset({"label-1", "../not-plain"}))

    assert sorted(path.name for path in directory.iterdir()) == [mapped.name]


def test_the_sweep_of_a_cache_that_holds_no_labels_yet_is_nothing(labels_only, caplog):
    with caplog.at_level(logging.INFO):
        entry.LabelCacheSweep(labels_only)(frozenset())

    assert caplog.records == []


def test_a_sweep_that_cannot_remove_is_said_once_and_never_raises(monkeypatch, labels_only, caplog):
    directory = labels_only.cache_dir / LABEL_CACHE_DIRNAME
    directory.mkdir(parents=True)
    (directory / "label-2.json").write_text("{}")

    def refuses(self, *, missing_ok=False):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "unlink", refuses)
    sweep = entry.LabelCacheSweep(labels_only)
    with caplog.at_level(logging.WARNING):
        sweep(frozenset())
        sweep(frozenset())

    said = [record for record in caplog.records if record.__dict__.get("event") == "label.cache_unsweepable"]
    assert len(said) == 1


async def test_the_supervisor_forgets_labels_only_once_a_document_is_read(labels_only, server, no_screens):
    """The wiring at the supervisor, without the panels: the ids handed over are the document's."""
    from arrt_player.client import Supervisor
    from arrt_player.pull import ClientPull
    from arrt_player.wall import Clock

    handed: list[frozenset[str]] = []
    server.map_label("label-1", "epd-7", "hall", a_label_document("Cat Litter", wall_id="hall"))
    supervisor = Supervisor(
        settings=labels_only,
        link=ClientPull(labels_only),
        worker=partial(_never, "a wall"),
        outputs=lambda: client_outputs(labels_only, drm_root=no_screens),
        now=Clock.system().now,
        monotonic=Clock.system().monotonic,
        forget_labels=handed.append,
    )
    stop = asyncio.Event()
    running = asyncio.create_task(supervisor.run(stop))
    try:
        await eventually(lambda: handed, what="the supervisor to hand over the mapped ids")
    finally:
        stop.set()
        await asyncio.wait_for(running, timeout=5)

    assert handed[0] == frozenset({"label-1"}), "a label the document maps, held here or not, was not kept"


async def _never(what, *args) -> None:
    raise AssertionError(f"{what} was started")
