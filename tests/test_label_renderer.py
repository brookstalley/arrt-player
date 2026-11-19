"""The label renderer, driven one poll at a time over a panel double.

A renderer is one label output captioning whichever wall the server maps it to
(`labels-and-surfaces.md` § The model): it polls its label document, applies the
label rule at its own clock, and redraws only when what the panel would show
changes, since an e-paper redraw flashes the panel for about 2 s.

**The label's contracts, driven by what the label now reads**: a label document
saying what the wall shows, rather than a television announcing a picture,
because the label follows the wall's reported state through the server. The
Frame loop's side of that — it reports and draws nothing — is
`test_heartbeat_wiring.py` and `test_display_state.py`.

The governing rule is unchanged: **nothing about a panel may stop anything
else.** Here that is the renderer itself, the client's other renderers and its
walls, none of which a broken panel may hold up.

Clock steps are not multiples of the poll or the hold, so a timer consumed early
cannot pass for one correctly withheld.
"""

import asyncio
import io
import json
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestServer
from fakes import FakeSurface
from server_double import ServerDouble

from arrt_player import label_renderer as renderer_module
from arrt_player import logs
from arrt_player.client import LabelAssignment, WorkerStop
from arrt_player.label_renderer import LabelPanel, LabelRenderer
from arrt_player.label_rule import HOLD, parse_label_document
from arrt_player.panel import TypeScale
from arrt_player.pull import CachedLabel, LabelAnswer, LabelPull

LABEL_ID = "label-hall-panel"
WALL = "w-living"
ASSIGNMENT = LabelAssignment(label_id=LABEL_ID, output="epd-0", wall_id=WALL)

CAT_LITTER = {"title": "Cat Litter", "artist": "Ed Ruscha", "artist_family_name": "Ruscha", "artist_given_name": "Ed"}
SILVER_SUN = {"title": "Silver Sun"}


def a_document(
    state: str = "showing_art",
    *,
    label: dict | None = None,
    work_id: str | None = "work-a",
    since: str | None = "2026-06-21T11:00:00+00:00",
    wall_name: str = "Living room",
) -> dict:
    """A label document as the server sends one; `label` defaults to Cat Litter's while art shows."""
    if label is None and state == "showing_art" and work_id is not None:
        label = CAT_LITTER
    return {
        "schema": {"major": 1, "minor": 0},
        "wall_id": WALL,
        "wall_name": wall_name,
        "display_state": {"state": state, "work_id": work_id if state == "showing_art" else None, "since": since},
        "label": label,
    }


class FakeLink:
    """A label document's route, as the renderer sees it: a new document once, then 304s."""

    def __init__(self) -> None:
        self.pending: dict | None = None
        self.reachable = True
        self.fetches = 0
        self.closed = 0
        #: What the link cached before this renderer started; None for nothing.
        self.stored: CachedLabel | None = None

    def serve(self, document: dict) -> None:
        self.pending = document

    def store(self, document: dict, answered_at) -> None:
        self.stored = CachedLabel(document=parse_label_document(json.dumps(document)), answered_at=answered_at)

    def cached(self) -> CachedLabel | None:
        return self.stored

    async def fetch(self) -> LabelAnswer:
        self.fetches += 1
        if not self.reachable:
            return LabelAnswer(reachable=False)
        document, self.pending = self.pending, None
        return LabelAnswer(reachable=True, document=parse_label_document(json.dumps(document)) if document else None)

    async def close(self) -> None:
        self.closed += 1


async def _comes_back(flag: threading.Event, *, within_seconds: float = 5.0) -> bool:
    """Wait on a worker thread's flag without blocking the loop waiting for it."""
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        if flag.is_set():
            return True
        await asyncio.sleep(0.005)
    return False


@pytest.fixture
def surface() -> Iterator[FakeSurface]:
    made = FakeSurface()
    yield made
    # A draw runs on a worker thread, and one armed to hang would otherwise still
    # be sitting in the panel when the session tries to exit.
    made.release.set()


@pytest.fixture
def link() -> FakeLink:
    return FakeLink()


def panel_on(surface: FakeSurface | None, *, error: str | None = None) -> LabelPanel:
    return LabelPanel(name="epd-0", surface=surface, error=error, size=(1448, 1072))


@pytest.fixture
def panel(surface) -> LabelPanel:
    return panel_on(surface)


def renderer_for(panel: LabelPanel, link, clock) -> LabelRenderer:
    return LabelRenderer(
        assignment=ASSIGNMENT,
        link=link,
        panel=panel,
        now=clock.as_clock().now,
        monotonic=clock.as_clock().monotonic,
        poll_seconds=1.0,
    )


@pytest.fixture
def renderer(panel, link, clock) -> LabelRenderer:
    return renderer_for(panel, link, clock)


async def show(renderer: LabelRenderer, link: FakeLink, document: dict) -> None:
    link.serve(document)
    await renderer.tick()


