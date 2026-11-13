"""The slideshow programme: order, dwell, refreshes, failures and outages, the label, and its composition.

Time is virtual. Every wait the programme makes goes through `Time.wait`, which
moves the clock by what was asked and lets the other tasks (the listing read,
the fill) run, so a 36 s dwell is asserted exactly and in microseconds. The
source and the downloader are doubles; the cache is the real one, on disk, so
eviction and "only what is cached is shown" are the real code's.

One test runs with real time and the real renderer: a cancelled slideshow must
leave no mpv behind, and only a real process can show that.
"""

import asyncio
import base64
import itertools
import json
import logging
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Self

import pytest
from fakes import FakeMonitor
from test_mpv import Bench, alive, wait_until

from arrt_player import __main__ as entry
from arrt_player.config import Location, PanelSettings, ScreenBrightnessSettings, SlideshowSettings, UiSettings
from arrt_player.dwell import DwellLimits
from arrt_player.label_rule import Drawing
from arrt_player.label_rule import Outcome as LabelOutcome
from arrt_player.mpv import Loaded, NotShown
from arrt_player.panel import read_label
from arrt_player.programmes.slideshow import (
    CHECK_EVERY_S,
    FAILED_REFRESH_S,
    NOT_SHOWN_PAUSE_S,
    REFRESH_S,
    Slideshow,
    label_for,
    load_listing,
    save_listing,
)
from arrt_player.programmes.slideshow_status import SlideshowStatus
from arrt_player.sources import Caption, Item, Kind, Listing, Outcome
from arrt_player.sources.cache import MediaCache
from arrt_player.sources.fetch import MAX_FILE_BYTES, ApiAnswer, Download
from arrt_player.sources.reddit_settings import listing_url
from arrt_player.sources.resolvers.redgifs import API

FIXTURES = Path(__file__).parent / "fixtures"
SHAPES = json.loads((FIXTURES / "reddit" / "shapes.json").read_text())
GIF = json.loads((FIXTURES / "redgifs" / "gif.json").read_text())
AUTH = json.loads((FIXTURES / "redgifs" / "auth.json").read_text())

LIMITS = DwellLimits(30.0, 60.0)
#: On the equator at the prime meridian, so the test clock's noon UTC is the sun's noon.
EQUATOR = Location(latitude=0.0, longitude=0.0, name="Null Island", region="")
# prawduct:allow prawduct/credential-shape -- a made-up cookie in a JWT's shape; it signs nothing and Reddit never saw it
COOKIE = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4MTAwMDAwMDB9.the-secret-part"


def item(name: str, *, url: str | None = None, title: str | None = None, score: int | None = 7) -> Item:
    caption = Caption(title=title or f"Title {name}", community="r/pics", author=f"u/{name}-author", score=score)
    return Item(
        id=f"t3_{name}",
        media_url=url or f"https://i.redd.it/{name}.jpg",
        kind=Kind.STILL,
        caption=caption,
        tier="reddit",
    )


def listed(*items: Item, unresolved: Mapping[str, int] | None = None) -> Listing:
    return Listing(Outcome.LISTED, items=items, unresolved=Counter(unresolved or {}))


class Time:
    """A clock that moves only when the programme waits, and stops the run when `until` says so."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.until: Callable[[], bool] = lambda: False
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def wait(self, stop: asyncio.Event, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        # Let the listing read and the fill, each a few awaits deep, run to completion.
        for _ in range(20):
            await asyncio.sleep(0)
        if self.until():
            stop.set()
        return stop.is_set()


class Source:
    """Answers with each listing in turn, then the last again. `cut` makes every later answer an outage."""

    def __init__(self, *answers: Listing) -> None:
        self.answers = list(answers)
        self.asked: list[float] = []
        self.time: Time | None = None

    async def listing(self) -> Listing:
        self.asked.append(self.time.now if self.time else 0.0)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def cut(self) -> None:
        self.answers = [Listing(Outcome.UNREACHABLE, detail="Reddit could not be reached: ClientConnectorError")]


class Downloader:
    """Writes a file named by the item, as `fetch.Download` does; fails the URLs it is told to."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.asked: list[str] = []

    async def download(self, url: str, destination: Path, max_bytes: int = MAX_FILE_BYTES) -> Download:
        self.asked.append(url)
        if url in self.failing:
            raise ConnectionError(url)
        extension = "mp4" if url.endswith(".mp4") else "jpg"
        path = destination.with_name(f"{destination.name}.{extension}")
        path.write_bytes(b"\x00" * 10)
        return Download(path=path, kind=Kind.CLIP if extension == "mp4" else Kind.STILL, extension=extension, size=10)


