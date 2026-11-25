"""Pull each wall's manifest and media into its cache, report its heartbeat, and ask the server which walls this client drives.

A wall's feed (manifest major 2) names each work's presentation master and is
cached whole (`_adopt_feed` says why).

**The only module that speaks HTTP to the Arrt server**, and
`tests/test_plane_isolation.py` holds it to that. (The slideshow, which has no
server, has a client of its own in `sources/fetch.py`, bounded there.) It spells five
routes — the client document and the client heartbeat, a wall's manifest at
each major it asks for, a wall's heartbeat, and a label's document — as
`contract/routes.json` spells them. Masters are fetched
from the address each work's `media.url` gives, resolved against the
manifest's own URL: today the same server's media route, after a
Library/Programming split perhaps another host. The client's token is sent to
the server's own origin only.

**The cache is the only thing a wall renders from.** A worker's watcher reads
`CACHE_DIR/<wall id>/manifest.json`, and this module writes that file once
every master it names has been fetched and checked against its hash, or
answered for, or once a master that keeps failing has been waited for as long
as `MEDIA_HOLD_SECONDS` (`_adopt_feed`). So a server that goes away changes nothing about
what is on the wall: the last good manifest stays, and so do its masters. The client document is kept the same way,
so a client restarted while the server is down still knows its walls, and so is each label document, so a
label restarted then still captions for what is left of the rule's hold (`LabelPull.cached`).

**Every failure keeps the cache** (`player-contract.md` § Transport). A transport
error, a timeout or a `5xx` means the server is unreachable: back off and try
again. `401`, `403` and a `404` on the wall at every major this Player reads
are configuration errors. A master whose bytes do not match its hash, that runs
past the size its feed declares, or that answers `404`, is not cached, and the
rest of the wall goes on. Each is said once in the
journal when it starts and once when it ends. None names the token.

**Nothing the server sends is read without a bound.** A master is streamed to
disk under a running hash and stopped at the size its feed declares; a feed is
read up to `MANIFEST_MAX_BYTES`, and the client and label documents up to
`DOCUMENT_MAX_BYTES`. A document past its bound is refused as unreadable, and
what the cache holds stays.

**The heartbeat rides along.** Each worker goes on writing its wall's heartbeat
file into the wall's cache (`WallSettings.heartbeat_root` says why there), and
this module POSTs it whenever it holds a report not yet sent, every poll
interval while the server answers, so a worker needs no second way of
reporting. A report the server refuses with a `4xx` is sent again only once it
changes; one it fails with a `5xx` is sent again on the next poll.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.connector import Connection
from aiohttp.tracing import Trace

from arrt_player.client import ClientDocument, ClientDocumentUnreadable, parse_client_document
from arrt_player.config import CACHED_MANIFEST_FILENAME, PLAIN_COMPONENT, ClientSettings, WallSettings
from arrt_player.episodes import ReportOnce
from arrt_player.heartbeat import REPORTED_AT_KEY
from arrt_player.heartbeat import path_in as heartbeat_path_in
from arrt_player.label_rule import LabelDocument, LabelDocumentUnreadable, parse_label_document
from arrt_player.manifest import MEDIA_DIRNAME, REQUESTED_MAJORS, Feed, ManifestUnreadable, Work, media_name, parse
from arrt_player.safe_write import write_whole

log = logging.getLogger(__name__)

#: The routes this module requests, as `contract/routes.json` spells them.
CLIENT_ROUTE: Final[str] = "/client"
CLIENT_HEARTBEAT_ROUTE: Final[str] = "/client/heartbeat"
MANIFEST_MAJOR_ROUTE: Final[str] = "/walls/{wall_id}/manifest/v{major}"
HEARTBEAT_ROUTE: Final[str] = "/walls/{wall_id}/heartbeat"
LABEL_ROUTE: Final[str] = "/labels/{label_id}"

#: Beside the cached manifest: the ETag it was served with, so a restarted Player
#: asks "has it changed since this?" rather than downloading it again.
ETAG_FILENAME: Final[str] = "manifest.etag"
#: Beside the cached client document, for the same reason.
CLIENT_ETAG_FILENAME: Final[str] = ".client.etag"

#: Directly under `CACHE_DIR`: one file per label this client renders, named by
#: its id, holding the last label document accepted for it. **A leading dot**,
#: as the client document has, so no wall's directory can take the name.
LABEL_CACHE_DIRNAME: Final[str] = ".labels"

#: How often a cached label document's file is stamped with the instant the
#: server last answered for it, while the server goes on answering and sends
#: nothing new. The stamp is the file's modification time, and it is what a
#: restarted renderer runs the rule's hold from, so it is never later than the
#: last answer and at most this much earlier: a hold after a restart ends up to
#: a minute early, never late. Stamping every poll would be a write a second.
LABEL_ANSWER_STAMP_SECONDS: Final[float] = 60.0


#: How long to wait before asking again after the server could not be reached,
#: doubling to the ceiling and reset on the next answer.
BACKOFF_START_SECONDS: Final[float] = 2.0
BACKOFF_MAX_SECONDS: Final[float] = 60.0

#: How often a wall served a lower major asks again for the higher ones
#: (`player-contract.md` § The cutover: a Player "may keep the major that last
#: answered and ask for a higher one less often than it polls"). The poll is
#: about a second, and until the server publishes the higher major for a wall
#: every request for it is a 404: asking each poll would double the requests and
#: put a 404 a second in the server's log. A minute is how long a wall takes to
#: notice a newly published major. Inert while this Player reads one major, and
#: kept because the next one is served beside major 2 the same way.
HIGHER_MAJOR_SECONDS: Final[float] = 60.0

#: How long a new feed waits for a master that fails for a reason that may
#: clear (a `5xx`, a timeout, a dropped connection) before it is cached without
#: it, as a master that answers `404` is at once. The wait exists so a feed is
#: not adopted half-fetched over a blip; its end is what keeps one master that
#: will not download from holding back the wall's schedule, scene and staging
#: for good. The master is asked for again on every later poll until it arrives.
MEDIA_HOLD_SECONDS: Final[float] = 10 * 60.0

#: The most of a client or label document this Player reads. Each is a few
#: kilobytes; the bound is what stops a server, or anything answering in its
#: place, from filling the Pi's memory with one. The contract's `max_bytes` for
#: both routes, which the server promises never to exceed.
DOCUMENT_MAX_BYTES: Final[int] = 1024 * 1024

#: The most of a feed this Player reads: the contract's `max_bytes` for the
#: manifest route. A home wall's feed is tens of kilobytes; a server would reach
#: this only by rotating every few seconds for days, so a feed past it is a
#: fault, and the wall keeps the one it has.
MANIFEST_MAX_BYTES: Final[int] = 16 * 1024 * 1024

#: How much of a body is read at a time.
_CHUNK_BYTES: Final[int] = 64 * 1024

#: The `4xx` statuses that are no verdict on a client heartbeat whatever body
#: comes with them: `401` is the token's fault, not the report's, and `408` and
#: `429` say to try again. Every other `4xx` with the server's error body is
#: its refusal of the report (`ClientPull.report`).
NOT_A_VERDICT_ON_THE_REPORT: Final[frozenset[int]] = frozenset(
    {HTTPStatus.UNAUTHORIZED, HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
)

#: How long a client heartbeat the server refused is held before it is sent
#: again unchanged. Long enough that a report the server will always refuse costs
#: one request in five minutes; short enough that a server which starts accepting
#: it (one upgraded, or one whose refusal was a fault of its own) hears from this
#: Player within one such interval. A changed report is sent at once whatever it is.
REFUSED_REPORT_RESEND_SECONDS: Final[float] = 300.0

#: The most of a refusal's body this Player reads to see whether it is the
#: server's: the error body is one sentence, and anything past this is not one.
ERROR_BODY_MAX_BYTES: Final[int] = 64 * 1024

#: A master's own timeout, in place of the session's total. A master is bounded
#: by the size its feed declares, and a large one over slow Wi-Fi can take longer
#: than any total that suits a manifest; what ends a download that has stalled is
#: thirty seconds with no bytes.
_MEDIA_TIMEOUT: Final = aiohttp.ClientTimeout(total=None, connect=10, sock_read=30)

_UNREACHABLE: Final = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


class DocumentTooLarge(ValueError):
    """A document the server sent runs past the bound this Player reads it to."""


class _Poll(Enum):
    """What one manifest poll found."""

    #: The manifest route answered, and nothing needs retrying.
    ANSWERED = "answered"
    #: The manifest route answered, and a render did not arrive: back off, but the server is there.
    MEDIA_FAILING = "media_failing"
    #: No answer, or a 5xx: back off, and send nothing else.
    UNREACHABLE = "unreachable"
    #: The manifest route answered, and the cache could not take what it sent: back off, but the server is there.
    UNWRITABLE = "unwritable"


class Pull:
    """Keeps one wall's directory holding the newest manifest this Player can fully render."""

    def __init__(
        self,
        settings: WallSettings,
        *,
        interval_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._monotonic = monotonic
        #: Which of `_manifest_urls` last answered with something other than a
        #: 404, and when the higher ones were last asked.
        self._answering: int | None = None
        self._higher_asked_at: float | None = None
        self._server = settings.server_url
        self._token = settings.client_token
        self._cache = settings.wall_dir
        self._media = settings.wall_dir / MEDIA_DIRNAME
        #: Each major this Player asks for, highest first, at its own URL
        #: (`player-contract.md` § The cutover).
        self._manifest_urls = [
            self._server + MANIFEST_MAJOR_ROUTE.format(wall_id=settings.wall_id, major=major)
            for major in sorted(REQUESTED_MAJORS, reverse=True)
        ]
        #: What a media address is resolved against. Any of the majors' URLs
        #: would do: they differ only in their last segment, so they share the
        #: directory a relative address is resolved from.
        self._manifest_url = self._manifest_urls[0]
        self._heartbeat_url = self._server + HEARTBEAT_ROUTE.format(wall_id=settings.wall_id)
        self._interval = settings.poll_interval_seconds if interval_seconds is None else interval_seconds
        self._backoff = BACKOFF_START_SECONDS
        self._heartbeat_seen: tuple[int, int] | None = None
        self._heartbeat_sent: str | None = None
        # One episode per kind of trouble, so each is said once when it starts
        # and once when it clears, rather than once a second.
        self._unreachable = ReportOnce()
        self._refused_status: dict[int, ReportOnce] = {}
        self._unreadable = ReportOnce()
        self._heartbeat_failing = ReportOnce()
        self._media_failing = ReportOnce()
        self._hold_ran_out = ReportOnce()
        self._unwritable = ReportOnce()
        self._skipped: set[str] = set()
        #: When the feed was first held for a master that may yet arrive, or
        #: None while nothing is being held. One wait for the episode, not one
        #: per document: a server republishing every minute must not restart it.
        self._held_since: float | None = None
        #: The hashes of the masters that failed on the last poll, which are
        #: asked for after the rest, so one slow master never keeps the others out.
        self._failing: set[str] = set()
        #: Whether the last manifest poll was answered, which is when the heartbeat goes.
        self._server_heard = False
        #: The stamp of the cached manifest last read and found good, so its
        #: ETag is vouched for without parsing it on every poll.
        self._etag_vouched: tuple[int, int] | None = None

    # -- the loop ---------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Pull until asked to stop, backing off while the server cannot be reached.

        **Two loops, each running one body that `cycle` runs too**: the
        manifest's (`_manifest_turn`, then the wait it names) and the
        heartbeat's (`_heartbeat_turn`, every poll interval), so a backoff never
        delays a report. A crash in the heartbeat loop ends the pull; a crash
        in the manifest loop cancels the heartbeat loop on the way out.
        """
        log.info(
            "pulling the manifest for wall %s from %s into %s",
            self._settings.wall_id,
            self._server,
            self._cache,
            extra={
                "event": "pull.started",
                "wall_id": self._settings.wall_id,
                "server_url": self._server,
                "cache_dir": str(self._cache),
            },
        )
        timeout = aiohttp.ClientTimeout(total=60, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            heartbeats = asyncio.create_task(self._heartbeats(session, stop), name=f"heartbeat:{self._settings.wall_id}")
            try:
                while not stop.is_set():
                    _, wait = await self._manifest_turn(session)
                    await _rest(stop, wait, heartbeats)
                    if heartbeats.done():
                        # Re-raises a heartbeat loop that crashed, so the pull
                        # ends failed rather than polling on without being heard.
                        heartbeats.result()
            finally:
                heartbeats.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeats

    async def cycle(self, session: aiohttp.ClientSession) -> bool:
        """One turn of each of `run`'s loops, the manifest's then the heartbeat's. False when the next poll should back off."""
        polled, _ = await self._manifest_turn(session)
        await self._heartbeat_turn(session)
        return polled is _Poll.ANSWERED

    async def _manifest_turn(self, session: aiohttp.ClientSession) -> tuple[_Poll, float]:
        """One manifest poll, and how long to wait before the next: the interval after an answer, else the backoff."""
        polled = await self._pull_manifest(session)
        self._server_heard = polled is not _Poll.UNREACHABLE
        if polled is _Poll.ANSWERED:
            self._backoff = BACKOFF_START_SECONDS
            return polled, self._interval
        return polled, self._next_backoff()

    async def _heartbeat_turn(self, session: aiohttp.ClientSession) -> None:
        """Forward the heartbeat, if the server answered the last manifest poll."""
        if self._server_heard:
            await self._forward_heartbeat(session)

    async def _heartbeats(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> None:
        """Forward the heartbeat every poll interval while the server answers, whatever the manifest's backoff is doing.

        **Backing off and being heard are separate.** A render that keeps
        failing backs the manifest poll off, but the server's manifest route
        answered, so the heartbeat still goes, and at the poll's own pace: a
        label follows the wall's display state through the server, and must not
        fall a minute behind because one render will not download. A server
        that did not answer the last poll is not posted to until it does.
        """
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            if not stop.is_set():
                await self._heartbeat_turn(session)

    def _next_backoff(self) -> float:
        wait = self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_SECONDS)
        return wait

    # -- the manifest -----------------------------------------------------------------------

    async def _pull_manifest(self, session: aiohttp.ClientSession) -> _Poll:
        headers = self._auth()
        etag = self._cached_etag()
        if etag is not None:
            headers["If-None-Match"] = etag
        try:
            status, body, served_etag = await self._request_manifest(session, headers)
        except _UNREACHABLE as exc:
            self._report_unreachable(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
            return _Poll.UNREACHABLE
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            self._report_unreachable(f"it answered {status}")
            return _Poll.UNREACHABLE
        self._report_reachable()

        if status not in (200, 304):
            self._report_refused(status)
            return _Poll.ANSWERED
        # A 304 is the server accepting this client's token for a manifest it has
        # already sent, so it ends a refusal just as a 200 does.
        self._report_accepted()
        if status == HTTPStatus.NOT_MODIFIED:
            # The feed already cached, which was cached with an ETag only when
            # every master it names was in hand, is the server's current one.
            self._media_settled()
            return _Poll.ANSWERED

        try:
            text = _text_of(body, "feed", MANIFEST_MAX_BYTES)
            parsed = parse(text)
            document = json.loads(text)
        except (DocumentTooLarge, UnicodeDecodeError, ManifestUnreadable) as exc:
            # Checked with the same parser the watcher uses, before anything is
            # cached: a refused document must never become the "last good" one a
            # restart would start from.
            if self._unreadable.begin():
                log.error(  # noqa: TRY400 -- the message is the finding
                    "refusing the manifest the server sent (%s); keeping the one already cached",
                    exc,
                    extra={"event": "pull.manifest_refused"},
                )
            return _Poll.ANSWERED
        if self._unreadable.end():
            log.info("the server's manifest can be read again", extra={"event": "pull.manifest_readable"})

        return await self._adopt_feed(session, parsed, document, served_etag)

    async def _request_manifest(
        self, session: aiohttp.ClientSession, headers: dict[str, str]
    ) -> tuple[int, bytes | None, str | None]:
        """Ask for each requested major, highest first, and keep the first answer that is not a 404.

        **A 404 means "not this major for this wall"**, and the next one down is
        asked; only when every one answers 404 is it the answer, which the caller
        reports as nothing published. Any other answer is the server's word on
        this wall and stops the walk: a refused token is refused at every major,
        and asking the next would only say so again.
        """
        start = self._first_to_ask()
        status, body, served_etag = await self._walk(session, headers, start)
        if status == HTTPStatus.NOT_FOUND and start > 0:
            # The major that was answering has stopped; a higher one may have
            # taken its place, so every major is asked now rather than in a minute.
            status, body, served_etag = await self._walk(session, headers, 0)
        return status, body, served_etag

    def _first_to_ask(self) -> int:
        """Where this poll starts: at the major last answering, unless the higher ones are due to be asked again."""
        now = self._monotonic()
        if self._answering and self._higher_asked_at is not None and now - self._higher_asked_at < HIGHER_MAJOR_SECONDS:
            return self._answering
        self._higher_asked_at = now
        return 0

    async def _walk(
        self, session: aiohttp.ClientSession, headers: dict[str, str], start: int
    ) -> tuple[int, bytes | None, str | None]:
        """Ask each major from `start` down, stopping at the first answer that is not a 404.

        A feed's body is None once it runs past `MANIFEST_MAX_BYTES`.
        """
        self._answering = None
        for index in range(start, len(self._manifest_urls)):
            async with session.get(self._manifest_urls[index], headers=headers) as response:
                status = response.status
                body = await _read_capped(response, MANIFEST_MAX_BYTES) if status == HTTPStatus.OK else b""
                served_etag = response.headers.get("ETag")
            if status != HTTPStatus.NOT_FOUND:
                self._answering = index
                break
        return status, body, served_etag

    async def _adopt_feed(
        self, session: aiohttp.ClientSession, feed: Feed, document: dict[str, Any], served_etag: str | None
    ) -> _Poll:
        """Cache a major 2 feed's media, then the feed itself, whole.

        **Kept whole**: every work the schedule names must be a key of `works`
        (`player-contract.md` § Rules a schema cannot state), so dropping one
        would cache a document this reader refuses. A work whose media could not
        be had stays in the feed, and the programme skips it until its file is
        here. Every work the feed carries is fetched, staged ones included, so a
        scene is a switch rather than a download.

        **A master that may yet arrive holds the feed, for a while.** While one
        fails for a reason that may clear, the feed already cached stays, for up
        to `MEDIA_HOLD_SECONDS`; then the new feed is cached without that master,
        and with no ETag, so every later poll is answered whole and asks for the
        master again until it arrives.
        """
        missing: list[Work] = []
        failed: set[str] = set()
        why = ""
        gone = False
        for work in self._in_fetch_order(feed):
            if gone:
                # The server stopped answering partway through: the rest are
                # not asked for this poll, which would only time out in turn,
                # and keep their place in the order for the next.
                missing.append(work)
                continue
            outcome = await self._cache_media(session, work, _declared_size(document, work.work_id))
            if isinstance(outcome, _Retry):
                missing.append(work)
                failed.add(work.sha256)
                # A cache that refused one master refuses the rest too.
                why, gone = outcome.why, outcome.unreachable or outcome.unwritable
        self._failing = failed
        if missing:
            return self._hold_or_adopt(feed, document, missing, why)
        self._media_settled()
        return _Poll.ANSWERED if self._cache_feed(feed, document, served_etag) else _Poll.UNWRITABLE

    def _media_settled(self) -> None:
        """Nothing is being waited for: the feed the server serves has every master here."""
        self._held_since = None
        self._hold_ran_out.end()
        if self._media_failing.end():
            log.info("renders for this wall arrive again", extra={"event": "pull.media_ok"})

    def _in_fetch_order(self, feed: Feed) -> list[Work]:
        """The feed's works in its order, those that failed last poll moved to the end."""
        works = list(feed.works.values())
        return [work for work in works if work.sha256 not in self._failing] + [
            work for work in works if work.sha256 in self._failing
        ]

    def _hold_or_adopt(self, feed: Feed, document: dict[str, Any], missing: list[Work], why: str) -> _Poll:
        now = self._monotonic()
        if self._held_since is None:
            self._held_since = now
        if self._media_failing.begin():
            log.warning(
                "a work's media for this wall could not be fetched (%s); keeping the manifest already cached "
                "for up to %d minutes, then taking the new one without it",
                why,
                MEDIA_HOLD_SECONDS // 60,
                extra={"event": "pull.media_failing", "hold_seconds": MEDIA_HOLD_SECONDS},
            )
        if now - self._held_since < MEDIA_HOLD_SECONDS:
            return _Poll.MEDIA_FAILING
        if self._hold_ran_out.begin():
            log.warning(
                "the media of %d works for this wall has not arrived in %d minutes (%s); caching the feed without it, "
                "and asking for it again on every poll",
                len(missing),
                MEDIA_HOLD_SECONDS // 60,
                why,
                extra={"event": "pull.hold_ended", "work_ids": [work.work_id for work in missing]},
            )
        self._cache_feed(feed, document, None)
        return _Poll.MEDIA_FAILING

    def _cache_feed(self, feed: Feed, document: dict[str, Any], served_etag: str | None) -> bool:
        """Write the feed, evict what it does not name, then its ETag. False when the cache refused any of it.

        **A cache that cannot be written costs the new feed, not the wall** (#9):
        the feed already cached stays where the watcher reads it, and the next
        poll, under the backoff, is answered whole and tries again.
        """
        manifest = self._cache / CACHED_MANIFEST_FILENAME
        text = json.dumps(document, indent=2).encode("utf-8")
        try:
            previous = self._cached_render_names()
            # **The old ETag goes before the manifest it describes is replaced**,
            # and a new one is written last: an ETag left beside a manifest it
            # does not describe gets a 304 if the server goes back to that
            # ETag's document, and the wall keeps a feed the server no longer
            # serves. In this order a crash, or a write the cache refuses,
            # anywhere before the end leaves no ETag, which costs one full
            # download and asks for all of this again.
            (self._cache / ETAG_FILENAME).unlink(missing_ok=True)
            # The same feed again, as every poll past a hold brings, is not
            # rewritten: the watcher would read it as new each time.
            written = not _holds(manifest, text)
            if written:
                _write_atomically(manifest, text)
            self._media.mkdir(parents=True, exist_ok=True)
            held = {Path(work.media_path).name for work in feed.works.values() if (self._cache / work.media_path).is_file()}
            kept = self._evict(held | previous)
            if written:
                log.info(
                    "cached the feed for this wall with %d of %d works' media; %d held",
                    len(held),
                    len(feed.works),
                    kept,
                    extra={"event": "pull.adopted", "works": len(feed.works), "media_held": len(held)},
                )
            if served_etag:
                _write_atomically(self._cache / ETAG_FILENAME, served_etag.encode("utf-8"))
        except OSError as exc:
            self._report_unwritable(exc)
            return False
        if self._unwritable.end():
            log.info("the cache for this wall can be written again", extra={"event": "pull.cache_writable"})
        return True

    async def _cache_media(  # noqa: PLR0911 -- one return per way a master can end, each said where it happens
        self, session: aiohttp.ClientSession, work: Work, declared: int | None
    ) -> "bool | _Retry":
        """Whether the work's master is in the cache now; False to skip the work; `_Retry` to try again later.

        The work's hash and address are already checked: the parser refuses a
        feed whose work carries no media with a hash and a URL. Its size is the
        feed's to declare, and bounds the download.
        """
        work_id, sha = work.work_id, work.sha256
        name = media_name(sha)
        if (self._media / name).is_file():
            return True
        if declared is None:
            self._skip_once(
                f"unsized:{sha}",
                "the feed declares no size for the master of work %s, and the size is what bounds its download; skipping it",
                work_id,
            )
            return False

        address = urljoin(self._manifest_url, work.url)
        # The token is this client's credential on this server. A master named on
        # another host is fetched without it, so a manifest can never be used to
        # send the token somewhere else.
        headers = self._auth() if _same_origin(address, self._server) else {}
        temporary = self._media / f".{name}.tmp"
        try:
            _on_disk(self._media.mkdir, parents=True, exist_ok=True)
            async with session.get(address, headers=headers, timeout=_MEDIA_TIMEOUT) as response:
                status = response.status
                digest = await _stream_to(response, temporary, declared) if status == HTTPStatus.OK else None
        except _CacheRefused as exc:
            # Caught first, and not an `OSError`: a full card is not the server gone.
            self._report_unwritable(exc.cause)
            return _Retry(f"the cache refused it ({exc.cause})", unwritable=True)
        except _UNREACHABLE as exc:
            return _Retry(f"{type(exc).__name__} fetching it", unreachable=True)
        if status == HTTPStatus.NOT_FOUND:
            self._skip_once(f"missing:{sha}", "the master for work %s is not held by the server; skipping it", work_id)
            return False
        if status != HTTPStatus.OK:
            return _Retry(f"the media route answered {status}")
        if digest is None:
            self._skip_once(
                f"oversize:{sha}",
                "the master for work %s ran past the %d bytes its feed declares and was discarded; skipping it",
                work_id,
                declared,
            )
            return False
        if digest != sha:
            _discard_quietly(temporary)
            self._skip_once(
                f"mismatch:{sha}",
                "the master for work %s did not match its hash and was discarded; skipping it",
                work_id,
            )
            return False
        try:
            temporary.replace(self._media / name)
        except OSError as exc:
            _discard_quietly(temporary)
            self._report_unwritable(exc)
            return _Retry(f"the cache refused it ({exc})", unwritable=True)
        return True

    def _evict(self, referenced: set[str]) -> int:
        """Remove every cached master the newly cached feed does not name. Returns how many remain."""
        kept = 0
        for path in self._media.iterdir():
            if path.name in referenced:
                kept += 1
                continue
            path.unlink(missing_ok=True)
        return kept

    def _cached_render_names(self) -> set[str]:
        """The media files the feed now in the cache names, or none if there is none."""
        try:
            cached = json.loads((self._cache / CACHED_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return set()
        works = cached.get("works") if isinstance(cached, dict) else None
        if not isinstance(works, dict):
            # A document of a major this Player no longer reads: no feed names
            # its files, so they go at the next adoption.
            return set()
        return {
            media_name(work["media"]["sha256"])
            for work in works.values()
            if isinstance(work, dict) and isinstance(work.get("media"), dict) and isinstance(work["media"].get("sha256"), str)
        }

    def _cached_etag(self) -> str | None:
        """The ETag to send, but only while the manifest it describes is cached and the watcher can read it.

        **Read with the watcher's own parser.** Sent beside a manifest the
        watcher refuses (a torn write, a damaged card, a hand edit), the ETag
        gets a 304, and the wall has no feed until the server's changes. Without
        it, the server sends the whole feed again and the pull replaces it.
        """
        manifest = self._cache / CACHED_MANIFEST_FILENAME
        try:
            stat = manifest.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self._etag_vouched:
                parse(manifest.read_text(encoding="utf-8"))
                self._etag_vouched = stamp
            return (self._cache / ETAG_FILENAME).read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeDecodeError, ManifestUnreadable):
            # Missing or unreadable, the answer is the same: ask without one,
            # and the server sends the whole document.
            return None

    # -- the heartbeat ----------------------------------------------------------------------

    async def _forward_heartbeat(self, session: aiohttp.ClientSession) -> None:
        path = heartbeat_path_in(self._settings.heartbeat_root, self._settings.wall_id)
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._heartbeat_seen:
                return
            body = path.read_bytes()
        except FileNotFoundError:
            return
        # Keyed on the report's own instant as well as the file's stamp: a file
        # rewritten with the same report, by anything, is not a new heartbeat,
        # and posting it again is how a shared directory turns into a loop.
        try:
            reported_at = json.loads(body).get("reported_at")
        except (ValueError, AttributeError):
            reported_at = None
        if reported_at is not None and reported_at == self._heartbeat_sent:
            self._heartbeat_seen = stamp
            return
        try:
            async with session.post(
                self._heartbeat_url, data=body, headers={**self._auth(), "Content-Type": "application/json"}
            ) as response:
                status = response.status
        except _UNREACHABLE:
            return
        self._heard(status, stamp, reported_at)

    def _heard(self, status: int, stamp: tuple[int, int], reported_at: object) -> None:
        """What the server's answer to a heartbeat means for the next one."""
        if status == HTTPStatus.NO_CONTENT:
            self._heartbeat_seen = stamp
            self._heartbeat_sent = reported_at
            if self._heartbeat_failing.end():
                log.info("the server is accepting this wall's heartbeat again", extra={"event": "pull.heartbeat_ok"})
            return
        if status < HTTPStatus.INTERNAL_SERVER_ERROR:
            # **A 4xx is the server's word on this report**, and posting it again
            # each poll would only hear the same word; the next report may differ.
            self._heartbeat_seen = stamp
            self._heartbeat_sent = reported_at
            if self._heartbeat_failing.begin():
                log.warning(
                    "the server refused this wall's heartbeat with %d; it will be sent again when it next changes",
                    status,
                    extra={"event": "pull.heartbeat_refused", "status": status},
                )
            return
        if self._heartbeat_failing.begin():
            log.warning(
                "the server failed this wall's heartbeat with %d; it will be sent again on the next poll",
                status,
                extra={"event": "pull.heartbeat_failed", "status": status},
            )

    # -- reporting --------------------------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _report_unreachable(self, why: str) -> None:
        if self._unreachable.begin():
            log.warning(
                "the server at %s cannot be reached (%s); the wall keeps showing what is cached",
                self._server,
                why,
                extra={"event": "pull.unreachable", "server_url": self._server},
            )

    def _report_unwritable(self, exc: OSError) -> None:
        if self._unwritable.begin():
            log.error(
                "the cache for this wall at %s cannot be written (%s); the wall keeps showing the feed already cached, "
                "and each poll tries again",
                self._cache,
                exc,
                extra={"event": "pull.cache_unwritable", "cache_dir": str(self._cache), "errno": exc.errno},
            )

    def _report_reachable(self) -> None:
        if self._unreachable.end():
            log.info("the server at %s answers again", self._server, extra={"event": "pull.reachable"})

    def _report_refused(self, status: int) -> None:
        episode = self._refused_status.setdefault(status, ReportOnce())
        if episode.begin():
            reason = {
                401: "it does not accept this client's token (CLIENT_TOKEN)",
                403: "this wall is not assigned to this client, or the server holds no such wall",
                404: (
                    "it publishes no manifest major this Player reads "
                    f"({', '.join(str(major) for major in REQUESTED_MAJORS)}) for this wall, "
                    "or no theme hangs there yet"
                ),
            }.get(status, f"it answered {status}")
            log.error(
                "the server refused this wall's manifest: %s; keeping the one already cached",
                reason,
                extra={"event": "pull.refused", "status": status},
            )

    def _report_accepted(self) -> None:
        for status, episode in self._refused_status.items():
            if episode.end():
                log.info(
                    "the server serves this wall's manifest again after %d",
                    status,
                    extra={"event": "pull.accepted", "status": status},
                )

    def _skip_once(self, key: str, message: str, work_id: object, *args: object) -> None:
        if key in self._skipped:
            return
        self._skipped.add(key)
        log.warning(message, work_id, *args, extra={"event": "pull.work_skipped", "work_id": work_id})


class _AddressNoting(aiohttp.TCPConnector):
    """A connector that notes the local address of each connection it hands out.

    **Read as the connection is handed out**, because that is the one moment it
    is certain to be held: a response whose body has already arrived returns its
    connection to the pool before `ClientResponse` is given back, and its
    `connection` is then None.
    """

    def __init__(self, noted: Callable[[str], None], **kwargs: Any) -> None:  # noqa: ANN401 -- TCPConnector's own keywords
        super().__init__(**kwargs)
        self._noted = noted

    async def connect(
        self,
        req: aiohttp.ClientRequest,
        traces: list[Trace],
        timeout: aiohttp.ClientTimeout,  # noqa: ASYNC109 -- the signature is aiohttp's, overridden
    ) -> Connection:
        connection = await super().connect(req, traces, timeout)
        transport = connection.transport
        sockname = transport.get_extra_info("sockname") if transport is not None else None
        if isinstance(sockname, tuple) and sockname and isinstance(sockname[0], str):
            self._noted(sockname[0])
        return connection


def _is_error_body(body: bytes | None) -> bool:
    """Whether a body is the transport table's error shape: a JSON object whose `error` is a sentence."""
    if body is None:
        return False
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return isinstance(document, dict) and isinstance(document.get("error"), str)


class ClientPull:
    """`GET /client` and `POST /client/heartbeat`, with this client's token: the supervisor's `ClientLink`.

    **Read with the same posture as a wall's manifest.** A document is cached,
    with its ETag, only once it has been read and accepted, so the file a
    restart starts from is always one this reader could act on. Every way the
    server can fail to give a new one — unreachable, refusing the token, sending
    something unreadable — answers None, which the supervisor reads as "keep the
    walls you have", and each is said once when it starts and once when it ends.
    """

    def __init__(self, settings: ClientSettings, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._server = settings.server_url
        self._token = settings.client_token
        self._document_path = settings.client_document_path
        self._etag_path = settings.cache_dir / CLIENT_ETAG_FILENAME
        self._client_url = self._server + CLIENT_ROUTE
        self._heartbeat_url = self._server + CLIENT_HEARTBEAT_ROUTE
        self._session: aiohttp.ClientSession | None = None
        self._unreachable = ReportOnce()
        self._refused_status: dict[int, ReportOnce] = {}
        self._unreadable = ReportOnce()
        self._uncacheable = ReportOnce()
        self._heartbeat_failing = ReportOnce()
        #: The last client heartbeat the server refused, without its instant, and
        #: when it was refused; None once one is accepted.
        self._held: tuple[dict[str, Any], float] | None = None
        #: This end's address on the last connection to the server, or None
        #: before the first one.
        self._local_address: str | None = None

    @property
    def local_address(self) -> str | None:
        """The address this Player reached the server from on its last connection, or None before any."""
        return self._local_address

    def cached(self) -> ClientDocument | None:
        """The last document this client accepted, or None if there is none it can read."""
        try:
            return parse_client_document(self._document_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ClientDocumentUnreadable) as exc:
            log.warning(
                "the cached client document at %s cannot be read (%s); starting with no walls until the server answers",
                self._document_path,
                exc,
                extra={"event": "client.cache_unreadable"},
            )
            return None

    async def fetch(  # noqa: C901, PLR0911, PLR0912 -- one branch per answer the server can give, each keeping the walls running
        self,
    ) -> ClientDocument | None:
        """A new client document, or None to keep the walls already running."""
        headers = self._auth()
        etag = self._cached_etag()
        if etag is not None:
            headers["If-None-Match"] = etag
        try:
            async with self._client().get(self._client_url, headers=headers) as response:
                status = response.status
                body = await _read_capped(response, DOCUMENT_MAX_BYTES) if status == HTTPStatus.OK else b""
                served_etag = response.headers.get("ETag")
        except _UNREACHABLE as exc:
            self._report_unreachable(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
            return None
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            self._report_unreachable(f"it answered {status}")
            return None
        if self._unreachable.end():
            log.info("the server at %s answers this client again", self._server, extra={"event": "client.reachable"})
        if status not in (200, 304):
            episode = self._refused_status.setdefault(status, ReportOnce())
            if episode.begin():
                reason = {401: "it does not accept this client's token (CLIENT_TOKEN)"}.get(status, f"it answered {status}")
                log.error(
                    "the server would not say which walls this client drives: %s; the walls already running keep going",
                    reason,
                    extra={"event": "client.refused", "status": status},
                )
            return None
        for refused, episode in self._refused_status.items():
            if episode.end():
                log.info(
                    "the server answers this client's token again after %d",
                    refused,
                    extra={"event": "client.accepted", "status": refused},
                )
        if status == HTTPStatus.NOT_MODIFIED:
            return None
        try:
            text = _text_of(body, "client document", DOCUMENT_MAX_BYTES)
            document = parse_client_document(text)
        except (DocumentTooLarge, UnicodeDecodeError, ClientDocumentUnreadable) as exc:
            if self._unreadable.begin():
                log.error(  # noqa: TRY400 -- the message is the finding
                    "refusing the client document the server sent (%s); the walls already running keep going",
                    exc,
                    extra={"event": "client.document_refused"},
                )
            return None
        if self._unreadable.end():
            log.info("the server's client document can be read again", extra={"event": "client.document_readable"})
        # **A cache that cannot be written costs the cache, not the walls.** The
        # document is handed over all the same: a full disk would otherwise end
        # the process, and every restart would meet the same disk.
        try:
            self._document_path.parent.mkdir(parents=True, exist_ok=True)
            # The old ETag goes first, for the reason `Pull._adopt_feed` gives.
            self._etag_path.unlink(missing_ok=True)
            _write_atomically(self._document_path, text.encode("utf-8"))
            if served_etag:
                _write_atomically(self._etag_path, served_etag.encode("utf-8"))
        except OSError as exc:
            if self._uncacheable.begin():
                log.error(  # noqa: TRY400 -- the message is the finding
                    "the client document cannot be cached at %s (%s); the walls follow it, and a restart "
                    "while the server is down starts from the last one cached",
                    self._document_path,
                    exc,
                    extra={"event": "client.cache_unwritable"},
                )
            return document
        if self._uncacheable.end():
            log.info("the client document is cached again", extra={"event": "client.cache_writable"})
        return document

    async def report(self, heartbeat: dict[str, Any]) -> bool:
        """POST the client heartbeat. True when the server answered 204.

        **A report the server refused is held**: not sent again until it changes,
        or until `REFUSED_REPORT_RESEND_SECONDS` have passed, so the same report
        reaches a server that refuses it once per lapse rather than once per
        poll, and a server that has come to accept it hears from this Player
        within one. Changed means any field but the instant it was made, so a
        screen plugged in, or a new settings address, is sent at once.

        **Refused means the server's word on this report**, which is known by
        its error body (the transport table's `error_body`), not by the status:
        any `4xx` carrying it, the table's `400` and `413` and a status no row
        names alike, but those in `NOT_A_VERDICT_ON_THE_REPORT`. An answer
        without that body was not the server's (a proxy's `404`, a captive
        portal's `200`, a framework's own `400`), and neither is a verdict on
        the report: like a `408`, a `429`, a `5xx` or no answer, it is read as
        the table's `unanswered` case is, and the report goes on being sent. So
        does one refused for its token (`401`), which is the token's fault.
        """
        report = {key: value for key, value in heartbeat.items() if key != REPORTED_AT_KEY}
        if self._held is not None:
            held, refused_at = self._held
            if report == held and self._monotonic() - refused_at < REFUSED_REPORT_RESEND_SECONDS:
                return False
        try:
            async with self._client().post(
                self._heartbeat_url,
                data=json.dumps(heartbeat).encode("utf-8"),
                headers={**self._auth(), "Content-Type": "application/json"},
            ) as response:
                status = response.status
                refused = (
                    HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
                    and status not in NOT_A_VERDICT_ON_THE_REPORT
                    and _is_error_body(await _read_capped(response, ERROR_BODY_MAX_BYTES))
                )
        except _UNREACHABLE:
            # Said by `fetch`, which runs on the same poll; a second line here
            # would be the same outage told twice.
            return False
        if status == HTTPStatus.NO_CONTENT:
            self._held = None
            if self._heartbeat_failing.end():
                log.info("the server is accepting this client's heartbeat again", extra={"event": "client.heartbeat_ok"})
            return True
        if refused:
            self._held = (report, self._monotonic())
        if self._heartbeat_failing.begin():
            log.warning(
                "the server did not take this client's heartbeat (it answered %d); %s",
                status,
                (
                    f"it will be sent again when it changes, or in {REFUSED_REPORT_RESEND_SECONDS:.0f} seconds"
                    if refused
                    else "it will be sent again"
                ),
                extra={"event": "client.heartbeat_refused", "status": status},
            )
        return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=10), connector=_AddressNoting(self._note_address)
            )
        return self._session

    def _cached_etag(self) -> str | None:
        """The ETag to send, but only while the document it describes is cached and `cached` can read it.

        Sent beside a document this client refuses, the ETag gets a 304, and no
        wall on the client starts until a curator changes its assignments.
        """
        try:
            parse_client_document(self._document_path.read_text(encoding="utf-8"))
            return self._etag_path.read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeDecodeError, ClientDocumentUnreadable):
            # Missing or unreadable, the answer is the same: ask without one,
            # and the server sends the whole document.
            return None

    def _note_address(self, address: str) -> None:
        self._local_address = address

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _report_unreachable(self, why: str) -> None:
        if self._unreachable.begin():
            log.warning(
                "the server at %s cannot be reached (%s); this client keeps its walls as they are",
                self._server,
                why,
                extra={"event": "client.unreachable", "server_url": self._server},
            )


@dataclass(frozen=True)
class CachedLabel:
    """The label document a client last accepted, as a restarted renderer starts from it."""

    document: LabelDocument
    #: When the server last answered for this label, by the wall clock: never
    #: later than the last answer, and at most `LABEL_ANSWER_STAMP_SECONDS` earlier.
    answered_at: datetime


@dataclass(frozen=True)
class LabelAnswer:
    """What one poll of a label document found."""

    #: False when the server could not be reached (no answer, a timeout, a 5xx):
    #: the renderer reads its last document as offline. True for every answer,
    #: a refusal included.
    reachable: bool
    #: A new document this reader accepted, or None to keep the one it has.
    document: LabelDocument | None = None


class LabelPull:
    """`GET /labels/{label_id}` with this client's token and an ETag: a label renderer's link.

    **Read with the manifest's posture.** A refusal (`401`, `403`, `404`) or a
    document this reader cannot use keeps the last good one; transport errors,
    timeouts and `5xx` mean the server is unreachable, which the renderer answers
    by holding its caption for the rule's 30 minutes. Each is said once when it
    starts and once when it ends.

    **Each accepted document is cached**, under `LABEL_CACHE_DIRNAME`, and the
    file is stamped as the server goes on answering, so a renderer restarted
    while the server is down starts from it (`cached`). The ETag is not: it is
    held in memory only, so a restarted renderer asks for the whole document and
    never sends an ETag for a document it does not hold.
    """

    def __init__(
        self,
        settings: ClientSettings,
        label_id: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._server = settings.server_url
        self._token = settings.client_token
        self._label_id = label_id
        plain = PLAIN_COMPONENT.fullmatch(label_id) is not None
        #: None for an id that is not a plain path component: put into the
        #: route, "../x" would ask for another route of the server's, so it is
        #: never asked for at all.
        self._url = self._server + LABEL_ROUTE.format(label_id=label_id) if plain else None
        #: None for the same ids: put into a path, "../x" would name a file
        #: outside the cache.
        self._cache_path = settings.cache_dir / LABEL_CACHE_DIRNAME / f"{label_id}.json" if plain else None
        #: The stamp's clock, which a restarted renderer compares with its own `now`.
        self._now = now
        #: The throttle's clock: when the file was last stamped.
        self._monotonic = monotonic
        self._stamped: float | None = None
        self._uncacheable = ReportOnce()
        self._unusable = ReportOnce()
        self._etag: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._unreachable = ReportOnce()
        self._refused_status: dict[int, ReportOnce] = {}
        self._unreadable = ReportOnce()

    async def fetch(self) -> LabelAnswer:
        if self._url is None:
            if self._unusable.begin():
                log.error(
                    "the server named label %r, which is not a plain path component; this client will not ask for it, "
                    "and the panel stays as it is",
                    self._label_id,
                    extra={"event": "label.id_unusable", "label_id": self._label_id},
                )
            return LabelAnswer(reachable=True)
        return await self._ask(self._url)

    @property
    def cache_path(self) -> Path | None:
        """Where this pull caches its label's document, or None for an id it will not cache.

        **The one rule for a label id's file**: whatever removes a label's cached
        document (`__main__.LabelCacheSweep`) learns the path here, never derives
        it beside this, so the file removed is the file written.
        """
        return self._cache_path

    def cached(self) -> CachedLabel | None:
        """The last document accepted for this label and when the server last answered, or None if there is none it can read.

        **Unreadable is absent**, as the client document's cache is: a file this
        reader refuses is never a reason to stop, and never one to draw from.
        """
        if self._cache_path is None:
            return None
        try:
            text = self._cache_path.read_text(encoding="utf-8")
            answered_at = datetime.fromtimestamp(self._cache_path.stat().st_mtime, UTC)
            return CachedLabel(document=parse_label_document(text), answered_at=answered_at)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError, LabelDocumentUnreadable) as exc:
            # `ValueError` is a modification time `fromtimestamp` cannot place.
            log.warning(
                "the cached document for label %s at %s cannot be read (%s); the label starts blank until the server answers",
                self._label_id,
                self._cache_path,
                exc,
                extra={"event": "label.cache_unreadable", "label_id": self._label_id},
            )
            return None

    async def _ask(self, url: str) -> LabelAnswer:  # noqa: C901, PLR0912 -- one branch per answer the server can give
        headers = {"Authorization": f"Bearer {self._token}"}
        if self._etag is not None:
            headers["If-None-Match"] = self._etag
        try:
            async with self._client().get(url, headers=headers) as response:
                status = response.status
                body = await _read_capped(response, DOCUMENT_MAX_BYTES) if status == HTTPStatus.OK else b""
                served_etag = response.headers.get("ETag")
        except _UNREACHABLE as exc:
            self._report_unreachable(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
            return LabelAnswer(reachable=False)
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            self._report_unreachable(f"it answered {status}")
            return LabelAnswer(reachable=False)
        if self._unreachable.end():
            log.info(
                "the server answers label %s again",
                self._label_id,
                extra={"event": "label.server_reachable", "label_id": self._label_id},
            )
        if status != HTTPStatus.OK:
            # An answer, so the hold a restart would run starts again here. Not
            # on a 200: its document replaces the cached one, and the stamp goes
            # with it, so a document this client could not cache is never left
            # stamped as though it were the server's latest.
            self._stamp()
        if status not in (200, 304):
            episode = self._refused_status.setdefault(status, ReportOnce())
            if episode.begin():
                reason = {
                    401: "it does not accept this client's token (CLIENT_TOKEN)",
                    403: "this label output is not this client's, or the server holds no such label",
                }.get(status, f"it answered {status}")
                log.error(
                    "the server refused label %s: %s; keeping what the panel shows",
                    self._label_id,
                    reason,
                    extra={"event": "label.refused", "label_id": self._label_id, "status": status},
                )
            return LabelAnswer(reachable=True)
        for refused, episode in self._refused_status.items():
            if episode.end():
                log.info(
                    "the server serves label %s again after %d",
                    self._label_id,
                    refused,
                    extra={"event": "label.accepted", "label_id": self._label_id, "status": refused},
                )
        if status == HTTPStatus.NOT_MODIFIED:
            return LabelAnswer(reachable=True)
        try:
            document = parse_label_document(_text_of(body, "label document", DOCUMENT_MAX_BYTES))
        except (DocumentTooLarge, UnicodeDecodeError, LabelDocumentUnreadable) as exc:
            if self._unreadable.begin():
                log.error(  # noqa: TRY400 -- the message is the finding
                    "refusing the label document the server sent for %s (%s); keeping the last one",
                    self._label_id,
                    exc,
                    extra={"event": "label.document_refused", "label_id": self._label_id},
                )
            return LabelAnswer(reachable=True)
        if self._unreadable.end():
            log.info(
                "the server's label document for %s can be read again",
                self._label_id,
                extra={"event": "label.document_readable", "label_id": self._label_id},
            )
        # **No ETag for a document the cache did not take**: asked with one,
        # the server answers 304 and the document is never cached while it
        # stays the server's latest.
        self._etag = (served_etag or None) if self._keep(body) else None
        return LabelAnswer(reachable=True, document=document)

    def _keep(self, body: bytes | None) -> bool:
        """Cache an accepted document, stamped now; False when the cache refused it.

        **A cache that cannot be written costs the cache, not the label.**
        """
        path = self._cache_path
        if path is None or body is None:
            return True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomically(path, body)
            self._set_stamp(path)
        except OSError as exc:
            # **The document it would have replaced goes too.** It was stamped
            # by this very answer, and left, a restart would hold it as the
            # server's latest word when the latest may say the screen is dark.
            with contextlib.suppress(OSError):
                _discard(path)
            self._report_uncacheable(exc)
            return False
        self._report_cacheable()
        return True

    def _stamp(self) -> None:
        """Move the cached document's stamp to now, at most once per `LABEL_ANSWER_STAMP_SECONDS`."""
        path = self._cache_path
        if path is None:
            return
        if self._stamped is not None and self._monotonic() - self._stamped < LABEL_ANSWER_STAMP_SECONDS:
            return
        try:
            self._set_stamp(path)
        except FileNotFoundError:
            # Nothing cached yet, so nothing to stamp: the next accepted
            # document is written with its own.
            return
        except OSError as exc:
            self._report_uncacheable(exc)
            return
        # A refused stamp began the episode too, and a 304 a minute may be all
        # the server sends for as long as its document stands.
        self._report_cacheable()

    def _set_stamp(self, path: Path) -> None:
        instant = self._now().timestamp()
        os.utime(path, (instant, instant))
        self._stamped = self._monotonic()

    def _report_cacheable(self) -> None:
        if self._uncacheable.end():
            log.info(
                "the document for label %s is cached again",
                self._label_id,
                extra={"event": "label.cache_writable", "label_id": self._label_id},
            )

    def _report_uncacheable(self, exc: OSError) -> None:
        if self._uncacheable.begin():
            log.error(
                "the document for label %s cannot be cached at %s (%s); the label follows the server, and a restart "
                "while the server is down starts blank",
                self._label_id,
                self._cache_path,
                exc,
                extra={"event": "label.cache_unwritable", "label_id": self._label_id},
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10, connect=5))
        return self._session

    def _report_unreachable(self, why: str) -> None:
        if self._unreachable.begin():
            log.warning(
                "the server at %s cannot be reached for label %s (%s); the label holds its caption for 30 minutes",
                self._server,
                self._label_id,
                why,
                extra={"event": "label.server_unreachable", "label_id": self._label_id, "server_url": self._server},
            )


@dataclass(frozen=True)
class _Retry:
    """A render that did not arrive for a reason that may clear, and the reason."""

    why: str
    #: The server stopped answering, as against answering with an error.
    unreachable: bool = False
    #: The cache refused the master's bytes: the server may be fine.
    unwritable: bool = False


class _CacheRefused(Exception):
    """A write to the cache failed. **Not an `OSError`**, so `_UNREACHABLE` never reads it as the server gone."""

    def __init__(self, cause: OSError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _on_disk[**P, T](action: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run one file operation, its `OSError` raised as `_CacheRefused`."""
    try:
        return action(*args, **kwargs)
    except OSError as exc:
        raise _CacheRefused(exc) from exc


def _declared_size(document: dict[str, Any], work_id: str) -> int | None:
    """The byte count the feed declares for a work's master, or None if it declares none this reader can use."""
    declared = document["works"][work_id]["media"].get("bytes")
    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 1:
        return None
    return declared


async def _stream_to(response: aiohttp.ClientResponse, path: Path, limit: int) -> str | None:
    """Write a body to `path` under a running hash: its SHA-256, or None once it runs past `limit` bytes.

    The file is removed when the body is refused or the transfer fails, so a
    master is on disk whole or not at all, and never more than `limit` and one
    chunk of it.
    """
    digest = hashlib.sha256()
    received = 0
    try:
        # Each file operation is the cache's, said as `_CacheRefused`; the
        # body's own errors are the server's, and stay as they come.
        with _on_disk(path.open, "wb") as handle:
            async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
                received += len(chunk)
                if received > limit:
                    response.close()
                    _discard(path)
                    return None
                digest.update(chunk)
                _on_disk(handle.write, chunk)
            _on_disk(handle.flush)
            _on_disk(os.fsync, handle.fileno())
    except BaseException:  # prawduct:allow prawduct/broad-except -- cleanup-and-reraise; the partial file must go on any exit
        # Quietly: a cache that refused the write may refuse the unlink, and
        # that second refusal must not hide the first.
        _discard_quietly(path)
        raise
    return digest.hexdigest()


def _discard(path: Path) -> None:
    path.unlink(missing_ok=True)


def _discard_quietly(path: Path) -> None:
    """`_discard`, on a cache that may refuse the unlink too."""
    with contextlib.suppress(OSError):
        _discard(path)


async def _read_capped(response: aiohttp.ClientResponse, limit: int) -> bytes | None:
    """A document's body, or None once it runs past `limit` bytes, read no further than one chunk past it."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def _text_of(body: bytes | None, what: str, limit: int) -> str:
    """A capped read as text. Raises `DocumentTooLarge` for one past the cap, `UnicodeDecodeError` for one that is not UTF-8."""
    if body is None:
        raise DocumentTooLarge(f"the {what} runs past {limit} bytes, the most this Player reads")
    return body.decode("utf-8")


def _holds(path: Path, data: bytes) -> bool:
    """Whether the file at `path` already holds exactly `data`."""
    try:
        return path.read_bytes() == data
    except OSError:
        return False


async def _rest(stop: asyncio.Event, seconds: float, beside: asyncio.Task[None]) -> None:
    """Wait `seconds`, or less if asked to stop or if the task beside this loop ends."""
    stopping = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({stopping, beside}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()


def _same_origin(address: str, server: str) -> bool:
    first, second = urlsplit(address), urlsplit(server)
    return (first.scheme, first.netloc) == (second.scheme, second.netloc)


def _write_atomically(path: Path, data: bytes) -> None:
    """Replace a cache file in one step, so the watcher never reads half of one."""
    write_whole(path, data, temporary=path.with_name(f".{path.name}.tmp"))