class TestTheLabelFollowsTheWall:
    async def test_art_on_the_wall_is_captioned(self, renderer, link, surface):
        await show(renderer, link, a_document())

        assert surface.shown, "the wall shows art and no label was drawn"
        # The artist leads and the work follows it — the panel's ordering, not a
        # museum wall's, because the family name is the token read from across a
        # room and a long title is what drove the tombstone off the bottom.
        assert surface.last_text[:2] == ["Ruscha, Ed", "Cat Litter"]

    async def test_the_label_is_set_at_the_surface_s_own_type_scale(self, link, clock):
        """**The seam between the device and the type, pinned on the drawn output.**

        The fake is given a scale no derivation would produce, because a fixture
        carrying the reference wall's own numbers cannot tell "used the surface's
        scale" apart from "hardcoded the reference wall's".
        """
        absurd = TypeScale(primary_px=7, floor_px=3)
        surface = FakeSurface(type_scale=absurd)
        try:
            await show(
                renderer_for(panel_on(surface), link, clock),
                link,
                a_document(label={"title": "Cat Litter", "artist": "Ed Ruscha"}),
            )
        finally:
            surface.release.set()

        sizes = {block.size_px for block in surface.shown[-1].blocks}
        assert sizes <= {absurd.primary_px, absurd.floor_px}, f"the label was set at sizes the surface never gave it: {sizes}"
        assert absurd.primary_px in sizes, "the leading line did not get the surface's primary tier"

    async def test_the_label_changes_with_the_wall(self, renderer, link, surface, clock):
        await show(renderer, link, a_document(label={"title": "Cat Litter"}))
        clock.advance(1.3)
        await show(renderer, link, a_document(label={"title": "Silver Sun"}, work_id="work-b"))

        assert [layout.blocks[0].text for layout in surface.shown] == ["Cat Litter", "Silver Sun"]

    async def test_a_work_with_no_label_text_is_a_blank_caption(self, renderer, link, surface):
        """A work whose institution published nothing is not an error."""
        await show(renderer, link, a_document(label={}))

        assert surface.shown, "an empty label was not drawn"
        assert surface.last_text == []

    async def test_an_unchanged_document_is_drawn_once(self, renderer, link, surface, clock):
        """A redraw flashes the panel: polls that bring nothing new draw nothing.

        Once moved from "the Player's own selection is not drawn twice" — the set
        echoed every selection back, and the rule that redrew on the echo flashed
        the panel twice per rotation. The echo is now a 304.
        """
        await show(renderer, link, a_document())
        for _ in range(4):
            clock.advance(1.1)
            await renderer.tick()

        assert len(surface.shown) == 1

    async def test_the_same_text_for_another_work_is_not_redrawn(self, renderer, link, surface, clock):
        await show(renderer, link, a_document())
        clock.advance(1.3)
        await show(renderer, link, a_document(work_id="work-a-reprint"))

        assert len(surface.shown) == 1

    async def test_a_picture_this_wall_did_not_put_there_is_the_card(self, renderer, link, surface):
        """**Changed with the label rule's table** (`labels-and-surfaces.md` § What a
        label says, "no work to show"): the Frame loop drew this blank, and the
        rule's vectors make it the quiet card with the wall's name, which names
        nothing false about the picture."""
        await show(renderer, link, a_document(work_id=None, label=None))

        assert surface.last_text == ["Living room"]

    async def test_an_unassigned_wall_is_the_card_and_a_renamed_wall_redraws_it(self, renderer, link, surface, clock):
        await show(renderer, link, a_document("unassigned", work_id=None, since=None))
        clock.advance(1.3)
        await show(renderer, link, a_document("unassigned", work_id=None, since=None, wall_name="Hall"))

        assert [layout.blocks[0].text for layout in surface.shown] == ["Living room", "Hall"]


class TestThePanelFollowsTheScreen:
    """Moved from `test_display_state.py`, where the Frame loop drew the panel from its own reading."""

    async def test_television_blanks_the_label_once_and_art_brings_it_back(self, renderer, link, surface, clock):
        await show(renderer, link, a_document())
        assert surface.last_text[:1] == ["Ruscha, Ed"]
        drawn = len(surface.shown)

        clock.advance(3.3)
        await show(renderer, link, a_document("in_use", work_id=None, label=CAT_LITTER))
        assert surface.last_text == [], "a caption stayed up beside somebody's programme"
        assert len(surface.shown) == drawn + 1

        for _ in range(3):
            clock.advance(4.1)
            await renderer.tick()
        assert len(surface.shown) == drawn + 1, "the blank was redrawn on every poll, a flash each time"

        clock.advance(2.9)
        await show(renderer, link, a_document())
        assert surface.last_text[:1] == ["Ruscha, Ed"]

    @pytest.mark.parametrize("state", ["dark", "no_screen"])
    async def test_a_dark_or_absent_screen_blanks_the_label(self, renderer, link, surface, clock, state):
        await show(renderer, link, a_document())
        clock.advance(3.3)
        await show(renderer, link, a_document(state, work_id=None))

        assert surface.last_text == []

    @pytest.mark.parametrize(
        ("minutes", "kept"),
        [(29, True), (31, False)],
        ids=["29 minutes keeps the caption", "31 minutes blanks it"],
    )
    @pytest.mark.parametrize("state", ["unreachable", "silent"])
    async def test_a_wall_that_cannot_say_keeps_the_caption_for_thirty_minutes(
        self, renderer, link, surface, clock, minutes, kept, state
    ):
        await show(renderer, link, a_document())
        drawn = len(surface.shown)

        since = clock.as_clock().now()
        await show(renderer, link, a_document(state, work_id=None, label=CAT_LITTER, since=since.isoformat()))
        assert len(surface.shown) == drawn, "the same caption, held, was redrawn"
        # Stepped in amounts that are no multiple of the hold.
        for _ in range(minutes):
            clock.advance(59.3)
            await renderer.tick()
        clock.advance(minutes * 0.7)
        await renderer.tick()
        await renderer.tick()

        if kept:
            assert surface.last_text[:1] == ["Ruscha, Ed"]
            assert len(surface.shown) == drawn
        else:
            assert surface.last_text == []
            assert len(surface.shown) == drawn + 1, "the blank was drawn more than once"

    def test_the_hold_is_the_owners_thirty_minutes(self):
        assert timedelta(minutes=30) == HOLD

    async def test_the_caption_comes_back_when_the_wall_does(self, renderer, link, surface, clock):
        await show(renderer, link, a_document())
        since = clock.as_clock().now()
        await show(renderer, link, a_document("unreachable", work_id=None, label=CAT_LITTER, since=since.isoformat()))
        clock.advance(HOLD.total_seconds() + 61.7)
        await renderer.tick()
        assert surface.last_text == []

        clock.advance(3.1)
        await show(renderer, link, a_document())

        assert surface.last_text[:1] == ["Ruscha, Ed"]