class Screen:
    """The renderer's interface: records what was put up and when, by the item's id."""

    def __init__(self, time: Time, durations: Mapping[str, float] | None = None) -> None:
        self.time = time
        self.durations = durations or {}
        self.shown: list[tuple[str, float]] = []
        self.refuse: set[str] = set()
        self.fail_after_load: set[str] = set()
        self._current: str | None = None
        self.closed = 0

    def load(self, path: Path) -> Loaded | NotShown:
        name = path.stem
        if name in self.refuse:
            self.shown.append((f"refused:{name}", self.time.now))
            return NotShown(path, "mpv could not play it")
        self.shown.append((name, self.time.now))
        self._current = name
        return Loaded(path, self.durations.get(name, 0.0))

    def check(self) -> NotShown | None:
        if self._current in self.fail_after_load:
            return NotShown(None, "Failed to commit atomic request")
        return None

    def close(self) -> None:
        self.closed += 1

    def ids(self) -> list[str]:
        return [name for name, _ in self.shown]


class Panel:
    """The label panel's interface: records each drawing."""

    def __init__(self) -> None:
        self.drawings: list[Drawing] = []

    async def draw(self, drawing: Drawing, *, label_id: str) -> None:
        self.drawings.append(drawing)


class Rig:
    def __init__(self, root: Path, source: Source, *, durations: Mapping[str, float] | None = None, failing=None) -> None:
        self.time = Time()
        source.time = self.time
        self.source = source
        self.downloader = Downloader(failing)
        self.media = root / "media"
        self.listing_path = root / "listing.json"
        self.cache = MediaCache(self.media, self.downloader)
        self.screen = Screen(self.time, durations)
        self.panel = Panel()
        self.status = SlideshowStatus()

    def slideshow(self, **overrides) -> Slideshow:
        return Slideshow(
            source=self.source,
            cache=self.cache,
            screen=self.screen,
            limits=LIMITS,
            captioner=self.panel,
            listing_path=self.listing_path,
            status=self.status,
            monotonic=self.time.monotonic,
            wait=self.time.wait,
            **overrides,
        )

    async def run_until(self, condition: Callable[[], bool], **overrides) -> None:
        self.time.until = condition
        await asyncio.wait_for(self.slideshow(**overrides).run(asyncio.Event()), timeout=10)

    async def run_for(self, shown: int, **overrides) -> None:
        await self.run_until(lambda: len(self.screen.shown) >= shown, **overrides)


# -- order and dwell -------------------------------------------------------------------


async def test_items_are_shown_in_listing_order_and_looped(tmp_path):
    rig = Rig(tmp_path, Source(listed(item("a"), item("b"), item("c"))))

    await rig.run_for(7)

    assert rig.screen.ids() == ["t3_a", "t3_b", "t3_c", "t3_a", "t3_b", "t3_c", "t3_a"]


async def test_each_item_stays_up_for_its_own_dwell(tmp_path):
    """A still for the minimum; an 18 s clip twice (36 s); a 2-minute clip cut at the maximum (60 s)."""
    items = (item("still"), item("short", url="https://v.redd.it/short.mp4"), item("long", url="https://v.redd.it/long.mp4"))
    rig = Rig(tmp_path, Source(listed(*items)), durations={"t3_short": 18.0, "t3_long": 120.0})

    await rig.run_for(4)

    times = [at for _, at in rig.screen.shown]
    assert [later - earlier for earlier, later in itertools.pairwise(times)] == [30.0, 36.0, 60.0]


async def test_what_is_up_is_checked_through_its_dwell_and_a_failure_ends_it_early(tmp_path, caplog):
    rig = Rig(tmp_path, Source(listed(item("a"), item("b"))))
    rig.screen.fail_after_load = {"t3_a"}

    with caplog.at_level(logging.WARNING):
        await rig.run_for(3)

    times = dict(rig.screen.shown[:2])
    assert times["t3_b"] - times["t3_a"] == CHECK_EVERY_S, "a screen failure did not end the item at the next check"
    assert any(r.event == "slideshow.not_shown" and r.item == "t3_a" for r in caplog.records)


