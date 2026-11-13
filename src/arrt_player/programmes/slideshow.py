"""The slideshow programme: a source's listing, played from the media cache through the renderer, captioned on the label.

**Three things run, and none waits on another.** The listing is read on its own
task, every `REFRESH_S`, and taken between items or during a dwell. The cache is
filled on another task, from the play position onwards, whenever a listing is
taken or nothing is ready to show. Playback runs here and shows only what is
already cached, so the screen never waits on the network unless nothing is
cached yet.

**Reddit answering anything but a listing never blanks the screen.** The last
listing goes on playing from the cache. It is also kept on disk
(`save_listing`), so a restart while Reddit is down, which every save on the
settings page causes, still has items and captions to play.

**After a refresh, play continues from the post after the one on screen** when
that post is still listed, and from the top when it is not (the build plan's
Decisions). Files of posts no longer listed are deleted at once.

**An item that is not shown is passed over, not retried at once.** A failure is
logged by name, and the next item waits `NOT_SHOWN_PAUSE_S`, so a screen that
will not draw cannot make this loop spin. The renderer has its own restart
backoff for the screen.

**The label says the post, not the work.** A post's caption is set through the
same `LabelText` and layout as a work's label (`label_for` says which field
carries what), so it gets the same type scale and floors.

**The screen's brightness follows the sun while an item is up**, as an Arrt-mode
screen's does (`displays/screen_brightness.py`), and is never written while
nothing is shown, so a brightness write cannot be what wakes the screen. mpv
reports no hot-plug, so the screen is taken to be present throughout. A
monitor swapped while the Player runs is not probed again until it restarts.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Final, Protocol

from arrt_player.dwell import DwellLimits, dwell_seconds
from arrt_player.episodes import ReportOnce
from arrt_player.label_rule import Drawing
from arrt_player.label_rule import Outcome as LabelOutcome
from arrt_player.mpv import Loaded, NotShown
from arrt_player.programmes.slideshow_status import SlideshowStatus
from arrt_player.sources import Caption, Item, Kind, Listing, Outcome, Source
from arrt_player.sources.cache import FillReport, MediaCache

log = logging.getLogger(__name__)

#: How often the listing is read again (*Inferred* in the build plan: one page of
#: up to 100 posts every 10 minutes, well inside Reddit's rate limit).
REFRESH_S: Final[float] = 600.0

#: How soon a listing that did not come is asked for again, unless Reddit asked
#: for longer. Sooner than a refresh, so a Player started while Reddit was down
#: begins playing soon after it is back; not so soon that a refused cookie costs
#: a request a minute.
FAILED_REFRESH_S: Final[float] = 120.0

#: The wait before the next item after one was not shown. The dwell floor, so a
#: run of failures is no faster than a run of stills.
NOT_SHOWN_PAUSE_S: Final[float] = 5.0

#: How often what is up is checked during its dwell. mpv's failure lines arrive
#: over its socket as they happen; this is how soon one ends the item.
CHECK_EVERY_S: Final[float] = 1.0

#: The wait while nothing is cached yet, between looks.
NOTHING_READY_POLL_S: Final[float] = 2.0

#: The label renderer's name for the slideshow's label, in its journal lines.
LABEL_ID: Final[str] = "slideshow"

#: The saved listing's layout.
LISTING_VERSION: Final[int] = 1


class Screen(Protocol):
    """What the slideshow draws through: `mpv.MpvOutput`. Blocking; called on a worker thread."""

    def load(self, path: Path) -> Loaded | NotShown: ...

    def check(self) -> NotShown | None: ...

    def close(self) -> None: ...


class Brightness(Protocol):
    """The screen's brightness: `displays.screen_brightness.ScreenBrightness`. Never raises on a monitor's fault."""

    async def apply(self, *, present: bool, showing: bool) -> None: ...

    def close(self) -> None: ...


class Captioner(Protocol):
    """What the slideshow captions on: `label_renderer.LabelPanel`. Never raises."""

    async def draw(self, drawing: Drawing, *, label_id: str) -> None: ...


Wait = Callable[[asyncio.Event, float], Awaitable[bool]]


async def wait_for_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Wait `seconds` or until `stop` is set; True when it was set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=max(seconds, 0.0))
    return stop.is_set()


def label_for(caption: Caption) -> dict[str, str]:
    """A post's caption as label fields, read by `panel.read_label`.

    The label's fields are named for a work of art, and a post is set into them
    by reading order (`LabelText.candidates`): the author leads where the artist
    does, the title follows in italic, then the subreddit and the score each on a
    line of their own, as the further facts that a small surface gives up first.
    """
    fields = {"title": caption.title, "artist": caption.author, "date_created": caption.community}
    if caption.score is not None:
        fields["medium"] = f"↑ {caption.score:,}"
    return {name: value for name, value in fields.items() if value}


def save_listing(path: Path, items: Sequence[Item]) -> None:
    """Keep a listing on disk, whole or not at all. Raises `OSError`."""
    document = {
        "version": LISTING_VERSION,
        "items": [
            {
                "id": item.id,
                "media_url": item.media_url,
                "kind": item.kind.value,
                "tier": item.tier,
                "link": item.link,
                "domain": item.domain,
                "title": item.caption.title,
                "community": item.caption.community,
                "author": item.caption.author,
                "score": item.caption.score,
            }
            for item in items
        ],
    }
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(document, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def load_listing(path: Path) -> tuple[Item, ...]:
    """The listing `save_listing` kept, or none when there is no file or it cannot be read, which is logged."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return _saved_items(document)
    except FileNotFoundError:
        return ()
    except (OSError, ValueError, KeyError, TypeError) as error:
        log.warning(
            "the saved listing cannot be read (%s); starting without it",
            error,
            extra={"event": "slideshow.saved_unreadable"},
        )
        return ()