class TestTheServerGoneAway:
    """Chunk 06 item 3: the renderer keeps its last document and still blanks on time."""

    async def test_the_caption_is_held_for_thirty_minutes_from_the_last_answer_then_blanked_once(
        self, renderer, link, surface, clock
    ):
        await show(renderer, link, a_document())
        clock.advance(2 * 3600 + 13.7)  # the art has been up for hours, answered all along
        await renderer.tick()
        link.reachable = False

        clock.advance(HOLD.total_seconds() - 60.3)
        await renderer.tick()
        assert surface.last_text[:1] == ["Ruscha, Ed"], "the caption went before the hold was up"
        assert len(surface.shown) == 1

        clock.advance(61.1)
        await renderer.tick()
        await renderer.tick()
        assert surface.last_text == []
        assert len(surface.shown) == 2, "the blank was drawn more than once"

    async def test_a_wall_clock_that_jumps_forward_does_not_blank_a_hold_that_is_a_minute_old(
        self, renderer, link, surface, clock
    ):
        """**The hold is an interval, so it is the monotonic clock's.** An NTP
        correction on a Pi that came up believing it was 1970 steps the wall clock
        by hours; 31 minutes of it is one minute of the hold."""
        await show(renderer, link, a_document())
        link.reachable = False

        clock.advance(61.3)
        clock.move_to(clock.as_clock().now() + timedelta(minutes=31, seconds=7.3))
        await renderer.tick()
        await renderer.tick()

        assert surface.last_text[:1] == ["Ruscha, Ed"], "the wall clock's step blanked a hold that was a minute old"
        assert len(surface.shown) == 1

    async def test_a_wall_clock_that_steps_back_does_not_extend_the_hold(self, renderer, link, surface, clock):
        """The other direction: 31 minutes passed, whatever the wall clock says."""
        await show(renderer, link, a_document())
        link.reachable = False

        clock.advance(HOLD.total_seconds() + 61.7)
        clock.move_to(clock.as_clock().now() - timedelta(hours=2, seconds=13.1))
        await renderer.tick()
        await renderer.tick()

        assert surface.last_text == [], "a wall clock stepped back held the caption past thirty minutes"
        assert len(surface.shown) == 2, "the blank was drawn more than once"

    async def test_a_server_that_comes_back_brings_the_caption_back(self, renderer, link, surface, clock):
        await show(renderer, link, a_document())
        link.reachable = False
        clock.advance(HOLD.total_seconds() + 5.3)
        await renderer.tick()
        assert surface.last_text == []

        link.reachable = True
        clock.advance(1.7)
        await renderer.tick()

        assert surface.last_text[:1] == ["Ruscha, Ed"], "a 304 after the outage left the panel blank"

    async def test_a_blank_is_never_turned_into_a_caption_by_the_server_going_away(self, renderer, link, surface, clock):
        await show(renderer, link, a_document("in_use", work_id=None, label=CAT_LITTER))
        link.reachable = False
        clock.advance(1.3)
        await renderer.tick()

        assert surface.last_text == []
        assert len(surface.shown) == 1

    async def test_a_renderer_that_never_reached_the_server_draws_blank(self, renderer, link, surface):
        link.reachable = False

        await renderer.tick()

        assert surface.last_text == []


class TestARestartedRenderer:
    """A renderer started while the server is away captions from its link's cache, for what is left of the hold.

    **The hold runs from the server's last answer before the restart**, by the
    wall clock, because a monotonic reading does not survive one; and never for
    more than the hold from the restart, by the monotonic clock.
    """

    @pytest.fixture
    def away(self, link):
        link.reachable = False
        return link

    async def test_a_cached_caption_is_drawn_on_the_first_tick(self, renderer, away, surface, clock):
        away.store(a_document(), clock.as_clock().now() - timedelta(minutes=4, seconds=13.1))

        await renderer.tick()

        assert surface.last_text[:2] == ["Ruscha, Ed", "Cat Litter"], "a restart during an outage blanked the label"

    async def test_the_hold_runs_from_the_last_answer_before_the_restart_not_from_the_restart(
        self, renderer, away, surface, clock
    ):
        """A caption already held for most of the hold keeps only what is left of it."""
        away.store(a_document(), clock.as_clock().now() - (HOLD - timedelta(seconds=61.3)))

        await renderer.tick()
        assert surface.last_text[:1] == ["Ruscha, Ed"]
        clock.advance(60.1)
        await renderer.tick()
        assert surface.last_text[:1] == ["Ruscha, Ed"], "the caption went before the hold was up"

        clock.advance(2.3)
        await renderer.tick()
        await renderer.tick()

        assert surface.last_text == [], "a restart lengthened the hold"
        assert len(surface.shown) == 2, "the blank was drawn more than once"

    async def test_no_hold_runs_past_thirty_minutes_from_the_restart_whatever_the_wall_clock_says(
        self, renderer, away, surface, clock
    ):
        """A last answer stamped by a clock that was ahead: the restart bounds the hold."""
        start = clock.as_clock().now()
        away.store(a_document(), start + timedelta(minutes=5, seconds=7.9))
        await renderer.tick()
        clock.advance(10 * 60 + 3.7)
        await renderer.tick()
        assert surface.last_text[:1] == ["Ruscha, Ed"]

        clock.advance(HOLD.total_seconds() - 10 * 60 - 3.7 + 1.3)
        await renderer.tick()

        assert surface.last_text == [], "the hold ran past thirty minutes from the restart"

    async def test_a_last_answer_the_wall_clock_puts_in_the_future_holds_nothing(self, renderer, away, surface, clock):
        away.store(a_document(), clock.as_clock().now() + timedelta(seconds=47.3))

        await renderer.tick()

        assert surface.last_text == [], "a caption was held from an answer the clock cannot place"

    @pytest.mark.parametrize("state", ["in_use", "dark", "no_screen", "unassigned"])
    async def test_a_cached_document_that_draws_no_caption_stays_uncaptioned(self, renderer, away, surface, clock, state):
        """The label text rides along, so only the rule keeps it off the panel."""
        away.store(a_document(state, work_id=None, label=CAT_LITTER), clock.as_clock().now() - timedelta(seconds=9.7))

        await renderer.tick()

        assert surface.last_text == [], f"a cached {state} document was captioned"

    async def test_a_cached_held_state_is_bounded_by_the_last_answer_not_its_own_since(self, renderer, away, surface, clock):
        """A wall the server last called silent a minute before: its own since is later than the answer's bound."""
        now = clock.as_clock().now()
        away.store(
            a_document("silent", work_id=None, label=CAT_LITTER, since=(now - timedelta(seconds=63.1)).isoformat()),
            now - (HOLD - timedelta(seconds=10.3)),
        )
        await renderer.tick()
        assert surface.last_text[:1] == ["Ruscha, Ed"]

        clock.advance(11.9)
        await renderer.tick()

        assert surface.last_text == [], "a held state's own since ran the hold past the last answer's"

    async def test_a_document_cached_for_another_wall_is_not_used(self, renderer, away, surface, clock):
        away.store({**a_document(), "wall_id": "w-kitchen"}, clock.as_clock().now() - timedelta(seconds=9.7))

        await renderer.tick()

        assert surface.last_text == [], "the label captioned a wall it no longer captions"

    async def test_a_server_that_answers_with_no_document_retires_the_cached_one(self, renderer, link, surface, clock):
        """A refusal after a restart: the cache stood in for an unreachable server, and the server is reachable."""
        link.store(a_document(), clock.as_clock().now() - timedelta(seconds=9.7))
        link.reachable = False
        await renderer.tick()
        assert surface.last_text[:1] == ["Ruscha, Ed"]

        link.reachable = True
        clock.advance(1.3)
        await renderer.tick()
        assert surface.last_text == [], "a cached caption outlived the server's answer"

        link.reachable = False
        clock.advance(1.9)
        await renderer.tick()
        assert surface.last_text == [], "the cached caption came back when the server went away again"

    async def test_a_new_document_replaces_the_cached_one(self, renderer, link, surface, clock):
        link.store(a_document(), clock.as_clock().now() - timedelta(seconds=9.7))

        await show(renderer, link, a_document(label=SILVER_SUN, work_id="work-b"))

        assert surface.last_text == ["Silver Sun"]