async def test_an_item_not_shown_is_passed_over_after_a_pause_not_retried_in_a_loop(tmp_path, caplog):
    rig = Rig(tmp_path, Source(listed(item("bad"), item("good"))))
    rig.screen.refuse = {"t3_bad"}

    with caplog.at_level(logging.WARNING):
        await rig.run_for(4)

    assert rig.screen.ids() == ["refused:t3_bad", "t3_good", "refused:t3_bad", "t3_good"]
    times = [at for _, at in rig.screen.shown]
    assert times[1] - times[0] == NOT_SHOWN_PAUSE_S
    assert next(r.reason for r in caplog.records if getattr(r, "event", None) == "slideshow.not_shown") == "mpv could not play it"


async def test_only_cached_items_are_shown_and_one_that_will_not_download_is_skipped(tmp_path):
    rig = Rig(tmp_path, Source(listed(item("a"), item("gone"), item("c"))), failing={"https://i.redd.it/gone.jpg"})

    await rig.run_for(4)

    assert rig.screen.ids() == ["t3_a", "t3_c", "t3_a", "t3_c"]
    assert rig.status.failed == 1


async def test_nothing_cached_waits_without_showing_and_says_so_once(tmp_path, caplog):
    rig = Rig(tmp_path, Source(listed(item("a"))), failing={"https://i.redd.it/a.jpg"})

    with caplog.at_level(logging.INFO):
        await rig.run_until(lambda: rig.time.now > 1100)

    assert rig.screen.shown == []
    assert [r.event for r in caplog.records if getattr(r, "event", None) == "slideshow.nothing_ready"] == [
        "slideshow.nothing_ready"
    ]


# -- refreshes --------------------------------------------------------------------------


async def test_the_listing_is_read_again_every_refresh_interval(tmp_path):
    source = Source(listed(item("a")))
    rig = Rig(tmp_path, source)

    await rig.run_until(lambda: rig.time.now >= 1000 + 2 * REFRESH_S + 31)

    gaps = [later - earlier for earlier, later in itertools.pairwise(source.asked)]
    assert len(source.asked) == 3
    # Asked within one dwell check of being due, never early.
    assert all(REFRESH_S <= gap < REFRESH_S + LIMITS.minimum for gap in gaps), gaps


#: When a refresh due at `REFRESH_S` has been taken: it is started when due and
#: taken at the next check of the dwell.
TAKEN = 1000 + REFRESH_S + CHECK_EVERY_S


def split_at(shown: list[tuple[str, float]], instant: float) -> tuple[list[str], list[str]]:
    return [n for n, at in shown if at <= instant], [n for n, at in shown if at > instant]


async def test_a_refresh_that_reorders_continues_after_the_post_on_screen(tmp_path):
    new_order = ["t3_d", "t3_c", "t3_b", "t3_a"]
    rig = Rig(tmp_path, Source(listed(item("a"), item("b"), item("c"), item("d")), listed(*(item(n[3:]) for n in new_order))))

    await rig.run_until(lambda: rig.time.now >= TAKEN + 61)

    before, after = split_at(rig.screen.shown, TAKEN)
    on_screen = before[-1]
    assert after[0] == new_order[(new_order.index(on_screen) + 1) % 4]
    assert after[0] != ["t3_a", "t3_b", "t3_c", "t3_d"][(["t3_a", "t3_b", "t3_c", "t3_d"].index(on_screen) + 1) % 4]


async def test_a_refresh_that_drops_the_post_on_screen_starts_from_the_top_and_deletes_its_file(tmp_path):
    rig = Rig(tmp_path, Source(listed(item("a"), item("b")), listed(item("x"), item("y"))))

    await rig.run_until(lambda: rig.time.now >= TAKEN + 61)

    _, after = split_at(rig.screen.shown, TAKEN)
    assert after[:2] == ["t3_x", "t3_y"]
    assert sorted(path.stem for path in rig.media.iterdir()) == ["t3_x", "t3_y"]


