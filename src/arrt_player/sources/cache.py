"""The slideshow's media on disk: fetched ahead of the play position, named by item id, and kept only while listed.

**The renderer plays only what is here.** mpv is handed a path under this
cache's root and never a URL (`mpv.py`), so a slideshow plays on from its cache
when Reddit stops answering, and nothing on the screen waits on the network
unless nothing is cached yet.

**A file exists under its final name only when whole.** `fetch.Download` writes
beside it and moves it into place, and anything left mid-download by a process
that died is cleared when the cache opens.

**What is kept is the current listing, up to a cap.** Eviction deletes every
file whose item is no longer listed. Downloads stop when the files here reach
`cap_bytes`, and a download is never allowed to take the total past it, so the
cap is a ceiling rather than a trigger. The files counted are every regular file
under the root, whatever made them.

**One item that will not download never stops the rest.** A refused or broken
link is remembered and not tried again for `retry_after_s`; the fill moves on.
A run of items that cannot be reached at all means the network is down, and the
fill stops until the next one.
"""

import logging
import os
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Final, Protocol

from arrt_player.sources import ITEM_ID, Candidate, Item, Kind
from arrt_player.sources.fetch import MAX_FILE_BYTES, PARTIAL_SUFFIX, UNREACHABLE, Download, FetchRefused, TooLarge

log = logging.getLogger(__name__)

#: The owner's cap for the slideshow's media (2026-11-10).
CAP_BYTES: Final[int] = 2 * 1024**3

#: How long an item that failed to download is left before it is tried again.
RETRY_AFTER_S: Final[float] = 30 * 60

#: Items in a row that could not be reached before a fill decides the network is down.
UNREACHABLE_RUN: Final[int] = 3

#: How a file's extension says what to show it as. `fetch.sniff` chose the
#: extension from the file's bytes.
KIND_BY_EXTENSION: Final[dict[str, Kind]] = {
    "jpg": Kind.STILL,
    "png": Kind.STILL,
    "webp": Kind.STILL,
    "avif": Kind.STILL,
    "gif": Kind.CLIP,
    "mp4": Kind.CLIP,
    "webm": Kind.CLIP,
}


class _Step(Enum):
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    UNREACHABLE = "unreachable"
    CAP = "cap"


async def _reddit_only(item: Item) -> list[Candidate]:
    """With no resolvers, an item's media is where the listing said."""
    return [Candidate(item.media_url, item.tier)] if item.media_url else []


class Downloader(Protocol):
    async def download(self, url: str, destination: Path, max_bytes: int = MAX_FILE_BYTES) -> Download: ...


@dataclass(frozen=True)
class Cached:
    """A whole file ready to show."""

    path: Path
    kind: Kind


@dataclass
class FillReport:
    """What one fill did, for the journal and the page."""

    downloaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    #: Downloads by the tier that gave them, so a resolver that always falls back shows.
    tiers: Counter[str] = field(default_factory=Counter)
    cap_reached: bool = False
    network_down: bool = False


def play_order(items: Sequence[Item], start: int) -> Iterator[Item]:
    """The items from `start` to the end, then from the top: the order they will be shown in."""
    if not items:
        return
    start %= len(items)
    yield from items[start:]
    yield from items[:start]


def evictions(on_disk: dict[str, Path], keep: set[str]) -> list[Path]:
    """The files whose items are not in `keep`."""
    return sorted(path for item_id, path in on_disk.items() if item_id not in keep)