class TestThroughTheRealRoute:
    """The renderer with `pull.LabelPull` against the server double, on the contract's route."""

    @pytest.fixture
    async def server(self):
        double = ServerDouble()
        test_server = TestServer(double.app())
        await test_server.start_server()
        double.test_server = test_server
        double.url = str(test_server.make_url("")).rstrip("/")
        yield double
        await test_server.close()

    @pytest.fixture
    def pulled(self, server, client_settings, panel, clock):
        link = self._link(server, client_settings, clock)
        return link, renderer_for(panel, link, clock)

    @staticmethod
    def _link(server, client_settings, clock) -> LabelPull:
        return LabelPull(
            replace(client_settings, server_url=server.url),
            LABEL_ID,
            now=clock.as_clock().now,
            monotonic=clock.as_clock().monotonic,
        )

    async def test_a_renderer_restarted_while_the_server_is_away_captions_from_the_cache(
        self, server, pulled, client_settings, panel, surface, clock
    ):
        """The plan's case: a new link and renderer, a server gone, a document cached by the last ones."""
        server.map_label(LABEL_ID, "epd-0", WALL, a_document())
        link, renderer = pulled
        try:
            await renderer.tick()
            clock.advance(3 * 60 + 1.1)
            await renderer.tick()  # a 304, which stamps the cached document as answered now
        finally:
            await link.close()
        await server.test_server.close()

        clock.advance(4.7)
        surface.shown.clear()
        restarted_link = self._link(server, client_settings, clock)
        restarted = renderer_for(panel, restarted_link, clock)
        try:
            await restarted.tick()
            assert surface.last_text[:1] == ["Ruscha, Ed"], "a restart during an outage blanked the label"
            clock.advance(HOLD.total_seconds() - 4.7 - 1.3)
            await restarted.tick()
            assert surface.last_text[:1] == ["Ruscha, Ed"], "the hold after the restart was not run from the 304"
            clock.advance(2.9)
            await restarted.tick()
        finally:
            await restarted_link.close()

        assert surface.last_text == [], "the hold after the restart ran past thirty minutes from the last answer"

    async def test_an_unchanged_document_is_asked_for_with_its_etag(self, server, pulled, surface, clock):
        server.map_label(LABEL_ID, "epd-0", WALL, a_document())
        link, renderer = pulled
        try:
            await renderer.tick()
            clock.advance(1.1)
            await renderer.tick()
        finally:
            await link.close()

        assert surface.last_text[:1] == ["Ruscha, Ed"]
        assert len(surface.shown) == 1
        assert server.requests.count(("GET", f"/labels/{LABEL_ID}")) == 2

    async def test_a_changed_document_is_drawn_on_the_next_poll(self, server, pulled, surface, clock):
        server.map_label(LABEL_ID, "epd-0", WALL, a_document())
        link, renderer = pulled
        try:
            await renderer.tick()
            server.label_documents[LABEL_ID] = a_document(label=SILVER_SUN, work_id="work-b")
            clock.advance(1.1)
            await renderer.tick()
        finally:
            await link.close()

        assert surface.last_text == ["Silver Sun"]

    async def test_a_server_gone_away_holds_the_caption_for_thirty_minutes_then_blanks(self, server, pulled, surface, clock):
        server.map_label(LABEL_ID, "epd-0", WALL, a_document())
        link, renderer = pulled
        try:
            await renderer.tick()
            await server.test_server.close()
            clock.advance(HOLD.total_seconds() - 30.7)
            await renderer.tick()
            assert surface.last_text[:1] == ["Ruscha, Ed"]
            clock.advance(31.3)
            await renderer.tick()
        finally:
            await link.close()

        assert surface.last_text == []

    @pytest.mark.parametrize("status", [503, 500])
    async def test_a_5xx_is_the_server_unreachable(self, server, pulled, surface, clock, status):
        server.map_label(LABEL_ID, "epd-0", WALL, a_document())
        link, renderer = pulled
        try:
            await renderer.tick()
            server.label_status = status
            clock.advance(HOLD.total_seconds() + 1.3)
            await renderer.tick()
        finally:
            await link.close()

        assert surface.last_text == []

    async def test_a_refusal_keeps_what_the_panel_shows_and_is_said_once(self, server, pulled, surface, clock, caplog):
        """A 403 is configuration, not an outage: the hold does not start on it."""
        server.map_label(LABEL_ID, "epd-0", WALL, a_document())
        link, renderer = pulled
        try:
            await renderer.tick()
            server.label_status = 403
            with caplog.at_level(logging.ERROR):
                for _ in range(3):
                    clock.advance(HOLD.total_seconds() / 2 + 1.3)
                    await renderer.tick()
        finally:
            await link.close()

        assert surface.last_text[:1] == ["Ruscha, Ed"]
        assert len([r for r in caplog.records if getattr(r, "event", None) == "label.refused"]) == 1

    async def test_a_document_this_reader_cannot_use_keeps_the_last_one(self, server, pulled, surface, clock, caplog):
        server.map_label(LABEL_ID, "epd-0", WALL, a_document())
        link, renderer = pulled
        try:
            await renderer.tick()
            server.label_documents[LABEL_ID] = {**a_document(), "schema": {"major": 2, "minor": 0}}
            with caplog.at_level(logging.ERROR):
                clock.advance(1.1)
                await renderer.tick()
                clock.advance(1.1)
                await renderer.tick()
        finally:
            await link.close()

        assert surface.last_text[:1] == ["Ruscha, Ed"]
        assert len([r for r in caplog.records if getattr(r, "event", None) == "label.document_refused"]) == 1

    async def test_the_request_carries_the_clients_token(self, server, pulled):
        server.map_label(LABEL_ID, "epd-0", WALL, a_document())
        link, renderer = pulled
        try:
            await renderer.tick()
        finally:
            await link.close()

        assert server.authorizations[-1] == "Bearer the-clients-token"