async def test_a_failed_refresh_keeps_playing_and_asks_again_sooner(tmp_path, caplog):
    source = Source(listed(item("a"), item("b")), Listing(Outcome.UNREACHABLE, detail="Reddit could not be reached"))
    rig = Rig(tmp_path, source)

    with caplog.at_level(logging.WARNING):
        await rig.run_until(lambda: rig.time.now >= TAKEN + FAILED_REFRESH_S + 61)

    assert rig.status.outcome is Outcome.UNREACHABLE
    _, after = split_at(rig.screen.shown, TAKEN)
    assert set(after) == {"t3_a", "t3_b"}
    gaps = [later - earlier for earlier, later in itertools.pairwise(source.asked)]
    assert FAILED_REFRESH_S <= gaps[1] < FAILED_REFRESH_S + LIMITS.minimum
    assert sorted(path.stem for path in rig.media.iterdir()) == ["t3_a", "t3_b"], "an outage deleted cached media"
    assert any(getattr(r, "event", None) == "slideshow.refresh_failed" for r in caplog.records)


async def test_a_rate_limit_waits_as_long_as_reddit_asked(tmp_path):
    limited = Listing(Outcome.RATE_LIMITED, retry_after_s=500.0, detail="Reddit asked us to slow down")
    source = Source(listed(item("a")), limited, listed(item("a")))
    rig = Rig(tmp_path, source)

    await rig.run_until(lambda: len(source.asked) >= 3)

    assert source.asked[2] - source.asked[1] >= 500.0


async def test_a_listing_with_nothing_playable_neither_blanks_nor_deletes(tmp_path):
    rig = Rig(tmp_path, Source(listed(item("a")), listed(unresolved={"gfycat.com": 3})))

    await rig.run_until(lambda: rig.time.now >= 1000 + REFRESH_S + 61)

    assert rig.screen.ids()[-1] == "t3_a"
    assert [path.stem for path in rig.media.iterdir()] == ["t3_a"]


async def test_a_reddit_outage_mid_run_plays_on_from_the_cache(tmp_path):
    """Reddit stops answering after the first listing: every cached item goes on showing, in order, for half an hour."""
    source = Source(listed(item("a"), item("b"), item("c")), Listing(Outcome.UNREACHABLE, detail="down"))
    rig = Rig(tmp_path, source)

    await rig.run_until(lambda: rig.time.now >= 1000 + 3 * REFRESH_S)

    _, late = split_at(rig.screen.shown, TAKEN)
    assert late[:6] in (
        ["t3_a", "t3_b", "t3_c", "t3_a", "t3_b", "t3_c"],
        ["t3_b", "t3_c", "t3_a", "t3_b", "t3_c", "t3_a"],
        ["t3_c", "t3_a", "t3_b", "t3_c", "t3_a", "t3_b"],
    )
    assert len(source.asked) > 3, "the source was not asked again during the outage"


# -- across a restart -------------------------------------------------------------------


async def test_a_restart_while_reddit_is_down_plays_the_saved_listing(tmp_path):
    first = Rig(tmp_path, Source(listed(item("a"), item("b"))))
    await first.run_for(2)

    second = Rig(tmp_path, Source(Listing(Outcome.UNREACHABLE, detail="down")))
    await second.run_for(3)

    assert second.screen.ids() == ["t3_a", "t3_b", "t3_a"]
    assert second.status.outcome is Outcome.UNREACHABLE
    assert second.panel.drawings[0].label == label_for(item("a").caption)


def test_a_saved_listing_reads_back_as_it_was(tmp_path):
    items = (
        item("a", score=None),
        Item(
            "t3_b_1", "", Kind.CLIP, Caption("T", "r/x", "", 3), "host", link="https://i.imgur.com/x.gifv", domain="i.imgur.com"
        ),
    )
    save_listing(tmp_path / "l.json", items)

    assert load_listing(tmp_path / "l.json") == items


@pytest.mark.parametrize("content", ["not json", '{"version": 2, "items": []}', '{"version": 1, "items": [{"id": "x"}]}'])
def test_a_saved_listing_that_cannot_be_read_is_none_and_said(tmp_path, content, caplog):
    (tmp_path / "l.json").write_text(content)

    with caplog.at_level(logging.WARNING):
        assert load_listing(tmp_path / "l.json") == ()
    assert any(getattr(r, "event", None) == "slideshow.saved_unreadable" for r in caplog.records)