class MediaCache:
    """The files under `root`. Opening it clears partial downloads and reads what is already whole."""

    def __init__(
        self,
        root: Path,
        downloader: Downloader,
        cap_bytes: int = CAP_BYTES,
        max_file_bytes: int = MAX_FILE_BYTES,
        clock: Callable[[], float] = time.monotonic,
        candidates: Callable[[Item], Awaitable[list[Candidate]]] | None = None,
    ) -> None:
        self.root = root
        self._downloader = downloader
        self._candidates = candidates or _reddit_only
        self.cap_bytes = cap_bytes
        self._max_file_bytes = max_file_bytes
        self._clock = clock
        self._failed_at: dict[str, float] = {}
        root.mkdir(parents=True, exist_ok=True)
        for partial in root.glob(f"*{PARTIAL_SUFFIX}"):
            partial.unlink()
            log.info("slideshow.cache.partial_cleared file=%s", partial.name)
        self._files = self._scan()

    def get(self, item_id: str) -> Cached | None:
        path = self._files.get(item_id)
        if path is None or not path.is_file():
            return None
        return Cached(path, KIND_BY_EXTENSION[path.suffix[1:]])

    def used_bytes(self) -> int:
        """The bytes of every regular file under the root."""
        return sum(entry.stat().st_size for entry in os.scandir(self.root) if entry.is_file(follow_symlinks=False))

    def evict(self, keep: set[str]) -> list[Path]:
        """Delete every file whose item is not in `keep`, and forget failures for those items."""
        removed = evictions(self._files, keep)
        for path in removed:
            path.unlink(missing_ok=True)
            del self._files[path.stem]
        self._failed_at = {item_id: at for item_id, at in self._failed_at.items() if item_id in keep}
        if removed:
            log.info("slideshow.cache.evicted count=%d", len(removed))
        return removed

    async def fill(self, items: Sequence[Item], start: int = 0) -> FillReport:
        """Download what is not here yet, in play order from `start`, until the cap or the end of the listing."""
        report = FillReport()
        unreachable_run: list[str] = []
        for item in play_order(items, start):
            if item.id in self._files or self._waiting(item.id):
                continue
            room = self.cap_bytes - self.used_bytes()
            step = _Step.CAP if room <= 0 else await self._fetch_one(item, min(self._max_file_bytes, room), report)
            if step is _Step.CAP:
                report.cap_reached = True
                break
            if step is not _Step.UNREACHABLE:
                # Any answer, even a refusal, shows the network is up.
                unreachable_run.clear()
                continue
            unreachable_run.append(item.id)
            if len(unreachable_run) >= UNREACHABLE_RUN:
                # The network, not these items: they are tried again on the
                # next fill rather than held back as broken links.
                for item_id in unreachable_run:
                    del self._failed_at[item_id]
                report.network_down = True
                break
        if report.cap_reached:
            log.warning("slideshow.cache.cap_reached used=%d cap=%d", self.used_bytes(), self.cap_bytes)
        if report.network_down:
            log.warning("slideshow.cache.network_down after=%d", UNREACHABLE_RUN)
        return report

    async def _fetch_one(self, item: Item, limit: int, report: FillReport) -> "_Step":
        """Try each place the item's media may be, best first, and keep the first that downloads.

        The item counts as unreachable only when every place was: one host that
        answers, even with a refusal, shows the network is up.
        """
        candidates = await self._candidates(item)
        reasons: list[str] = []
        all_unreachable = bool(candidates)
        for candidate in candidates:
            try:
                got = await self._downloader.download(candidate.url, self.root / item.id, max_bytes=limit)
            except TooLarge as error:
                if limit < self._max_file_bytes:
                    # Too large for the room left, not for a file: the cap stops the fill.
                    return _Step.CAP
                reasons.append(f"{candidate.tier}: {error}")
                all_unreachable = False
            except FetchRefused as error:
                reasons.append(f"{candidate.tier}: {error}")
                all_unreachable = False
            except UNREACHABLE as error:
                reasons.append(f"{candidate.tier}: unreachable: {type(error).__name__}")
            else:
                self._files[item.id] = got.path
                report.downloaded.append(item.id)
                report.tiers[candidate.tier] += 1
                if reasons:
                    log.warning("slideshow.cache.fell_back item=%s to=%s after=%s", item.id, candidate.tier, "; ".join(reasons))
                log.info("slideshow.cache.downloaded item=%s tier=%s bytes=%d", item.id, candidate.tier, got.size)
                return _Step.DOWNLOADED
        self._fail(item, report, "; ".join(reasons) or "nothing to fetch")
        return _Step.UNREACHABLE if all_unreachable else _Step.FAILED

    def _fail(self, item: Item, report: FillReport, reason: str) -> None:
        self._failed_at[item.id] = self._clock()
        report.failed[item.id] = reason
        log.warning("slideshow.cache.download_failed item=%s tier=%s reason=%s", item.id, item.tier, reason)

    def _waiting(self, item_id: str) -> bool:
        failed = self._failed_at.get(item_id)
        return failed is not None and self._clock() - failed < RETRY_AFTER_S

    def _scan(self) -> dict[str, Path]:
        """Whole files already here, by item id. A name this cache would not have written is left alone."""
        found: dict[str, Path] = {}
        for path in self.root.iterdir():
            if path.is_file() and path.suffix[1:] in KIND_BY_EXTENSION and ITEM_ID.fullmatch(path.stem):
                found[path.stem] = path
        return found