class TestAPanelFailureNeverStopsTheRenderer:
    async def test_a_failure_that_is_not_the_declared_one_is_caught_and_reported(self, renderer, link, surface, caplog):
        """**The half a declared exception type cannot cover**: `measure` reaches
        Pango through C bindings outside the `show` that converts failures."""
        surface.measurement_explodes = True

        with caplog.at_level("WARNING"):
            await show(renderer, link, a_document())

        assert surface.shown == []
        assert [r for r in caplog.records if getattr(r, "event", None) == "label.failed"]

    async def test_a_refusing_panel_is_tried_again_when_the_label_changes(self, renderer, link, surface, clock):
        surface.refuses = True
        await show(renderer, link, a_document())
        clock.advance(1.3)
        await show(renderer, link, a_document(label=SILVER_SUN, work_id="work-b"))

        assert surface.draws_begun == 2

    async def test_the_failure_is_reported_once_not_once_a_change(self, renderer, link, surface, clock, caplog):
        """A panel with a loose ribbon fails every change, all night."""
        surface.refuses = True
        with caplog.at_level("WARNING"):
            await show(renderer, link, a_document())
            clock.advance(1.3)
            await show(renderer, link, a_document(label=SILVER_SUN, work_id="work-b"))

        assert len([r for r in caplog.records if getattr(r, "event", None) == "label.failed"]) == 1

    async def test_a_refusing_panel_is_not_re_asked_on_every_poll(self, renderer, link, surface, clock):
        """The poll is a second and a real draw is seconds. That ratio is the fault."""
        surface.refuses = True
        await show(renderer, link, a_document())
        for _ in range(2):
            clock.advance(1.1)
            await renderer.tick()

        assert surface.draws_begun == 1

    async def test_a_recovered_panel_says_so(self, renderer, link, surface, clock, caplog):
        surface.refuses = True
        await show(renderer, link, a_document())

        surface.refuses = False
        clock.advance(1.3)
        with caplog.at_level("INFO"):
            await show(renderer, link, a_document(label=SILVER_SUN, work_id="work-b"))

        assert [r for r in caplog.records if getattr(r, "event", None) == "label.recovered"]

    async def test_a_panel_that_fails_after_working_is_reported_as_not_connected(
        self, renderer, link, surface, panel, clock, caplog
    ):
        """**The edge.** A ribbon that works cold and fails warm: a report that
        stayed connected would tell the server the panel is fine while nobody in
        the room can read a label. Replaces the wall heartbeat's
        `label_surface_working`, which the Frame loop no longer carries."""
        await show(renderer, link, a_document())
        assert surface.shown, "the panel never worked, so this is not the mid-run case"
        assert panel.report().connected is True

        surface.refuses = True
        clock.advance(1.3)
        with caplog.at_level("WARNING"):
            await show(renderer, link, a_document(label=SILVER_SUN, work_id="work-b"))

        assert len([r for r in caplog.records if getattr(r, "event", None) == "label.failed"]) == 1
        assert panel.report().connected is False

        surface.refuses = False
        clock.advance(1.3)
        await show(renderer, link, a_document())
        assert panel.report().connected is True, "a recovered panel stayed reported as disconnected"


class TestTheDrawIsNotOnTheEventLoop:
    """A full frame is seconds, and every wall on this client polls on the same loop."""

    async def test_the_loop_keeps_running_while_the_panel_draws(self, renderer, link, surface):
        surface.draw_takes_seconds = 0.2
        link.serve(a_document())

        ticking = asyncio.create_task(renderer.tick())
        turns_taken_mid_draw = 0
        while not ticking.done():
            if surface.entered.is_set() and not surface.left.is_set():
                turns_taken_mid_draw += 1
            await asyncio.sleep(0.001)
        await ticking

        assert surface.shown, "the label never drew, so this proves nothing"
        assert turns_taken_mid_draw > 0, "nothing else on the loop got a turn while the panel drew"

    async def test_a_panel_that_never_comes_back_does_not_hold_the_renderer(
        self, renderer, link, surface, clock, caplog, monkeypatch
    ):
        """**The one way a panel can stop things that no `except` clause reaches**: a
        driver wedged in an SPI transaction never returns. Bounded, the renderer
        goes on; and the gate dispatches nothing more into the wedged driver."""
        monkeypatch.setattr(renderer_module, "LABEL_DRAW_BUDGET_SECONDS", 0.05)
        surface.blocks = True

        try:
            with caplog.at_level("WARNING"):
                link.serve(a_document())
                await asyncio.wait_for(renderer.tick(), timeout=10)
                clock.advance(1.3)
                link.serve(a_document(label=SILVER_SUN, work_id="work-b"))
                await asyncio.wait_for(renderer.tick(), timeout=10)

            assert [r for r in caplog.records if getattr(r, "event", None) == "label.failed"]
            assert surface.draws_begun == 1
        finally:
            surface.release.set()
            assert await _comes_back(surface.left), "the draw thread never came back"

    async def test_a_renderer_restarted_onto_a_wedged_panel_does_not_draw_into_it(self, link, surface, panel, clock, monkeypatch):
        """**Why the gate is the panel's and not the renderer's.** A mapping changed
        while a draw is wedged starts a new renderer on the same panel; a gate it
        held itself would let it dispatch a second draw into the same driver."""
        monkeypatch.setattr(renderer_module, "LABEL_DRAW_BUDGET_SECONDS", 0.05)
        surface.blocks = True
        try:
            link.serve(a_document())
            await asyncio.wait_for(renderer_for(panel, link, clock).tick(), timeout=10)
            fresh = FakeLink()
            fresh.serve(a_document(label=SILVER_SUN, work_id="work-b"))
            await asyncio.wait_for(renderer_for(panel, fresh, clock).tick(), timeout=10)

            assert surface.draws_begun == 1
        finally:
            surface.release.set()
            assert await _comes_back(surface.left), "the draw thread never came back"

    async def test_a_draw_that_never_got_a_thread_does_not_close_the_gate_for_ever(
        self, renderer, link, surface, clock, monkeypatch
    ):
        """**The budget can expire before the work starts, not only while it runs.**
        The pool is squeezed to one occupied worker, which is that queue with the
        timing made certain."""
        monkeypatch.setattr(renderer_module, "LABEL_DRAW_BUDGET_SECONDS", 0.05)
        occupied, let_go = threading.Event(), threading.Event()

        with ThreadPoolExecutor(max_workers=1) as only_one_thread:
            asyncio.get_running_loop().set_default_executor(only_one_thread)
            only_one_thread.submit(lambda: (occupied.set(), let_go.wait(30)))
            assert await _comes_back(occupied), "the pool's one worker was never taken"

            link.serve(a_document())
            await asyncio.wait_for(renderer.tick(), timeout=10)
            assert surface.draws_begun == 0, "the draw ran, so this is not the queued case"

            # Let the loop settle before freeing the worker: the two are unrelated
            # in life, and freeing it in the same step hides a cancellation.
            await asyncio.sleep(0.05)
            let_go.set()
            assert await _comes_back(surface.left), "the queued draw never ran once a thread was free"

            clock.advance(1.3)
            link.serve(a_document(label=SILVER_SUN, work_id="work-b"))
            await asyncio.wait_for(renderer.tick(), timeout=10)

        assert surface.draws_begun == 2, "the panel was never drawn to again"

    async def test_a_panel_that_comes_back_is_drawn_to_again(self, renderer, link, surface, clock, monkeypatch):
        """The gate has to open again, and only its own draw can open it."""
        monkeypatch.setattr(renderer_module, "LABEL_DRAW_BUDGET_SECONDS", 0.05)
        surface.blocks = True
        link.serve(a_document())
        await asyncio.wait_for(renderer.tick(), timeout=10)

        surface.release.set()
        assert await _comes_back(surface.left), "the draw thread never came back"
        surface.blocks = False
        clock.advance(1.3)
        link.serve(a_document(label=SILVER_SUN, work_id="work-b"))
        await asyncio.wait_for(renderer.tick(), timeout=10)

        assert surface.shown, "the panel recovered and was never drawn to again"