# -- the label --------------------------------------------------------------------------


def test_a_posts_caption_reads_author_title_subreddit_score_through_the_real_label():
    caption = Caption(title="A heron at dusk", community="r/birding", author="u/marsh_walker", score=12_345)

    text = read_label(label_for(caption))

    words = ["".join(run.text for run in candidate.runs) for candidate in text.candidates()]
    assert words == ["u/marsh_walker", "A heron at dusk", "r/birding", "↑ 12,345"]


def test_a_caption_with_no_author_or_score_leaves_them_off_rather_than_blank():
    text = read_label(label_for(Caption(title="Untitled", community="r/pics", author="", score=None)))

    assert ["".join(run.text for run in c.runs) for c in text.candidates()] == ["Untitled", "r/pics"]


async def test_each_post_is_captioned_once_and_a_gallerys_pictures_share_one_drawing(tmp_path):
    gallery = [
        Item(f"t3_g_{n}", f"https://i.redd.it/g{n}.jpg", Kind.STILL, Caption("G", "r/x", "u/y", 1), "reddit") for n in (1, 2)
    ]
    rig = Rig(tmp_path, Source(listed(item("a"), *gallery)))

    await rig.run_for(4)

    labels = [drawing.label for drawing in rig.panel.drawings]
    assert labels == [label_for(item("a").caption), label_for(gallery[0].caption), label_for(item("a").caption)]
    assert all(d.outcome is LabelOutcome.CAPTION for d in rig.panel.drawings)


async def test_a_caption_asked_for_while_the_panel_is_busy_is_drawn_while_its_post_is_still_up(tmp_path):
    """Not on the post's next turn round the loop: by then the label has named the wrong post for a whole cycle."""
    rig = Rig(tmp_path, Source(listed(item("a"), item("b"), item("c"))))
    release = asyncio.Event()
    drawn_during: list[tuple[str, str]] = []

    class SlowPanel(Panel):
        async def draw(self, drawing: Drawing, *, label_id: str) -> None:
            drawn_during.append((drawing.label["title"], rig.screen.ids()[-1]))
            if len(drawn_during) == 1:
                await release.wait()

    rig.panel = SlowPanel()

    def release_once_b_is_up() -> bool:
        if rig.screen.ids()[-1:] == ["t3_b"]:
            release.set()
        return len(rig.screen.shown) >= 3

    await rig.run_until(release_once_b_is_up)

    assert drawn_during[:2] == [("Title a", "t3_a"), ("Title b", "t3_b")]


async def test_a_fill_that_fails_unforeseen_ends_the_run_and_still_closes_the_screen(tmp_path):
    """Neither a fill that stops for good nor an mpv left running: the run fails, so systemd restarts it."""
    rig = Rig(tmp_path, Source(listed(item("a"))))

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("a defect in the fill")

    rig.cache.fill = broken  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="a defect in the fill"):
        await rig.run_until(lambda: rig.time.now > 1000 + 60)

    assert rig.screen.closed == 1


async def test_a_listing_read_that_fails_unforeseen_still_closes_the_screen(tmp_path):
    class Broken(Source):
        async def listing(self) -> Listing:
            raise RuntimeError("a defect in the source")

    rig = Rig(tmp_path, Broken(listed(item("a"))))

    with pytest.raises(RuntimeError, match="a defect in the source"):
        await rig.run_until(lambda: rig.time.now > 1000 + 60)

    assert rig.screen.closed == 1


# -- the status the page reads ---------------------------------------------------------


async def test_the_status_says_what_reddit_answered_and_what_is_up(tmp_path):
    rig = Rig(tmp_path, Source(listed(item("a"), unresolved={"gfycat.com": 2})))

    await rig.run_for(1)

    assert rig.status.outcome is Outcome.LISTED
    assert (rig.status.items, rig.status.unresolved, rig.status.downloaded) == (1, Counter({"gfycat.com": 2}), 1)
    assert rig.status.tiers == Counter({"reddit": 1})
    assert rig.status.showing == "Title a"