def _saved_items(document: dict[str, Any]) -> tuple[Item, ...]:
    """The items of a saved listing. Raises `ValueError`, `KeyError` or `TypeError` for one this does not read."""
    if document["version"] != LISTING_VERSION:
        raise ValueError(f"layout version {document['version']!r}")
    return tuple(
        Item(
            id=entry["id"],
            media_url=entry["media_url"],
            kind=Kind(entry["kind"]),
            tier=entry["tier"],
            link=entry["link"],
            domain=entry["domain"],
            caption=Caption(title=entry["title"], community=entry["community"], author=entry["author"], score=entry["score"]),
        )
        for entry in document["items"]
    )


class Slideshow:
    """One listing on one screen, until stopped."""

    def __init__(
        self,
        *,
        source: Source,
        cache: MediaCache,
        screen: Screen,
        limits: DwellLimits,
        captioner: Captioner | None = None,
        brightness: Brightness | None = None,
        listing_path: Path | None = None,
        status: SlideshowStatus | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
        wait: Wait = wait_for_stop,
        refresh_s: float = REFRESH_S,
    ) -> None:
        self._source = source
        self._cache = cache
        self._screen = screen
        self._limits = limits
        self._captioner = captioner
        self._brightness = brightness
        self._listing_path = listing_path
        self.status = status if status is not None else SlideshowStatus()
        self._monotonic = monotonic
        self._now = now
        self._wait = wait
        self._refresh_s = refresh_s
        self._items: tuple[Item, ...] = ()
        #: Where in `_items` the next item is looked for.
        self._position = 0
        #: The item on the screen, or None.
        self._current: Item | None = None
        self._refresh_at = 0.0
        self._refreshing: asyncio.Task[Listing] | None = None
        self._fill_wanted = asyncio.Event()
        #: The caption the item on the screen wants, and the last one handed to the panel.
        self._wanted: Drawing | None = None
        self._drawn: Drawing | None = None
        self._drawing: asyncio.Task[None] | None = None
        self._nothing_ready = ReportOnce()

    async def run(self, stop: asyncio.Event) -> None:
        """Play until `stop`. Every way out, a cancellation included, stops the renderer and its mpv."""
        if self._listing_path is not None:
            self._items = load_listing(self._listing_path)
            self.status.items = len(self._items)
            self.status.from_saved = bool(self._items)
            if self._items:
                log.info(
                    "playing the %d items saved from the last listing until Reddit answers",
                    len(self._items),
                    extra={"event": "slideshow.saved_listing", "items": len(self._items)},
                )
        self._refresh_at = self._monotonic()
        filling = asyncio.create_task(self._filling(), name="slideshow-fill")
        self._fill_wanted.set()
        try:
            while not stop.is_set():
                if filling.done():
                    # The fill never ends on its own. One that did raised
                    # something unforeseen: ended here, the run fails and
                    # systemd restarts it, rather than the cache quietly
                    # never filling again.
                    filling.result()
                self._take_a_finished_refresh()
                await self._show_next(stop)
        finally:
            await self._wind_down(filling)

    async def _wind_down(self, filling: "asyncio.Task[None]") -> None:
        """Stop the tasks and close the screen. The screen is closed whatever the tasks ended with."""
        try:
            tasks = [task for task in (filling, self._refreshing) if task is not None]
            for task in tasks:
                task.cancel()
            # Gathered as results, so a task that already failed cannot raise
            # here and skip the close below; its error is the run's, raised by
            # whoever saw it first.
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._drawing is not None and not self._drawing.done():
                # The panel's own budget bounds this; cancelling would leave its draw gate open.
                with contextlib.suppress(asyncio.CancelledError):
                    await self._drawing
        finally:
            try:
                # On a thread: a load in flight holds the renderer's lock until it returns.
                await asyncio.to_thread(self._screen.close)
            finally:
                if self._brightness is not None:
                    # On a thread: closing waits for a DDC/CI exchange in flight.
                    await asyncio.to_thread(self._brightness.close)

    # -- the listing -------------------------------------------------------------------------

    def _take_a_finished_refresh(self) -> None:
        """Take a listing that has arrived, and start reading the next when one is due."""
        if self._refreshing is not None and self._refreshing.done():
            listing = self._refreshing.result()
            self._refreshing = None
            self._take(listing)
        if self._refreshing is None and self._monotonic() >= self._refresh_at:
            self._refreshing = asyncio.create_task(self._source.listing(), name="slideshow-listing")

    def _take(self, listing: Listing) -> None:
        status = self.status
        status.outcome, status.detail, status.answered_at = listing.outcome, listing.detail, self._now()
        if listing.outcome is not Outcome.LISTED:
            wait = max(FAILED_REFRESH_S, listing.retry_after_s or 0.0)
            self._refresh_at = self._monotonic() + wait
            log.warning(
                "Reddit gave no listing (%s); playing on from the cache and asking again in %.0f s",
                listing.detail or listing.outcome.value,
                wait,
                extra={"event": "slideshow.refresh_failed", "outcome": listing.outcome.value, "items": len(self._items)},
            )
            return
        self._refresh_at = self._monotonic() + self._refresh_s
        status.unresolved = listing.unresolved
        if not listing.items:
            # *Inferred*: an answer with nothing playable in it is not taken as an
            # instruction to delete everything and blank the screen.
            log.warning(
                "the listing had nothing that can be shown; playing on from the cache",
                extra={"event": "slideshow.listed_nothing", "unresolved": dict(listing.unresolved)},
            )
            return
        self._items = listing.items
        status.items, status.from_saved = len(self._items), False
        self._position = self._index_after(self._current)
        removed = self._cache.evict({item.id for item in self._items})
        if self._listing_path is not None:
            try:
                save_listing(self._listing_path, self._items)
            except OSError as error:
                log.warning("the listing could not be saved: %s", error, extra={"event": "slideshow.save_failed"})
        log.info(
            "took a listing of %d items; %d files of posts no longer listed were deleted",
            len(self._items),
            len(removed),
            extra={
                "event": "slideshow.listed",
                "items": len(self._items),
                "evicted": len(removed),
                "unresolved": dict(listing.unresolved),
            },
        )
        self._fill_wanted.set()

    def _index_after(self, current: Item | None) -> int:
        """The position after `current` in the listing, or the top when it is not listed."""
        if current is not None:
            for index, item in enumerate(self._items):
                if item.id == current.id:
                    return index + 1
        return 0

    # -- the cache ---------------------------------------------------------------------------

    async def _filling(self) -> None:
        while True:
            await self._fill_wanted.wait()
            self._fill_wanted.clear()
            if not self._items:
                continue
            report = await self._cache.fill(self._items, start=self._position)
            self._report_fill(report)

    def _report_fill(self, report: FillReport) -> None:
        status = self.status
        status.downloaded += len(report.downloaded)
        status.tiers += report.tiers
        status.failed = len(report.failed)
        status.cap_reached, status.network_down = report.cap_reached, report.network_down
        if report.downloaded or report.failed:
            log.info(
                "the cache took %d files and failed %d",
                len(report.downloaded),
                len(report.failed),
                extra={
                    "event": "slideshow.filled",
                    "downloaded": len(report.downloaded),
                    "failed": len(report.failed),
                    "tiers": dict(report.tiers),
                    "cap_reached": report.cap_reached,
                    "network_down": report.network_down,
                },
            )

    # -- the screen --------------------------------------------------------------------------

    def _next_ready(self) -> tuple[Item, Path] | None:
        """The next cached item in play order, moving the position past it; None when nothing is cached."""
        count = len(self._items)
        for step in range(count):
            index = (self._position + step) % count
            cached = self._cache.get(self._items[index].id)
            if cached is not None:
                self._position = index + 1
                return self._items[index], cached.path
        return None

    async def _show_next(self, stop: asyncio.Event) -> None:
        ready = self._next_ready()
        if ready is None:
            if self._nothing_ready.begin():
                log.info(
                    "nothing is cached to show yet (%d items listed)",
                    len(self._items),
                    extra={"event": "slideshow.nothing_ready", "items": len(self._items)},
                )
            self._fill_wanted.set()
            await self._wait(stop, NOTHING_READY_POLL_S)
            return
        self._nothing_ready.end()
        item, path = ready
        result = await asyncio.to_thread(self._screen.load, path)
        if isinstance(result, NotShown):
            self._not_shown(item, result.reason)
            await self._wait(stop, NOT_SHOWN_PAUSE_S)
            return
        self._current = item
        self.status.showing = item.caption.title
        dwell = dwell_seconds(result.duration, self._limits)
        log.info(
            "showing %s for %.1f s",
            item.id,
            dwell,
            extra={
                "event": "slideshow.shown",
                "item": item.id,
                "tier": item.tier,
                "duration": result.duration,
                "dwell": dwell,
            },
        )
        self._caption(item)
        await self._follow_the_sun()
        await self._dwell(stop, item, dwell)

    async def _dwell(self, stop: asyncio.Event, item: Item, seconds: float) -> None:
        """Hold the item for `seconds`, ending early on `stop` or when the screen says it is no longer up."""
        end = self._monotonic() + seconds
        while (left := end - self._monotonic()) > 0:
            if await self._wait(stop, min(CHECK_EVERY_S, left)):
                return
            self._take_a_finished_refresh()
            self._draw_wanted()
            failed = await asyncio.to_thread(self._screen.check)
            if failed is not None:
                self._not_shown(item, failed.reason)
                return
            await self._follow_the_sun()

    async def _follow_the_sun(self) -> None:
        """Brightness while an item is up. The brightness keeps its own interval, so this is called freely."""
        if self._brightness is not None:
            await self._brightness.apply(present=True, showing=self._current is not None)

    def _not_shown(self, item: Item, reason: str) -> None:
        self._current = None
        self.status.showing = None
        log.warning(
            "%s was not shown: %s",
            item.id,
            reason,
            extra={"event": "slideshow.not_shown", "item": item.id, "reason": reason},
        )

    # -- the label ---------------------------------------------------------------------------

    def _caption(self, item: Item) -> None:
        """Ask for the post's caption on the panel."""
        self._wanted = Drawing(LabelOutcome.CAPTION, label=label_for(item.caption), work_id=item.id, state=LABEL_ID)
        self._draw_wanted()

    def _draw_wanted(self) -> None:
        """Hand the wanted caption to the panel, unless it is already there (one gallery's pictures share one).

        While the panel is still drawing the last one this waits, and the dwell
        asks again each check: the panel would refuse a second draw, and the
        caption that matters is the one for what is on the screen.
        """
        if self._captioner is None or self._wanted is None or self._wanted == self._drawn:
            return
        if self._drawing is not None and not self._drawing.done():
            return
        self._drawn = self._wanted
        self._drawing = asyncio.create_task(self._captioner.draw(self._wanted, label_id=LABEL_ID), name="slideshow-label")