class TestRunningAndStopping:
    async def test_a_retired_renderer_leaves_its_panel_blank(self, renderer, link, surface):
        """The mapping went: a caption left up would name a wall the panel no longer belongs to."""
        link.serve(a_document())
        stop = WorkerStop()
        running = asyncio.create_task(renderer.run(stop))
        assert await _comes_back(surface.left), "the first label was never drawn"
        assert surface.last_text[:1] == ["Ruscha, Ed"]

        stop.retire()
        await asyncio.wait_for(running, timeout=5)

        assert surface.last_text == []
        assert link.closed == 1

    async def test_a_stopped_renderer_leaves_its_panel_as_it_was_and_open(self, renderer, link, surface):
        """A client going down, or a label moved to another wall: the next drawing
        replaces it, and the panel stays open for the next renderer."""
        link.serve(a_document())
        stop = WorkerStop()
        running = asyncio.create_task(renderer.run(stop))
        assert await _comes_back(surface.left), "the first label was never drawn"

        stop.set()
        await asyncio.wait_for(running, timeout=5)

        assert surface.last_text[:1] == ["Ruscha, Ed"]
        assert len(surface.shown) == 1
        assert surface.closed == 0
        assert link.closed == 1

    async def test_a_retired_renderer_blanks_once_and_says_so_once(self, renderer, link, surface, caplog):
        link.serve(a_document())
        stop = WorkerStop()
        with caplog.at_level(logging.INFO, logger="arrt_player.label_renderer"):
            running = asyncio.create_task(renderer.run(stop))
            assert await _comes_back(surface.left), "the first label was never drawn"
            stop.retire()
            await asyncio.wait_for(running, timeout=5)

        assert [[block.text for block in layout.blocks] for layout in surface.shown][1:] == [[]], "not one blank"
        events = [record.__dict__.get("event") for record in caplog.records]
        assert events.count("label.renderer_started") == 1
        assert events.count("label.renderer_retired") == 1

    async def test_a_renderer_called_already_retired_blanks_and_says_nothing_of_starting(self, renderer, link, surface, caplog):
        """The supervisor's once-more call, for a label unmapped while its renderer
        waited to restart: it leaves the panel blank, asks for nothing, and its line
        says that, not that a renderer started captioning a wall it no longer has."""
        stop = WorkerStop()
        stop.retire()
        with caplog.at_level(logging.INFO, logger="arrt_player.label_renderer"):
            await asyncio.wait_for(renderer.run(stop), timeout=5)

        assert len(surface.shown) == 1
        assert surface.last_text == []
        assert link.fetches == 0, "a retired renderer asked the server for its label"
        assert link.closed == 1
        events = [record.__dict__.get("event") for record in caplog.records]
        assert "label.renderer_started" not in events, "a retired renderer's last call said it started"
        assert events.count("label.renderer_retired") == 1

    async def test_a_renderer_called_already_stopped_and_not_retired_does_nothing(self, renderer, link, surface, caplog):
        stop = WorkerStop()
        stop.set()
        with caplog.at_level(logging.INFO, logger="arrt_player.label_renderer"):
            await asyncio.wait_for(renderer.run(stop), timeout=5)

        assert surface.shown == []
        assert link.fetches == 0
        assert caplog.records == []

    async def test_a_plain_event_stops_a_renderer_without_retiring_it(self, renderer, link, surface):
        """Retirement is said one way, by `WorkerStop`: an event that cannot carry it reads as not retired."""
        link.serve(a_document())
        stop = asyncio.Event()
        running = asyncio.create_task(renderer.run(stop))
        assert await _comes_back(surface.left), "the first label was never drawn"

        stop.set()
        await asyncio.wait_for(running, timeout=5)

        assert surface.last_text[:1] == ["Ruscha, Ed"]
        assert len(surface.shown) == 1

    def test_closing_the_panel_releases_the_surface(self, panel, surface):
        """On e-paper `close()` is the power-down, not bookkeeping."""
        panel.close()

        assert surface.closed == 1


class _RecordsWhenItWasClosed(FakeSurface):
    """A surface that notes whether a draw was inside it when `close` arrived."""

    def __init__(self) -> None:
        super().__init__()
        self.closed_inside_a_draw: list[bool] = []

    def close(self) -> None:
        self.closed_inside_a_draw.append(self.entered.is_set() and not self.left.is_set())
        super().close()