async def test_downloads_are_counted_by_tier_across_fills_since_the_start(tmp_path):
    rig = Rig(tmp_path, Source(listed(item("a")), listed(item("a"), item("b"))))

    await rig.run_until(lambda: rig.time.now >= TAKEN + 31)

    assert rig.status.downloaded == 2
    assert rig.status.tiers == Counter({"reddit": 2})


# -- the composition --------------------------------------------------------------------


class Reddit:
    """Reddit's listing and redgifs' API as `Fetcher.get_api` answers them, and media as `Fetcher.download` does."""

    def __init__(self) -> None:
        self.api: list[tuple[str, dict]] = []
        self.downloads: list[str] = []

    async def get_api(self, url: str, headers: Mapping[str, str] | None = None) -> ApiAnswer:
        self.api.append((url, dict(headers or {})))
        if url.startswith("https://www.reddit.com/"):
            return ApiAnswer(200, json.dumps(SHAPES).encode())
        if url == f"{API}/auth/temporary":
            claims = json.dumps(dict.fromkeys(AUTH["token_claims"], "x") | {"exp": 4_000_000_000}).encode()
            body = base64.urlsafe_b64encode(claims).decode().rstrip("=")
            return ApiAnswer(200, json.dumps({**AUTH["body"], "token": f"h.{body}.s"}).encode())
        return ApiAnswer(GIF["status"], json.dumps(GIF["body"]).encode())

    async def download(self, url: str, destination: Path, max_bytes: int = MAX_FILE_BYTES) -> Download:
        self.downloads.append(url)
        path = destination.with_name(f"{destination.name}.mp4")
        path.write_bytes(b"\x00" * 10)
        return Download(path=path, kind=Kind.CLIP, extension="mp4", size=10)


def slideshow_settings(cache_dir: Path) -> SlideshowSettings:
    return SlideshowSettings(
        cache_dir=cache_dir,
        listing_url="https://www.reddit.com/r/gifs/top/?t=month",
        reddit_session=COOKIE,
        limits=LIMITS,
        output="hdmi-a-1",
        panel=PanelSettings(1448, 1072, 180, None, None, None, ""),
    )


async def test_the_composition_wires_the_resolvers_to_the_source_and_the_cache(tmp_path):
    """An imgur .gifv with no copy on Reddit gets its mp4, and a redgifs post gets redgifs' HD.

    Wired to only the source, the imgur post would be listed with nothing to
    fetch; wired to only the cache, it would not be listed at all, and the
    redgifs post would come down as Reddit's copy.
    """
    reddit = Reddit()
    source, cache = entry.slideshow_parts(slideshow_settings(tmp_path), reddit)  # type: ignore[arg-type]

    listing = await source.listing()
    by_title = {i.caption.title: i for i in listing.items}
    gifv = by_title["A gifv on imgur without a copy"]
    redgifs = next(i for i in listing.items if i.domain == "redgifs.com")
    report = await cache.fill([gifv, redgifs])

    assert gifv.media_url == ""
    assert report.tiers == Counter({"host:imgur": 1, "host:redgifs": 1})
    assert f"https://i.imgur.com/{gifv.link.rsplit('/', 1)[1].split('.')[0]}.mp4" in reddit.downloads
    assert GIF["body"]["gif"]["urls"]["hd"] in reddit.downloads
    assert reddit.api[0][0] == listing_url("https://www.reddit.com/r/gifs/top/?t=month")


async def test_a_whole_slideshow_run_logs_no_cookie(tmp_path, caplog, monkeypatch):
    """The entry point's whole slideshow: listing, resolvers, fill, a show and the wind-down, then every record searched."""
    stop = asyncio.Event()
    shown: list[str] = []

    class Fetcher(Reddit):
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class StoppingScreen(Screen):
        def load(self, path: Path) -> Loaded | NotShown:
            shown.append(path.stem)
            # From the renderer's worker thread, so through the loop.
            loop.call_soon_threadsafe(stop.set)
            return super().load(path)

    async def no_page(*_args: object, **_kwargs: object) -> bool:
        await stop.wait()
        return True

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(entry, "run_settings_page", no_page)
    ui = UiSettings(cache_dir=tmp_path, bind="127.0.0.1", port=0, pinned=frozenset())
    with caplog.at_level(logging.DEBUG):
        await asyncio.wait_for(
            entry.run_slideshow(
                slideshow_settings(tmp_path),
                ui,
                stop,
                fetcher=Fetcher(),
                screen=lambda _: StoppingScreen(Time()),
                problems=list,
            ),
            timeout=20,
        )

    assert shown, "nothing was shown, so the run proves little"
    events = {getattr(r, "event", None) for r in caplog.records}
    assert {"slideshow.starting", "slideshow.listed", "slideshow.filled", "slideshow.shown"} <= events
    secret = COOKIE.rsplit(".", 1)[1]
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert all(secret not in repr(value) for value in record.__dict__.values())