class TestClosingThePanelUnderADraw:
    """**`close` is the e-paper power-down**, and a draw past its budget is left running on purpose."""

    async def test_a_draw_past_the_budget_is_not_closed_under(self, link, clock, caplog, monkeypatch):
        monkeypatch.setattr(renderer_module, "LABEL_DRAW_BUDGET_SECONDS", 0.05)
        surface = _RecordsWhenItWasClosed()
        surface.blocks = True
        panel = panel_on(surface)
        try:
            link.serve(a_document())
            await asyncio.wait_for(renderer_for(panel, link, clock).tick(), timeout=10)
            assert surface.entered.is_set()
            assert not surface.left.is_set(), "the draw came back, so this is not the in-flight case"

            with caplog.at_level("WARNING"):
                panel.close()
                panel.close()

            assert surface.closed == 0, "the panel was powered down under a draw still in the driver"
            left_open = [r for r in caplog.records if getattr(r, "event", None) == "label.close_abandoned"]
            assert len(left_open) == 1, "leaving the panel was not said exactly once"
        finally:
            surface.release.set()
            assert await _comes_back(surface.left), "the draw thread never came back"

    async def test_a_draw_inside_the_budget_is_waited_for_and_then_the_panel_is_closed(self, link, clock, monkeypatch):
        monkeypatch.setattr(renderer_module, "LABEL_DRAW_BUDGET_SECONDS", 5.0)
        surface = _RecordsWhenItWasClosed()
        surface.draw_takes_seconds = 0.3
        panel = panel_on(surface)
        link.serve(a_document())
        ticking = asyncio.create_task(renderer_for(panel, link, clock).tick())
        assert await _comes_back(surface.entered), "the draw never began"

        await asyncio.to_thread(panel.close)
        await ticking

        assert surface.left.is_set(), "close returned with the draw still in the driver"
        assert surface.closed_inside_a_draw == [False]
        assert len(surface.shown) == 1, "the draw close waited for was lost"

    async def test_a_draw_queued_behind_the_close_puts_nothing_on_a_closed_panel(self, link, clock):
        surface = _RecordsWhenItWasClosed()
        panel = panel_on(surface)
        panel.close()

        link.serve(a_document())
        await asyncio.wait_for(renderer_for(panel, link, clock).tick(), timeout=10)

        assert surface.draws_begun == 0, "a draw reached the driver after the panel was powered down"
        assert surface.closed == 1

    def test_closing_a_panel_that_never_opened_is_nothing(self):
        panel_on(None, error="no such device").close()


class TestThePanelAsALabelOutput:
    def test_a_panel_that_has_not_drawn_yet_is_connected(self, panel):
        """Not drawn is not broken: a freshly started client reports its panel connected."""
        assert panel.report().connected is True
        assert panel.report().document() == {"name": "epd-0", "kind": "epaper", "connected": True, "size": [1448, 1072]}

    def test_a_panel_that_would_not_open_is_reported_and_not_connected(self):
        """A configured panel that would not open is a device meant to have a label
        that has none, and that must not read like a client with no panel."""
        broken = panel_on(None, error="could not open the e-paper device 'waveshare_epd.it8951' (no SPI device)")

        assert broken.report().connected is False
        assert broken.report().name == "epd-0"

    async def test_a_panel_that_would_not_open_is_never_drawn_to_and_never_raises(self, link, clock):
        broken = panel_on(None, error="no SPI device")

        await show(renderer_for(broken, link, clock), link, a_document())

        assert broken.report().connected is False