async def test_what_would_stop_mpv_is_said_once_at_start_and_the_slideshow_plays_on(tmp_path, caplog, monkeypatch):
    stop = asyncio.Event()
    statuses: list[SlideshowStatus] = []

    class Fetcher(Reddit):
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class StoppingScreen(Screen):
        def load(self, path: Path) -> Loaded | NotShown:
            loop.call_soon_threadsafe(stop.set)
            return super().load(path)

    async def the_page(state, _stop) -> bool:
        statuses.append(state.slideshow)
        await stop.wait()
        return True

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(entry, "run_settings_page", the_page)
    ui = UiSettings(cache_dir=tmp_path, bind="127.0.0.1", port=0, pinned=frozenset())
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(
            entry.run_slideshow(
                slideshow_settings(tmp_path),
                ui,
                stop,
                fetcher=Fetcher(),
                screen=lambda _: StoppingScreen(Time()),
                problems=lambda: ["mpv is not installed"],
            ),
            timeout=20,
        )

    said = [r for r in caplog.records if getattr(r, "event", None) == "slideshow.renderer_unavailable"]
    assert [r.reason for r in said] == ["mpv is not installed"]
    assert statuses[0].renderer_problems == ("mpv is not installed",)
    assert any(getattr(r, "event", None) == "slideshow.shown" for r in caplog.records), "it stopped rather than playing on"


# -- the screen's brightness -------------------------------------------------------------


class Brightness:
    """The brightness's interface: records each `apply`, and when it was closed."""

    def __init__(self, time: Time) -> None:
        self.time = time
        self.applied: list[tuple[float, bool, bool]] = []
        self.closed = 0

    async def apply(self, *, present: bool, showing: bool) -> None:
        self.applied.append((self.time.now, present, showing))

    def close(self) -> None:
        self.closed += 1


async def test_the_brightness_is_followed_while_an_item_is_up_and_closed_at_the_end(tmp_path):
    rig = Rig(tmp_path, Source(listed(item("a"), item("b"))))
    brightness = Brightness(rig.time)

    await rig.run_for(3, brightness=brightness)

    first_shown = rig.screen.shown[0][1]
    assert brightness.applied[0] == (first_shown, True, True), "not applied as the first item went up"
    # And through the dwell, so the sun is followed on a screen that shows one long clip.
    assert len([a for a in brightness.applied if first_shown < a[0] < rig.screen.shown[1][1]]) >= 2
    assert all(showing for _, _, showing in brightness.applied)
    assert brightness.closed == 1


async def test_a_slow_brightness_exchange_takes_from_the_dwell_never_adds_to_it(tmp_path):
    """An 18 s clip plays twice in a 36 s dwell; a DDC/CI exchange that took time must not let it start a third play."""
    rig = Rig(tmp_path, Source(listed(item("clip", url="https://v.redd.it/clip.mp4"), item("b"))), durations={"t3_clip": 18.0})

    class Slow(Brightness):
        async def apply(self, *, present: bool, showing: bool) -> None:
            await super().apply(present=present, showing=showing)
            self.time.now += 0.7

    await rig.run_for(2, brightness=Slow(rig.time))

    [(_, clip_at), (_, next_at)] = rig.screen.shown
    assert next_at - clip_at == pytest.approx(36.0)


async def test_nothing_shown_means_no_brightness_asked_for(tmp_path):
    """A brightness write must never be what wakes a screen with nothing on it."""
    rig = Rig(tmp_path, Source(listed(item("a"))), failing={"https://i.redd.it/a.jpg"})
    brightness = Brightness(rig.time)

    await rig.run_until(lambda: rig.time.now > 1100, brightness=brightness)

    assert rig.screen.shown == []
    assert brightness.applied == []
    assert brightness.closed == 1


async def test_the_brightness_is_closed_even_when_the_screen_fails_to_close(tmp_path):
    rig = Rig(tmp_path, Source(listed(item("a"))))
    brightness = Brightness(rig.time)

    def broken_close() -> None:
        raise RuntimeError("mpv would not stop")

    rig.screen.close = broken_close  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="mpv would not stop"):
        await rig.run_for(1, brightness=brightness)

    assert brightness.closed == 1


async def test_a_slideshow_run_sets_its_own_screens_brightness_from_its_settings(tmp_path, monkeypatch, clock):
    """Through `run_slideshow`, with a connector and levels no default has, so an unwired setting cannot pass.

    The clock is at noon on the equator at the prime meridian, so the curve's value is its day level.
    """
    stop = asyncio.Event()
    opened: list[tuple[str, FakeMonitor]] = []

    def bus(output: str) -> FakeMonitor:
        monitor = FakeMonitor(current=10, maximum=100)
        opened.append((output, monitor))
        return monitor

    class Fetcher(Reddit):
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class StoppingScreen(Screen):
        def check(self) -> NotShown | None:
            # Past the first item's going up, when the brightness is first applied.
            loop.call_soon_threadsafe(stop.set)
            return super().check()

    async def no_page(*_args: object, **_kwargs: object) -> bool:
        await stop.wait()
        return True

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(entry, "run_settings_page", no_page)
    monkeypatch.setattr(entry, "screen_ddc", bus)
    monkeypatch.setattr("arrt_player.programmes.slideshow.CHECK_EVERY_S", 0.01)
    settings = replace(
        slideshow_settings(tmp_path),
        output="hdmi-a-2",
        screen_brightness=ScreenBrightnessSettings(location=EQUATOR, minimum=17, maximum=23, interval_seconds=91.0),
    )
    ui = UiSettings(cache_dir=tmp_path, bind="127.0.0.1", port=0, pinned=frozenset())
    await asyncio.wait_for(
        entry.run_slideshow(
            settings,
            ui,
            stop,
            fetcher=Fetcher(),
            screen=lambda _: StoppingScreen(Time()),
            problems=list,
            clock=clock.as_clock(),
        ),
        timeout=20,
    )

    [(output, monitor)] = opened
    assert output == "hdmi-a-2"
    assert monitor.writes == [23]
    assert monitor.closed == 1


def test_a_slideshow_with_no_location_or_the_switch_off_has_no_brightness(tmp_path, clock):
    no_location = slideshow_settings(tmp_path)
    switched_off = replace(no_location, screen_brightness=ScreenBrightnessSettings(location=EQUATOR, enabled=False))
    located = replace(no_location, screen_brightness=ScreenBrightnessSettings(location=EQUATOR))

    assert entry.slideshow_brightness(no_location, clock.as_clock()) is None
    assert entry.slideshow_brightness(switched_off, clock.as_clock()) is None
    assert entry.slideshow_brightness(located, clock.as_clock()) is not None


# -- the renderer's life ----------------------------------------------------------------


@pytest.fixture
def short_root() -> Iterator[Path]:
    """Short, as `test_mpv.root` is: a Unix socket path must fit in 104 bytes on macOS."""
    path = Path(tempfile.mkdtemp(prefix="show-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def test_a_cancelled_slideshow_leaves_no_mpv(short_root, monkeypatch):
    bench = Bench(short_root, monkeypatch)
    (bench.media / "t3_a.mp4").write_text("18.0")
    cache = MediaCache(bench.media, Downloader())
    source = Source(listed(item("a", url="https://v.redd.it/a.mp4")))
    show = Slideshow(source=source, cache=cache, screen=bench.output, limits=LIMITS)

    task = asyncio.create_task(show.run(asyncio.Event()))
    assert await asyncio.to_thread(wait_until, lambda: bool(bench.starts()), 10.0), "mpv never started"
    pid = bench.pid()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(wait_until, lambda: not alive(pid), 5.0)
    assert not (bench.runtime / "mpv.pid").exists()