class TestTheJournalSaysWhatThePanelShows:
    """**The panel is a device of its own, and the journal is the only way to ask it.**

    Read through `logs.configure()` rather than `caplog`, because the work id is
    stamped by a filter on the handler `configure` installs; parsing the JSON is
    what an operator does with `journalctl | jq`.
    """

    @pytest.fixture
    def journal(self):
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        logs.configure()
        installed = [handler for handler in root.handlers if handler not in handlers]
        assert len(installed) == 1, "configure() no longer installs exactly one handler"
        written = io.StringIO()
        installed[0].setStream(written)

        def lines() -> list[dict]:
            return [json.loads(line) for line in written.getvalue().splitlines() if line.startswith("{")]

        yield lines
        root.handlers[:] = handlers
        root.setLevel(level)

    @staticmethod
    def _events(lines: list[dict], event: str) -> list[dict]:
        """Keyed on the event name, never on position."""
        return [line for line in lines if line.get("event") == event]

    async def test_a_panel_that_drew_says_which_work_it_captioned_and_for_which_label(self, renderer, link, journal):
        await show(renderer, link, a_document())

        drawn = self._events(journal(), "label.drawn")
        assert [line["work_id"] for line in drawn] == ["work-a"]
        assert drawn[0]["label_id"] == LABEL_ID

    async def test_a_panel_captioning_correctly_is_not_silent_across_changes(self, renderer, link, clock, journal):
        await show(renderer, link, a_document())
        clock.advance(1.3)
        await show(renderer, link, a_document(label={"title": "PH-129"}, work_id="work-b"))

        assert [line["work_id"] for line in self._events(journal(), "label.drawn")] == ["work-a", "work-b"]

    async def test_a_work_with_no_label_text_is_not_reported_as_captioned(self, renderer, link, journal):
        await show(renderer, link, a_document(label={}))

        lines = journal()
        assert self._events(lines, "label.drawn") == []
        assert [line["work_id"] for line in self._events(lines, "label.absent")] == ["work-a"]

    async def test_the_card_is_a_deliberate_outcome_not_a_failure_and_names_no_work(self, renderer, link, journal):
        await show(renderer, link, a_document(work_id=None, label=None))

        lines = journal()
        card = self._events(lines, "label.card")
        assert [line["display_state"] for line in card] == ["showing_art"]
        assert "work_id" not in card[0], "there is no work here; naming one is the failure this avoids"
        assert not self._events(lines, "label.failed")
        assert not self._events(lines, "label.drawn")

    async def test_a_blank_names_the_state_that_asked_for_it_once(self, renderer, link, clock, journal):
        await show(renderer, link, a_document("in_use", work_id=None))
        for _ in range(3):
            clock.advance(1.1)
            await renderer.tick()

        blanked = self._events(journal(), "label.blanked")
        assert [line["display_state"] for line in blanked] == ["in_use"]

    async def test_a_failed_caption_still_names_the_work(self, renderer, link, surface, journal):
        surface.refuses = True
        await show(renderer, link, a_document(work_id="work-b", label=SILVER_SUN))

        failed = self._events(journal(), "label.failed")
        assert [line["work_id"] for line in failed] == ["work-b"]
        assert failed[0]["label_id"] == LABEL_ID

    async def test_a_failure_with_no_work_to_name_still_names_the_label(self, renderer, link, surface, journal):
        surface.refuses = True
        await show(renderer, link, a_document("unassigned", work_id=None, since=None))

        failed = self._events(journal(), "label.failed")
        assert [line["label_id"] for line in failed] == [LABEL_ID]
        assert "work_id" not in failed[0]

    async def _on(self, surface: FakeSurface, link, clock, *documents: dict) -> None:
        renderer = renderer_for(panel_on(surface), link, clock)
        try:
            for document in documents:
                await show(renderer, link, document)
                clock.advance(1.3)
        finally:
            surface.release.set()

    async def test_a_truncated_label_names_the_work_whose_lines_came_off(self, link, clock, journal):
        """The facts that come off have to be optional ones: a title and an artist
        shrink and never truncate."""
        label = {"title": "Cat Litter", "artist": "Ed Ruscha", "medium": "Oil on canvas", "dimensions": "50 × 50 cm"}
        await self._on(FakeSurface(width_px=200, height_px=60, margin_px=5), link, clock, a_document(label=label))

        truncated = self._events(journal(), "label.truncated")
        assert [line["work_id"] for line in truncated] == ["work-a"]
        assert set(truncated[0]["dropped"]) == {"Oil on canvas", "50 × 50 cm"}

    async def test_a_label_set_below_the_floor_says_so_and_names_the_floor(self, link, clock, journal):
        """The condition the type floor's one exception rests on, at WARNING."""
        await self._on(
            FakeSurface(width_px=200, height_px=60, margin_px=5),
            link,
            clock,
            a_document(label={"title": "Cat Litter", "artist": "Ed Ruscha"}),
        )

        shrunk = self._events(journal(), "label.shrunk")
        assert [line["work_id"] for line in shrunk] == ["work-a"]
        assert shrunk[0]["level"] == "WARNING"
        assert set(shrunk[0]["shrunk"]) == {"Ed Ruscha", "Cat Litter"}
        assert shrunk[0]["smallest_px"] < shrunk[0]["floor_px"]

    async def test_a_name_the_line_breaker_split_is_reported_and_names_the_line(self, link, clock, journal):
        label = {"title": "Cat Litter", "artist": "Toulouse-Lautrec", "artist_family_name": "Toulouse-Lautrec"}
        await self._on(FakeSurface(width_px=260, height_px=900, margin_px=10), link, clock, a_document(label=label))

        broken = self._events(journal(), "label.name_wrapped")
        assert [line["level"] for line in broken] == ["WARNING"]
        assert broken[0]["work_id"] == "work-a"
        assert broken[0]["wrapped"] == ["Toulouse-Lautrec"], "the line reported is not the one that broke"
        assert broken[0]["rows"] > 1

    async def test_a_work_with_no_maker_does_not_report_its_title_as_a_broken_name(self, link, clock, journal):
        await self._on(
            FakeSurface(width_px=260, height_px=900, margin_px=10),
            link,
            clock,
            a_document(label={"title": "Stirrup Spout Vessel of Considerable Length"}),
        )

        lines = journal()
        assert [line["work_id"] for line in self._events(lines, "label.drawn")] == ["work-a"]
        assert not self._events(lines, "label.name_wrapped"), "a title was reported as a name the surface could not hold"

    async def test_a_surface_with_no_usable_area_is_the_loudest_outcome_not_the_quietest(self, link, clock, journal):
        await self._on(
            FakeSurface(width_px=100, height_px=100, margin_px=60),
            link,
            clock,
            a_document(label={"title": "Cat Litter", "artist": "Ed Ruscha"}),
        )

        lines = journal()
        unusable = self._events(lines, "label.unusable")
        assert [line["level"] for line in unusable] == ["WARNING"]
        assert unusable[0]["label_id"] == LABEL_ID, "the line names no label"
        assert not self._events(lines, "label.drawn"), "it claimed a caption that is not on the panel"

    async def test_an_unusable_surface_still_reports_the_panel_connected(self, link, clock):
        """**The driver took the frame; the geometry is what failed**, and reporting
        the panel disconnected would send somebody to its wiring for a margin they
        can fix in a config file. The journal's WARNING is what says it."""
        swallowed = FakeSurface(width_px=100, height_px=100, margin_px=60)
        panel = panel_on(swallowed)
        try:
            await show(renderer_for(panel, link, clock), link, a_document())
        finally:
            swallowed.release.set()

        assert panel.report().connected is True

    async def test_the_unusable_surface_is_reported_once_and_not_every_change(self, link, clock, journal):
        """A geometry holds until somebody changes a setting. Three changes rather
        than two, because a gate that resets reports on the first and the third."""
        await self._on(
            FakeSurface(width_px=100, height_px=100, margin_px=60),
            link,
            clock,
            a_document(label={"title": "Cat Litter", "artist": "Ed Ruscha"}),
            a_document(label={"title": "Another Work", "artist": "Someone Else"}, work_id="work-b"),
            a_document(label={"title": "A Third", "artist": "Somebody"}, work_id="work-c"),
        )

        assert len(self._events(journal(), "label.unusable")) == 1

    async def test_a_surface_given_its_area_back_can_report_the_fault_again(self, link, clock, journal):
        """The other half of a gate, and the half that fails silently."""
        swallowed = FakeSurface(width_px=100, height_px=100, margin_px=60)
        renderer = renderer_for(panel_on(swallowed), link, clock)
        try:
            await show(renderer, link, a_document(label={"title": "Cat Litter"}))
            clock.advance(1.3)
            swallowed.resize(width_px=1448, height_px=1072, margin_px=40)
            await show(renderer, link, a_document(label={"title": "Another"}, work_id="work-b"))
            clock.advance(1.3)
            swallowed.resize(width_px=100, height_px=100, margin_px=60)
            await show(renderer, link, a_document(label={"title": "A Third"}, work_id="work-c"))
        finally:
            swallowed.release.set()

        lines = journal()
        assert self._events(lines, "label.drawn"), "the widened surface never captioned, so nothing recovered"
        assert len(self._events(lines, "label.unusable")) == 2, "the episode never re-armed"

    async def test_a_work_whose_institution_published_nothing_is_not_a_fault(self, renderer, link, journal):
        await show(renderer, link, a_document(label={}))

        assert not self._events(journal(), "label.unusable")

    async def test_a_label_that_fits_says_nothing_about_shrinking(self, renderer, link, journal):
        await show(renderer, link, a_document(label={"title": "Cat Litter"}))

        assert not self._events(journal(), "label.shrunk")
