"""The Player as a client: one process, one worker per wall the server assigns it.

`clients.md` § The Player. A client is configured with the server, its token and
its cache, and nothing about walls. It asks the server which walls it drives
(`GET /client`, about every 30 seconds), starts a worker for each on the output
the curator chose, stops the worker for a wall taken away, and tells the server
which outputs it has (`POST /client/heartbeat`) so a curator can choose one by
name. `player-contract.md` § Transport is the specification of both routes.

**Labels are run the same way, beside the walls** (`labels-and-surfaces.md`).
The client reports its label outputs too, and the document names which of them
caption a wall; the supervisor runs one label renderer per mapped label output,
keyed on the output, and stops it when the mapping goes. A label's wall need not
be one this client shows.

**This module speaks no HTTP.** The two requests are made by `pull.py`, the one
module the isolation guard lets hold a client, and handed in by the entry
point as a `ClientLink`. So is the worker itself: what runs a wall on the Frame
names the television's library, and only the composition root may.

**Every failure keeps the walls running** (`player-contract.md`: every failure
keeps the cache). A server that cannot be reached, refuses this client's token
or sends a document this reader cannot use changes nothing: the walls already
running go on rotating their caches. The last good document is kept on disk
(`ClientLink.cached`), so a client restarted while the server is down starts
the walls it last knew rather than none.

**The walls follow the outputs as well as the document.** A card the kernel
enumerates after the Player starts, or a connector it stops listing, changes
which walls this client can place while the server, which has nothing new to
say, answers 304. So the last document is kept, and the walls are matched to it
again whenever the outputs differ from the ones it was last matched against.

**A wall that leaves its screen leaves it dark; a client that stops does not.**
A worker is told why it was stopped (`WorkerStop`): retired, when the document no
longer places its wall on that output, so a screen wall draws black there rather
than going on showing a work the server now says nobody shows; or merely
stopped, when the client is going down or the output itself is gone, which
leave the screen as it is. A label renderer is told the same way, by the same
type: retired when its label output is no longer mapped, so the panel is left
blank.

**A label's cached document goes when nothing maps its label any more.** The
supervisor hands the ids the current document maps to the entry point's
`forget_labels` at each match and as each renderer finishes stopping; it is
never called before a document has been read.

**A wall's directory goes a week after the wall left this client.** A wall
moved to another Player, or deleted, would otherwise leave its feed, masters,
compositions and heartbeat under `CACHE_DIR` for ever. Its directory is stamped
when a client document is read that does not place it here, and removed once
this client has gone on reading documents that do not, for a week by the
monotonic clock (`WallCaches`); a wall that comes back first is un-stamped and
loses nothing.

**Both caches are swept only on a document that was read.** The label cache
and the walls' directories are swept against the current document, never
before one has been read and parsed: a client that cannot read its document
does not know which walls and labels it has, and reading that as "none" would
delete every one (`Supervisor._read_document`).

**Every line a worker logs names its wall.** The supervisor binds the wall's
id around everything a wall's worker runs, and a label's id and its wall's
around everything its renderer runs (`logs.wall_context`, `logs.label_context`),
so a line logged three calls deep in a pull or a drawing thread says which wall
it is about. The supervisor's own lines about the client say none.

**A worker that fails is restarted here rather than taking the process down.**
One process drives every wall this client has, so a fault in one wall's worker
— its pull dying on a full disk, its television's library raising something
nobody predicted — must not blank the others. Each crash is logged at ERROR with
its traceback, and the worker is started again after a wait that doubles to a
ceiling. The supervisor's own failure is not caught: that ends the process, and
systemd restarts it.
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, Final, Protocol

from arrt_player import heartbeat as heartbeat_module
from arrt_player.config import (
    ALLOWED_HOSTS_SETTING,
    CACHE_DIR_NAMES,
    CACHED_MANIFEST_FILENAME,
    CLIENT_DOCUMENT_FILENAME,
    COMPOSED_DIRNAME,
    FRAME_OUTPUT,
    PLAIN_COMPONENT,
    STATE_FILENAME,
    ClientSettings,
    UiSettings,
    WallIdUnusable,
    WallSettings,
    normal_host_name,
)
from arrt_player.episodes import Backoff, ReportOnce
from arrt_player.logs import known_as, label_context, wall_context
from arrt_player.manifest import MEDIA_DIRNAME
from arrt_player.safe_write import write_whole

log = logging.getLogger(__name__)

#: Where the kernel lists display connectors. Injectable everywhere it is read,
#: so the suite reads a directory it built rather than the machine it runs on.
DRM_ROOT: Final[Path] = Path("/sys/class/drm")

#: An HDMI connector's sysfs directory: `card0-HDMI-A-1`. The card number is
#: dropped from the output's name, because it is the driver's enumeration order
#: rather than anything a person plugged a cable into, and it can change across a
#: kernel update while the socket on the board does not.
_HDMI_CONNECTOR: Final = re.compile(r"card\d+-HDMI-A-(\d+)")

#: A mode as `modes` lists it: `1920x1080`, sometimes with a suffix (`i`).
_MODE: Final = re.compile(r"(\d+)x(\d+)")

#: The two output kinds the client heartbeat names.
FRAME_KIND: Final[str] = "frame"
FRAMEBUFFER_KIND: Final[str] = "framebuffer"
#: The one label output kind the client heartbeat names.
EPAPER_KIND: Final[str] = "epaper"

#: How long the Frame's identity read may take before this report goes without it.
#: The read has its own shorter bound inside the television's seam; this one is
#: the supervisor's, so no seam can hold a client poll for longer.
IDENTITY_READ_BUDGET_SECONDS: Final[float] = 5.0

#: Bounds on the wait before a crashed worker is started again. Short enough
#: that a passing fault costs the wall seconds; capped so a worker that fails
#: on every start costs a line every five minutes rather than a line a second.
RESTART_MIN_SECONDS: Final[float] = 5.0
RESTART_MAX_SECONDS: Final[float] = 300.0


# -- the client document ---------------------------------------------------------------


class ClientDocumentUnreadable(Exception):
    """A `GET /client` answer this reader will not act on."""


@dataclass(frozen=True)
class Assignment:
    """One wall this client drives, and the output it is shown on."""

    wall_id: str
    name: str
    output: str
    #: The server's id for the display behind `output`, or None from a server
    #: that does not name one. A worker is keyed on the output, never on this.
    display: str | None = None


@dataclass(frozen=True)
class LabelAssignment:
    """One of this client's label outputs, and the wall it captions."""

    label_id: str
    output: str
    wall_id: str


@dataclass(frozen=True)
class ClientDocument:
    """What `GET /client` answered: this client, its walls, and its labels."""

    client_id: str
    name: str
    walls: tuple[Assignment, ...]
    #: Empty from a server that names no labels, which is the same as none.
    labels: tuple[LabelAssignment, ...] = ()


def player_name(cache_dir: Path, local: str) -> str:
    """The name this Player goes by: the server's, once it has given one, else `PLAYER_NAME`.

    **The server's wins** (owner, 2026-11-10): a Player that has paired with a
    server is called what the server calls it, read from the last client
    document cached under `cache_dir`, so a rename on the server reaches here on
    the next poll with nothing written locally. `local` is the name of a Player
    that has no server yet, or whose cached document cannot be read; empty when
    it has none either.
    """
    try:
        return parse_client_document((cache_dir / CLIENT_DOCUMENT_FILENAME).read_text(encoding="utf-8")).name
    except (OSError, UnicodeDecodeError, ClientDocumentUnreadable):
        return local


def parse_client_document(text: str) -> ClientDocument:
    """Read a client document (`contract/schemas/client.v1.schema.json`), or refuse it.

    **Refused whole rather than read in part.** A document that names a wall
    without its output, or whose walls are not a list, is one where acting on the
    readable part would stop workers for every wall the unreadable part named. So
    a refusal keeps the walls already running, which is what every other failure
    here does too. Unknown keys are allowed, as the schema allows them.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientDocumentUnreadable(f"the client document is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ClientDocumentUnreadable(f"the client document is a {type(document).__name__}, not an object")
    client_id, name, walls = document.get("client_id"), document.get("name"), document.get("walls")
    if not _text(client_id) or not _text(name):
        raise ClientDocumentUnreadable("the client document carries no client_id or name")
    if not isinstance(walls, list):
        raise ClientDocumentUnreadable("the client document's walls are not a list")
    assignments = []
    for position, wall in enumerate(walls):
        if not isinstance(wall, dict):
            raise ClientDocumentUnreadable(f"wall {position} is a {type(wall).__name__}, not an object")
        for key in ("wall_id", "name", "output"):
            if not _text(wall.get(key)):
                raise ClientDocumentUnreadable(f"wall {position} carries no {key}")
        if "display" in wall and not _text(wall["display"]):
            raise ClientDocumentUnreadable(f"wall {position} names its display without an id")
        assignments.append(
            Assignment(wall_id=wall["wall_id"], name=wall["name"], output=wall["output"], display=wall.get("display"))
        )
    return ClientDocument(client_id=client_id, name=name, walls=tuple(assignments), labels=_labels(document))


def _labels(document: dict) -> tuple[LabelAssignment, ...]:
    """The document's labels, absent read as none, and refused whole like its walls."""
    labels = document.get("labels", [])
    if not isinstance(labels, list):
        raise ClientDocumentUnreadable("the client document's labels are not a list")
    read = []
    for position, label in enumerate(labels):
        if not isinstance(label, dict):
            raise ClientDocumentUnreadable(f"label {position} is a {type(label).__name__}, not an object")
        for key in ("label_id", "output", "wall_id"):
            if not _text(label.get(key)):
                raise ClientDocumentUnreadable(f"label {position} carries no {key}")
        read.append(LabelAssignment(label_id=label["label_id"], output=label["output"], wall_id=label["wall_id"]))
    return tuple(read)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


# -- outputs -------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputReport:
    """One output this client can drive, as the client heartbeat states it."""

    name: str
    kind: str
    connected: bool
    #: `(width, height)` in pixels, or None when the client does not know it.
    screen: tuple[int, int] | None
    #: Who the display is, as read from the device itself; None for an output
    #: with no identity a client can read, and then the key is left out.
    identity: str | None = None

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "connected": self.connected,
            "screen": list(self.screen) if self.screen is not None else None,
        }
        if self.identity is not None:
            document["identity"] = self.identity
        return document


@dataclass(frozen=True)
class LabelOutputReport:
    """One label output this client holds, as the client heartbeat states it."""

    name: str
    kind: str
    #: Whether the panel answers now: false for one that would not open, and
    #: for one whose last draw failed.
    connected: bool
    #: `(width, height)` in pixels, or None when the client does not know it.
    size: tuple[int, int] | None

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "connected": self.connected,
            "size": list(self.size) if self.size is not None else None,
        }


class FrameIdentity:
    """The Frame's device id, read again once per heartbeat interval and kept through a failed read.

    **Read by the client, not by a wall's worker**, because a Frame with no wall on
    it must still say who it is: a Frame moved to a new client has no wall there
    yet, and its walls follow it only once the server hears its identity
    (`player-contract.md` § Transport). The read is the television seam's
    `read_identity`, which opens no art channel and presses no key.

    **Asked on each report until it answers, then once per heartbeat interval**,
    so a set asleep at boot is identified once it wakes, and another set put at
    the Frame's address is reported within one interval, without a restart: one
    REST GET a minute. A failed read keeps the last identity read, so it does not
    flap away while the set sleeps. A failure is said once per episode, and a
    changed identity once per change.
    """

    def __init__(
        self,
        read: Callable[[], Awaitable[str]],
        *,
        budget_seconds: float = IDENTITY_READ_BUDGET_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        interval_seconds: float = heartbeat_module.INTERVAL_SECONDS,
    ) -> None:
        self._read = read
        self._budget = budget_seconds
        self._monotonic = monotonic
        self._interval = interval_seconds
        self.value: str | None = None
        self._asked_at: float | None = None
        self._unreadable = ReportOnce()

    async def refresh(self) -> None:
        """Read the identity if it is not yet known or its interval has come round. Never raises."""
        now = self._monotonic()
        if self.value is not None and self._asked_at is not None and now - self._asked_at < self._interval:
            return
        self._asked_at = now
        try:
            read = await asyncio.wait_for(self._read(), timeout=self._budget)
            if not isinstance(read, str) or not read:
                raise ValueError("the read answered no device id")  # noqa: TRY301 -- one way the read fails among the rest
        # An identity is an annotation: whatever stops the read, the Frame is reported with the last one, or none.
        except Exception as exc:  # noqa: BLE001  # prawduct:allow prawduct/broad-except -- see above
            if self._unreadable.begin():
                log.warning(
                    "the Frame's device id could not be read (%s); %s",
                    f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                    (
                        "it is reported without an identity until it can be"
                        if self.value is None
                        else "it is reported with the last one read until it can be read again"
                    ),
                    extra={"event": "client.identity_unreadable", "output": FRAME_OUTPUT},
                )
            return
        if self._unreadable.end():
            log.info("the Frame's device id was read", extra={"event": "client.identity_read", "output": FRAME_OUTPUT})
        if self.value is not None and read != self.value:
            log.warning(
                "the set at the Frame's address is another set now (%s, was %s); it is reported as the new one",
                known_as(read),
                known_as(self.value),
                extra={
                    "event": "client.identity_changed",
                    "output": FRAME_OUTPUT,
                    "set_now": known_as(read),
                    "set_was": known_as(self.value),
                },
            )
        self.value = read


def hdmi_outputs(drm_root: Path = DRM_ROOT) -> list[OutputReport]:
    """The HDMI connectors the kernel lists, named `hdmi-a-1`, `hdmi-a-2`, connected or not.

    **A connector with nothing plugged in is still listed**, as `connected:
    false`: it is an output a curator may place a wall on before the screen
    arrives, and the worker draws once it does. `connected` is the connector's
    `status` reading `connected`; anything else, `unknown` included, is false,
    because a screen this client cannot confirm is not one it should claim. The
    screen is the first line of `modes`, which the kernel lists preferred mode
    first, and None when nothing is connected or no mode is listed.

    A machine with no DRM directory (a Mac, a container) has no HDMI outputs, and
    that is an answer rather than an error.
    """
    found: dict[str, OutputReport] = {}
    try:
        connectors = sorted(drm_root.iterdir())
    except OSError:
        return []
    for connector in connectors:
        matched = _HDMI_CONNECTOR.fullmatch(connector.name)
        if matched is None:
            continue
        name = f"hdmi-a-{matched.group(1)}"
        if name in found:
            # Two cards exposing the same connector number. The heartbeat may not
            # carry one name twice, and the first card is the one the board's
            # display controller enumerates first.
            continue
        connected = _read_first_line(connector / "status") == "connected"
        found[name] = OutputReport(
            name=name,
            kind=FRAMEBUFFER_KIND,
            connected=connected,
            screen=_mode(_read_first_line(connector / "modes")) if connected else None,
        )
    return list(found.values())


def _read_first_line(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def _mode(line: str) -> tuple[int, int] | None:
    matched = _MODE.match(line)
    if matched is None:
        return None
    width, height = int(matched.group(1)), int(matched.group(2))
    return (width, height) if width > 0 and height > 0 else None


def client_outputs(
    settings: ClientSettings, *, drm_root: Path = DRM_ROOT, frame_identity: str | None = None
) -> list[OutputReport]:
    """Every output this client can drive: the Frame when it has one, and its HDMI connectors.

    **The Frame is reported connected because it is configured**, with no screen
    size. It is a television on the network rather than a cable this host can
    sense, and whether it answers is a fact about the wall shown on it, which
    that wall's own heartbeat reports (`television_reachable`). Its size is the
    wall's to report: this Player composes for the panel it is configured with,
    and the wall's heartbeat carries it (`capabilities`). Its identity is what
    `FrameIdentity` has read, if anything yet.
    """
    outputs = (
        [OutputReport(name=FRAME_OUTPUT, kind=FRAME_KIND, connected=True, screen=None, identity=frame_identity)]
        if settings.frame
        else []
    )
    return outputs + hdmi_outputs(drm_root)


def client_heartbeat(
    outputs: list[OutputReport],
    *,
    reported_at: datetime,
    label_outputs: list[LabelOutputReport] | None = None,
    settings_url: str | None = None,
) -> dict[str, Any]:
    """The client heartbeat (`contract/schemas/client-heartbeat.v1.schema.json`).

    `label_outputs` is left out for a client that holds none, which is how the
    schema reads a client with no label outputs; `settings_url` is left out when
    it is not known, because the schema allows it absent and never null.
    """
    heartbeat: dict[str, Any] = {
        heartbeat_module.REPORTED_AT_KEY: reported_at.isoformat(),
        "outputs": [output.document() for output in outputs],
    }
    if label_outputs:
        heartbeat["label_outputs"] = [output.document() for output in label_outputs]
    if settings_url is not None:
        heartbeat["settings_url"] = settings_url
    return heartbeat


def settings_url(ui: UiSettings, local_address: str | None) -> str | None:
    """Where a person on the server's network reaches this Player's settings page, or None when it cannot be said.

    **None when the page is bound to loopback**: no one else can reach it, and a
    link would point at the server's own machine. Otherwise the first name in
    `UI_ALLOWED_HOSTS`, which is the operator's word for it; else the address the
    page is bound to, when it is bound to one; else, for a page on every
    interface, the address this Player reached the server from
    (`pull.ClientPull.local_address`), which is on the server's network by
    construction. Always plain `http` on `UI_PORT`, which is how the page serves.

    The first name is read from the environment `load_ui` read, in the order it
    was written there: `UiSettings.allowed_hosts` is a set, and keeps no first.
    An address the page could not be reached at is None rather than a guess: an
    IPv6 address for a page bound to IPv4's every interface, a link-local
    address (it needs an interface no other machine shares), or a bind to a
    name rather than an address.
    """
    bind = ipaddress.ip_address(ui.bind) if _is_address(ui.bind) else None
    if ui.bind.lower() == "localhost" or (bind is not None and bind.is_loopback):
        return None
    listed = [part.strip() for part in (ui.environment.get(ALLOWED_HOSTS_SETTING) or "").split(",") if part.strip()]
    if listed:
        return _page_at(normal_host_name(listed[0]), ui.port)
    if bind is None:
        return None
    host = str(bind) if not bind.is_unspecified else _reachable(local_address, bind)
    return _page_at(host, ui.port) if host is not None else None


def _reachable(local_address: str | None, bind: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """The local address, as a page bound to every interface of `bind`'s family can be reached at, or None."""
    if local_address is None or not _is_address(local_address):
        return None
    local = ipaddress.ip_address(local_address)
    if isinstance(local, ipaddress.IPv6Address) and local.ipv4_mapped is not None:
        local = local.ipv4_mapped
    if local.is_link_local or local.version > bind.version:
        return None
    return str(local)


def _is_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _page_at(host: str, port: int) -> str:
    return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"


# -- the walls' directories ------------------------------------------------------------

#: How long a wall's directory outlives the wall's leaving this client: a week
#: of this client reading documents that do not place it here. A wall moved
#: away and back within it keeps its masters, pictures and Frame bindings.
WALL_CACHE_KEPT_SECONDS: Final[float] = 7 * 24 * 60 * 60

#: The stamp in a wall's directory that says the wall has left, when, and for
#: how long this client has seen it gone. **A leading dot**, so no master,
#: composition or heartbeat can be named it.
LEFT_STAMP_FILENAME: Final[str] = ".left.json"

#: How much more absence a stamp sees before it is written again. Each write is
#: a write to the Pi's card, so the count is kept in memory and written this
#: often; a restart loses at most this much of it, which only delays a removal.
LEFT_STAMP_REWRITE_SECONDS: Final[float] = 15 * 60

#: The most absence one sweep may add to a stamp. A sweep follows each poll, so
#: a longer gap is a loop that stalled or a monotonic clock that leapt, and
#: neither is a week of documents read; the week is made of sweeps.
LEFT_CREDIT_PER_SWEEP_SECONDS: Final[float] = 10 * 60

#: What a wall's directory is renamed to, directly under `CACHE_DIR`, before it
#: is deleted. **The rename is the removal**: it happens in one step on the event
#: loop, so a worker started for the wall a moment later makes a new directory
#: rather than writing into one a thread is deleting. A leading dot, so no wall
#: can be named it; one a failed deletion left is deleted again at the next sweep.
REMOVING_PREFIX: Final[str] = ".removing-"


def _wall_markers(name: str) -> tuple[str, ...]:
    """The names only a wall's directory holds, for a directory named `name`.

    Derived from the constants that name them, so a directory is recognised
    as a wall's by what the Player wrote there and never by its name alone: a
    plain-named directory somebody else put under `CACHE_DIR` is left alone.
    """
    return (
        CACHED_MANIFEST_FILENAME,
        MEDIA_DIRNAME,
        COMPOSED_DIRNAME,
        STATE_FILENAME,
        heartbeat_module.path_in(Path(), name).name,
        LEFT_STAMP_FILENAME,
    )


#: `config.CACHE_DIR_NAMES` as a wall id is compared with them: without case.
_RESERVED: Final[frozenset[str]] = frozenset(name.casefold() for name in CACHE_DIR_NAMES)


@dataclass
class _Stamp:
    #: When the wall was first seen gone, by the wall clock: for the journal.
    left_at: datetime
    #: How long this client has seen it gone, by the monotonic clock: the rule.
    absent_seconds: float
    #: `absent_seconds` as last written to disk.
    written_seconds: float


class WallCaches:
    """Removes the directory of a wall that left this client, a week after it left.

    **Swept against a document that was read, and only then** (the supervisor
    calls `sweep` only while it holds one). Given every wall that document
    places here, each directory under `CACHE_DIR` that is a wall's and is not
    one of them is stamped at first sight; a stamped one is removed once this
    client has seen it gone for `kept_seconds`; one whose wall is placed here
    again has its stamp removed.

    **Measured by the monotonic clock, and only while documents are being read.**
    The time since the last sweep, at most `credit_seconds`, is added to each
    stamp at every sweep, and a process's first sweep adds none. So a wall clock
    that jumps forward expires nothing early, nor does a stalled loop or a
    leaping monotonic clock; a Player switched off for a month resumes its week
    from its stamp when it comes back, the month counting for nothing and at most
    `rewrite_seconds` of the week lost; and a client that cannot read a document
    adds nothing at all. The wall clock is
    written into the stamp to say when, never to decide.

    **What it may remove:** a real directory directly under `CACHE_DIR`, whose
    name a wall id may take (`config.PLAIN_COMPONENT`, and none of
    `config.CACHE_DIR_NAMES`, without case), that holds one of the names only a
    wall's directory holds (`_wall_markers`). A symlink is never followed and
    never removed; a dotted name (the client document, its ETag, the label
    cache, the settings, the slideshow's files, a write in progress) is outside
    what a wall id can be called and is passed over in silence; anything else a
    wall id could be called that is not a wall's directory is left and said
    once. A wall's masters live in its own directory, so removing one wall's
    never touches a master another wall holds.

    **Never raises** for the disk: a directory that cannot be read, stamped or
    removed costs disk, and is said once per episode.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
        kept_seconds: float = WALL_CACHE_KEPT_SECONDS,
        rewrite_seconds: float = LEFT_STAMP_REWRITE_SECONDS,
        credit_seconds: float = LEFT_CREDIT_PER_SWEEP_SECONDS,
    ) -> None:
        self._root = cache_dir
        self._credit = credit_seconds
        self._now = now
        self._monotonic = monotonic
        self._kept = kept_seconds
        self._rewrite = rewrite_seconds
        self._stamps: dict[str, _Stamp] = {}
        self._swept_at: float | None = None
        self._said_unknown: set[str] = set()
        self._unsweepable = ReportOnce()

    async def sweep(self, placed: Collection[str]) -> None:
        """One pass, given every wall the current document places here (and any still running or closing).

        Everything but the deletion is done here on the loop, in one step: a
        directory to go is renamed out of its wall's path before this awaits, so
        nothing a worker starts afterwards can land in what is being deleted.
        """
        failed, doomed = self._pass(placed)
        for path in doomed:
            try:
                await asyncio.to_thread(shutil.rmtree, path)
            except OSError as exc:
                failed = True
                self._say_unsweepable(path, exc)
        if not failed and self._unsweepable.end():
            log.info("the walls' directories can be swept again", extra={"event": "client.wall_cache_sweepable"})

    def _pass(self, placed: Collection[str]) -> tuple[bool, list[Path]]:
        """Stamp, un-stamp and rename what this pass finds. Whether any of it failed, and what to delete."""
        swept_at = self._monotonic()
        elapsed = 0.0 if self._swept_at is None else min(max(0.0, swept_at - self._swept_at), self._credit)
        self._swept_at = swept_at
        kept = {wall_id.casefold() for wall_id in placed}
        try:
            entries = sorted(self._root.iterdir())
        except FileNotFoundError:
            return False, []
        except OSError as exc:
            self._say_unsweepable(self._root, exc)
            return True, []
        failed, doomed = False, []
        for entry in entries:
            try:
                gone = self._consider(entry, kept, elapsed)
            except OSError as exc:
                failed = True
                self._say_unsweepable(entry, exc)
                continue
            if gone is not None:
                doomed.append(gone)
        return failed, doomed

    def _consider(self, entry: Path, kept: set[str], elapsed: float) -> Path | None:
        """What this pass does with one entry directly under `CACHE_DIR`: the path to delete, if any."""
        name = entry.name
        if name.startswith(REMOVING_PREFIX):
            # A deletion a failure or a stop left unfinished.
            return entry if entry.is_dir() and not entry.is_symlink() else None
        if not PLAIN_COMPONENT.fullmatch(name) or name.casefold() in _RESERVED:
            return None
        if name.casefold() in kept:
            self._returned(entry)
            return None
        if entry.is_symlink() or not entry.is_dir() or not any(os.path.lexists(entry / marker) for marker in _wall_markers(name)):
            self._say_unknown(entry)
            return None
        return self._age(entry, elapsed)

    def _age(self, entry: Path, elapsed: float) -> Path | None:
        """A wall's directory no document places here: stamped, aged by `elapsed`, or taken away once it is old enough."""
        name = entry.name
        stamp = self._stamps.get(name)
        if stamp is None:
            stamp = self._read_stamp(entry)
            if stamp is None:
                self._stamp(entry)
                return None
            self._stamps[name] = stamp
        else:
            stamp.absent_seconds += elapsed
        if stamp.absent_seconds >= self._kept:
            return self._remove(entry, stamp)
        if stamp.absent_seconds - stamp.written_seconds >= self._rewrite:
            self._write_stamp(entry, stamp)
        return None

    def _stamp(self, entry: Path) -> None:
        stamp = _Stamp(left_at=self._now(), absent_seconds=0.0, written_seconds=0.0)
        self._write_stamp(entry, stamp)
        self._stamps[entry.name] = stamp
        log.info(
            "wall %s is not this client's now; its directory is removed after %s unless it comes back",
            entry.name,
            timedelta(seconds=self._kept),
            extra={"event": "client.wall_cache_stamped", "wall_id": entry.name, "path": str(entry)},
        )

    def _returned(self, entry: Path) -> None:
        had = self._stamps.pop(entry.name, None) is not None
        stamp_path = entry / LEFT_STAMP_FILENAME
        if not entry.is_symlink() and os.path.lexists(stamp_path):
            stamp_path.unlink()
            had = True
        if had:
            log.info(
                "wall %s is this client's again; its directory is kept",
                entry.name,
                extra={"event": "client.wall_cache_returned", "wall_id": entry.name, "path": str(entry)},
            )

    def _remove(self, entry: Path, stamp: _Stamp) -> Path:
        """Take a wall's directory out of its path, and say so. Returns where it went, to be deleted."""
        doomed = self._root / f"{REMOVING_PREFIX}{entry.name}.{uuid.uuid4().hex}"
        entry.rename(doomed)
        self._stamps.pop(entry.name, None)
        log.info(
            "removed the directory of wall %s, which left this client at %s",
            entry.name,
            stamp.left_at.isoformat(),
            extra={
                "event": "client.wall_cache_removed",
                "wall_id": entry.name,
                "path": str(entry),
                "left_at": stamp.left_at.isoformat(),
            },
        )
        return doomed

    def _read_stamp(self, entry: Path) -> _Stamp | None:
        """The stamp in `entry`, or None when there is none this reader can use: it is then stamped afresh."""
        try:
            document = json.loads((entry / LEFT_STAMP_FILENAME).read_text(encoding="utf-8"))
            left_at = datetime.fromisoformat(document["left_at"])
            absent = float(document["absent_seconds"])
        except FileNotFoundError:
            return None
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            # A stamp cut short or from another shape. Stamping afresh only
            # delays the removal, which is the side to be wrong on.
            return None
        if left_at.tzinfo is None or not absent >= 0:
            return None
        return _Stamp(left_at=left_at, absent_seconds=absent, written_seconds=absent)

    def _write_stamp(self, entry: Path, stamp: _Stamp) -> None:
        path = entry / LEFT_STAMP_FILENAME
        temporary = entry / f".{LEFT_STAMP_FILENAME}.tmp"
        data = json.dumps({"left_at": stamp.left_at.isoformat(), "absent_seconds": stamp.absent_seconds}).encode("utf-8")
        # Forced to the card before the rename: the stamp is the only record of
        # when the wall left, and a power cut must leave the old one or the new,
        # whole.
        write_whole(path, data, temporary=temporary)
        stamp.written_seconds = stamp.absent_seconds

    def _say_unknown(self, entry: Path) -> None:
        if entry.name in self._said_unknown:
            return
        self._said_unknown.add(entry.name)
        log.info(
            "%s is under CACHE_DIR and is not a wall's directory this client recognises; it is left as it is",
            entry,
            extra={"event": "client.cache_entry_unknown", "path": str(entry)},
        )

    def _say_unsweepable(self, path: Path, exc: OSError) -> None:
        if self._unsweepable.begin():
            log.warning(
                "the directories of walls that left this client cannot be swept (%s: %s); they stay until they can",
                path,
                exc,
                extra={"event": "client.wall_cache_unsweepable", "path": str(path)},
            )


# -- the supervisor --------------------------------------------------------------------


def _why_stopped(output: str, placed_on: str | None, available: dict[str, Any], claimed: dict[str, str]) -> tuple[bool, str]:
    """Whether a wall's worker on `output` is retired, and why it is stopped, for the journal.

    **Retired only when the document moves the wall off this output.** One it
    still places here (`placed_on`) is stopped and its screen left as it is:
    either the output is gone from this client, and the wall starts there again
    when it comes back, or another wall the server also put there is about to
    draw on it.
    """
    if placed_on is None:
        return True, "no longer assigned to this client"
    if placed_on != output:
        return True, f"moved to {placed_on}"
    if output not in available:
        return False, f"on {output}, which this client no longer lists"
    return False, f"on {output}, where wall {claimed[output]} is shown"


class ReportAnswer(StrEnum):
    """What the server last answered this Player's client heartbeat, as far as it bears on the Player's state.

    Only the answers that say something of the Player on the server's side are
    told apart: the server took the report, it refused it as one it cannot read,
    or it refused the token it came with. Everything else (no answer, a `5xx`, a
    `408` or `429`, an answer that is not the server's, a refusal for its size)
    leaves the server's view of the Player as it was, so it is one answer here:
    judged by the last report it took, or still unreadable after a refusal, which
    the server says until it takes a report (`ClientPull.last_answer`).
    """

    #: `204`: the server holds this report.
    ACCEPTED = "accepted"
    #: `400` with the server's error body: the server's verdict that it cannot
    #: read the report, which it says of this Player until it takes one.
    UNREADABLE = "unreadable"
    #: `401`: the server does not know the token, so it cannot say which Player sent it.
    TOKEN_REFUSED = "token_refused"  # noqa: S105 -- an answer's name, not a secret
    #: Anything else: the server's view of this Player is still its last report taken.
    NOT_TAKEN = "not_taken"


#: How many heartbeat intervals a report counts as current for, the contract's
#: `stale_after_intervals` (`contract/timing.json`). Past it the server says a
#: Player is not heard from, and so does the Player of itself.
STALE_AFTER_INTERVALS: Final[int] = 3

#: How old the last report taken may be while the server still calls the Player reporting.
STALE_AFTER_SECONDS: Final[float] = heartbeat_module.INTERVAL_SECONDS * STALE_AFTER_INTERVALS


@dataclass(frozen=True, slots=True)
class HeardReport:
    """What this Player knows of how the server last heard it: its last answer, and the last report taken."""

    #: What the server's answers to the client heartbeat say of this Player now
    #: (`ClientPull.last_answer`), or None before any was answered.
    answer: ReportAnswer | None
    #: The instant the last report the server took was made (its `reported_at`),
    #: or None while none has been taken since this process started.
    accepted_at: datetime | None = None
    #: How long ago that report was taken, by the monotonic clock, so a clock set
    #: right after boot cannot age it; None with `accepted_at`.
    accepted_age_seconds: float | None = None


class ClientLink(Protocol):
    """The two client routes, as the supervisor needs them. `pull.ClientPull` is the real one."""

    def cached(self) -> ClientDocument | None:
        """The last good document, from before this process started, or None."""

    async def fetch(self) -> ClientDocument | None:
        """A new document when the server sent one this reader accepts, else None: keep what you have."""

    async def report(self, heartbeat: dict[str, Any]) -> bool:
        """POST the client heartbeat. True when the server took it."""

    @property
    def last_answer(self) -> ReportAnswer | None:
        """What the server's answers to `report` say of this Player now (`ClientPull.last_answer`), or None before one."""

    async def close(self) -> None: ...


class WorkerStop(asyncio.Event):
    """The event that stops a wall's worker or a label's renderer, and why it was set.

    **The one way either is told it is retired.** The reason rides on the event,
    so a worker that has no use for it need not take it, and `retired` is set
    before the event is, so whatever wakes on the event reads the reason it was
    set for. It is true when the document no longer places what the worker runs
    on its output: a wall taken away or moved to another output, or a label
    output no longer mapped at all. It is false when the client is stopping, or
    when the document still places it there: an output gone from this client,
    one the server also gave another wall, or a label output now mapped to
    another wall or label. Once true it stays true.

    **Called once more, already stopped and retired.** A retirement that lands
    while the worker waits to be restarted after a failure finds nothing running
    to see it. So the supervisor calls the worker once more with this event
    already set and `retired` true, so that what it does on leaving its output
    (a screen drawn black, a label panel left blank) still happens; it must then
    do only that, and return, saying nothing of starting. A stop that is not a
    retirement is never followed by such a call.
    """

    def __init__(self) -> None:
        super().__init__()
        self.retired = False

    def retire(self) -> None:
        """Stop the worker because what it runs has left its output."""
        self.retired = True
        self.set()


def is_retired(stop: asyncio.Event) -> bool:
    """Whether `stop` stopped a worker because what it runs left its output. False for a plain event."""
    return isinstance(stop, WorkerStop) and stop.retired


#: What runs one wall: given its settings, the output it is on, and the event
#: that stops it (a `WorkerStop`), run until stopped. It may raise; the
#: supervisor restarts it. Called once more as `WorkerStop` says.
Worker = Callable[[WallSettings, OutputReport, WorkerStop], Awaitable[None]]

#: What runs one label output: given its assignment and the event that stops it
#: (a `WorkerStop`), run until stopped. It may raise; the supervisor restarts it.
#: Called once more as `WorkerStop` says.
LabelWorker = Callable[[LabelAssignment, WorkerStop], Awaitable[None]]

#: Given every label id the current client document maps, remove what this
#: client cached for any other. Never raises.
ForgetLabels = Callable[[frozenset[str]], None]


@dataclass
class _Running:
    assignment: Assignment
    stop: WorkerStop
    task: asyncio.Task[None]


@dataclass
class _RunningLabel:
    assignment: LabelAssignment
    stop: WorkerStop
    task: asyncio.Task[None]


class Supervisor:
    """Keeps one worker running per wall the server assigns this client, on its output."""

    def __init__(
        self,
        *,
        settings: ClientSettings,
        link: ClientLink,
        worker: Worker,
        outputs: Callable[[], list[OutputReport]],
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
        label_worker: LabelWorker | None = None,
        label_outputs: Callable[[], list[LabelOutputReport]] = list,
        forget_labels: ForgetLabels | None = None,
        wall_caches: WallCaches | None = None,
        frame_identity: FrameIdentity | None = None,
        settings_url: Callable[[], str | None] = lambda: None,
        restart_min_seconds: float = RESTART_MIN_SECONDS,
        restart_max_seconds: float = RESTART_MAX_SECONDS,
    ) -> None:
        """`wall_caches` sweeps the directories of walls that left: by default under the cache, on `now` and `monotonic`."""
        self._settings = settings
        self._wall_caches = (
            wall_caches if wall_caches is not None else WallCaches(settings.cache_dir, now=now, monotonic=monotonic)
        )
        self._link = link
        self._worker = worker
        self._outputs = outputs
        self._label_worker = label_worker
        self._label_outputs = label_outputs
        self._forget_labels = forget_labels
        self._frame_identity = frame_identity
        self._settings_url = settings_url
        self._now = now
        self._monotonic = monotonic
        self._restart_min = restart_min_seconds
        self._restart_max = restart_max_seconds
        self._running: dict[str, _Running] = {}
        #: One renderer per label output, keyed on the output's name: a label is
        #: drawn on a surface, and two renderers on one surface would fight.
        self._labels: dict[str, _RunningLabel] = {}
        #: Workers asked to stop and not yet finished, so shutdown can wait for
        #: each to close what it holds — on the Frame, the art channel.
        self._stopping: set[asyncio.Task[None]] = set()
        #: The wall each worker in `_stopping` ran, so its directory is never
        #: swept while it may still be writing there.
        self._stopping_for: dict[asyncio.Task[None], str] = {}
        #: Assignments already reported as impossible, so each is said once. Keyed
        #: on the wall *and* the output: a wall moved to another output it also
        #: lacks is news.
        self._reported_unplaceable: set[tuple[str, str]] = set()
        self._reported_unplaceable_labels: set[tuple[str, str]] = set()
        #: The outputs, label outputs and settings address last reported and taken.
        self._reported: tuple[list[OutputReport], list[LabelOutputReport], str | None] | None = None
        self._reported_at: float | None = None
        #: The instant the last report the server took was made, for the settings page.
        self._accepted_at: datetime | None = None
        #: The document the workers were last matched to, and the outputs they
        #: were matched against, so a change in either is a reason to match again.
        self._document: ClientDocument | None = None
        self._reconciled_outputs: tuple[list[OutputReport], list[LabelOutputReport]] | None = None

    @property
    def running(self) -> dict[str, str]:
        """Each wall with a worker, and the output it is on."""
        return {wall_id: running.assignment.output for wall_id, running in self._running.items()}

    @property
    def labels(self) -> dict[str, LabelAssignment]:
        """Each label output with a renderer, and what it captions."""
        return {output: running.assignment for output, running in self._labels.items()}

    def heard(self) -> HeardReport:
        """How the server last heard this Player: its last answer, and when the last report it took was made."""
        if self._accepted_at is None or self._reported_at is None:
            return HeardReport(answer=self._link.last_answer)
        return HeardReport(
            answer=self._link.last_answer,
            accepted_at=self._accepted_at,
            accepted_age_seconds=self._monotonic() - self._reported_at,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Supervise until asked to stop, then stop every worker and wait for each."""
        log.info(
            "client starting against %s",
            self._settings.server_url,
            extra={
                "event": "client.started",
                **self._settings.startup_lines(),
                # The name it goes by, which is the server's once there is one:
                # the local PLAYER_NAME alone would contradict the settings page.
                "player_name": player_name(self._settings.cache_dir, self._settings.player_name) or "(none)",
            },
        )
        try:
            cached = self._link.cached()
            if cached is not None:
                # **Before the first request**, so a server that is down at boot
                # costs the walls nothing.
                self.reconcile(cached)
            while not stop.is_set():
                await self.cycle()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.client_poll_seconds)
        finally:
            await self.stop_all()
            await self._link.close()
            log.info("client stopped", extra={"event": "client.stopped"})

    async def cycle(self) -> None:
        """One poll: the outputs reported if they are due, then the walls.

        The walls are matched to a new document when the server sends one, and to
        the last one again when this client's outputs are not the ones it was
        last matched against.
        """
        await self._report_outputs()
        document = await self._link.fetch()
        if document is not None:
            self.reconcile(document)
        elif self._document is not None and self._current_outputs() != self._reconciled_outputs:
            self.reconcile(self._document)
        await self._sweep_walls()

    @property
    def _stopping_walls(self) -> set[str]:
        return {wall_id for task, wall_id in self._stopping_for.items() if not task.done()}

    def _current_outputs(self) -> tuple[list[OutputReport], list[LabelOutputReport]]:
        return (self._outputs(), self._label_outputs())

    @property
    def _read_document(self) -> ClientDocument | None:
        """The document the workers follow, or None while none has been read and parsed.

        **The one guard both caches' sweeps stand on.** None means this client
        does not know which walls and labels it has, never that it has none: a
        server down at boot with no cached document, or a cached one that cannot
        be read. Each sweep returns at once on None, so neither removes anything
        until a document says what to keep.
        """
        return self._document

    async def _sweep_walls(self) -> None:
        """Sweep the walls' directories against the current document. Never raises."""
        document = self._read_document
        if document is None:
            return
        # Every wall the document places here, on any output and placeable or
        # not, and every wall a worker still runs or is still closing for: none
        # of their directories is a wall that left.
        placed = {assignment.wall_id for assignment in document.walls} | set(self._running) | self._stopping_walls
        try:
            await self._wall_caches.sweep(placed)
        # A sweep's fault costs disk, never the walls: it is said, and the next poll sweeps again.
        except Exception:  # prawduct:allow prawduct/broad-except -- see above
            log.exception(
                "sweeping the directories of walls that left this client failed",
                extra={"event": "client.wall_cache_unsweepable", "path": str(self._settings.cache_dir)},
            )

    # -- the walls ------------------------------------------------------------------

    def reconcile(self, document: ClientDocument) -> None:
        """Make the running workers match the document: start, stop, move."""
        outputs = self._current_outputs()
        self._document, self._reconciled_outputs = document, outputs
        available = {output.name: output for output in outputs[0]}
        placed = {assignment.wall_id: assignment.output for assignment in document.walls}
        wanted: dict[str, Assignment] = {}
        claimed: dict[str, str] = {}
        for assignment in document.walls:
            output = available.get(assignment.output)
            if output is None:
                self._unplaceable(assignment, "this client has no output by that name")
                continue
            if assignment.output in claimed:
                # The server refuses two walls on one output; a client that met
                # it anyway shows the first and says so, rather than letting two
                # workers fight over one screen.
                self._unplaceable(assignment, f"wall {claimed[assignment.output]} is already shown there")
                continue
            claimed[assignment.output] = assignment.wall_id
            wanted[assignment.wall_id] = assignment

        for wall_id, running in list(self._running.items()):
            target = wanted.get(wall_id)
            if target is not None and target.output == running.assignment.output:
                continue
            retire, why = _why_stopped(running.assignment.output, placed.get(wall_id), available, claimed)
            self._stop(wall_id, retire=retire, why=why)

        for wall_id, assignment in wanted.items():
            if wall_id in self._running:
                continue
            try:
                wall = self._settings.wall(wall_id)
            except WallIdUnusable as exc:
                self._unplaceable(assignment, str(exc))
                continue
            self._start(wall, assignment, available[assignment.output])

        self._reconcile_labels(document, {output.name for output in outputs[1]})
        self._forget_unmapped_labels()

    # -- the labels -----------------------------------------------------------------

    def _reconcile_labels(self, document: ClientDocument, available: set[str]) -> None:
        """Make the running label renderers match the document's labels.

        A renderer is restarted when its output's label changes (another wall, or
        another label id), and **retired** when its output is no longer mapped at
        all, which tells it to leave the panel blank rather than captioning a wall
        it no longer belongs to.
        """
        if self._label_worker is None:
            return
        wanted: dict[str, LabelAssignment] = {}
        for label in document.labels:
            if label.output not in available:
                self._label_unplaceable(label, "this client has no label output by that name")
                continue
            if label.output in wanted:
                self._label_unplaceable(label, f"label {wanted[label.output].label_id} is already drawn there")
                continue
            wanted[label.output] = label

        for output, running in list(self._labels.items()):
            target = wanted.get(output)
            if target != running.assignment:
                self._stop_label(output, retired=target is None)

        for output, label in wanted.items():
            if output not in self._labels:
                self._start_label(label)

    def _forget_unmapped_labels(self, _finished: object = None) -> None:
        """Remove what is cached for every label the document does not map, once a document has been read.

        **Again as each renderer finishes stopping**, because one stopped while
        its poll was under way may write its document after the match removed
        it. A label the document still maps keeps its file, on whichever output
        it is now, so a label moved between this client's outputs starts from it.
        """
        document = self._read_document
        if self._forget_labels is None or document is None:
            return
        self._forget_labels(frozenset(label.label_id for label in document.labels))

    def _start_label(self, label: LabelAssignment) -> None:
        stop = WorkerStop()
        worker = self._label_worker
        assert worker is not None  # noqa: S101 -- reconcile returns before here without one
        # After every renderer still stopping, for the reason walls wait: two
        # renderers drawing on one panel at once.
        stopping = tuple(self._stopping)
        task = asyncio.create_task(
            self._keep_running(
                lambda: worker(label, stop),
                stop,
                what=f"the renderer for label {label.label_id} on {label.output}",
                event="client.label",
                extra={"label_id": label.label_id, "output": label.output, "wall_id": label.wall_id},
                bound=partial(label_context, label.label_id, label.wall_id),
                after=stopping,
            ),
            name=f"label:{label.output}",
        )
        self._labels[label.output] = _RunningLabel(assignment=label, stop=stop, task=task)
        self._reported_unplaceable_labels = {key for key in self._reported_unplaceable_labels if key[0] != label.label_id}
        log.info(
            "captioning wall %s with label %s on %s",
            label.wall_id,
            label.label_id,
            label.output,
            extra={"event": "client.label_started", "label_id": label.label_id, "output": label.output, "wall_id": label.wall_id},
        )

    def _stop_label(self, output: str, *, retired: bool) -> None:
        running = self._labels.pop(output)
        if retired:
            running.stop.retire()
        else:
            running.stop.set()
        self._stopping.add(running.task)
        running.task.add_done_callback(self._stopping.discard)
        running.task.add_done_callback(self._forget_unmapped_labels)
        log.info(
            "label %s on %s %s; stopping its renderer",
            running.assignment.label_id,
            output,
            "captions no wall now" if retired else "captions another wall now",
            extra={
                "event": "client.label_stopped",
                "label_id": running.assignment.label_id,
                "output": output,
                "wall_id": running.assignment.wall_id,
                "retired": running.stop.retired,
            },
        )

    def _label_unplaceable(self, label: LabelAssignment, why: str) -> None:
        key = (label.label_id, label.output)
        if key in self._reported_unplaceable_labels:
            return
        self._reported_unplaceable_labels.add(key)
        log.error(
            "label %s for wall %s is assigned to label output %s, and it is not started: %s",
            label.label_id,
            label.wall_id,
            label.output,
            why,
            extra={
                "event": "client.label_unplaceable",
                "label_id": label.label_id,
                "output": label.output,
                "wall_id": label.wall_id,
            },
        )

    # -- starting and stopping --------------------------------------------------------

    def _start(self, wall: WallSettings, assignment: Assignment, output: OutputReport) -> None:
        stop = WorkerStop()
        # **After every worker still stopping has finished.** A wall moved to
        # another output would otherwise run two workers on its one directory for
        # a moment, and an output handed from one wall to another two workers on
        # one screen — on the Frame, two art channels at a set that refuses a
        # second while the first is open.
        stopping = tuple(self._stopping)
        task = asyncio.create_task(
            self._keep_running(
                lambda: self._worker(wall, output, stop),
                stop,
                what=f"the worker for wall {wall.wall_id} on {assignment.output}",
                event="client.worker",
                extra={"wall_id": wall.wall_id, "output": assignment.output},
                bound=partial(wall_context, wall.wall_id),
                after=stopping,
            ),
            name=f"wall:{wall.wall_id}",
        )
        self._running[wall.wall_id] = _Running(assignment=assignment, stop=stop, task=task)
        self._reported_unplaceable = {key for key in self._reported_unplaceable if key[0] != wall.wall_id}
        log.info(
            "showing wall %s (%s) on %s",
            assignment.name,
            wall.wall_id,
            assignment.output,
            extra={"event": "client.wall_started", "wall_id": wall.wall_id, "output": assignment.output},
        )

    def _stop(self, wall_id: str, *, retire: bool, why: str) -> None:
        """Ask a worker to stop, retired when its wall has left its output. `why` completes "wall X is …".

        The task finishes on its own; `stop_all` waits for every one.
        """
        running = self._running.pop(wall_id)
        output = running.assignment.output
        if retire:
            running.stop.retire()
        else:
            running.stop.set()
        self._stopping.add(running.task)
        self._stopping_for[running.task] = wall_id
        running.task.add_done_callback(self._stopping.discard)
        running.task.add_done_callback(lambda task: self._stopping_for.pop(task, None))
        log.info(
            "wall %s is %s; stopping its worker on %s",
            wall_id,
            why,
            output,
            extra={
                "event": "client.wall_stopped",
                "wall_id": wall_id,
                "output": output,
                "retired": running.stop.retired,
            },
        )

    def _unplaceable(self, assignment: Assignment, why: str) -> None:
        key = (assignment.wall_id, assignment.output)
        if key in self._reported_unplaceable:
            return
        self._reported_unplaceable.add(key)
        log.error(
            "wall %s (%s) is assigned to output %s, and it is not started: %s",
            assignment.name,
            assignment.wall_id,
            assignment.output,
            why,
            extra={"event": "client.wall_unplaceable", "wall_id": assignment.wall_id, "output": assignment.output},
        )

    async def stop_all(self) -> None:
        """Stop every worker and renderer, and wait for each to finish closing what it holds.

        Renderers are stopped, never retired: a client going down leaves each
        panel as it was, which on e-paper is the caption it last drew.
        """
        for wall_id in list(self._running):
            running = self._running.pop(wall_id)
            running.stop.set()
            self._stopping.add(running.task)
        for output in list(self._labels):
            label = self._labels.pop(output)
            label.stop.set()
            self._stopping.add(label.task)
        if self._stopping:
            await asyncio.gather(*self._stopping, return_exceptions=True)
            self._stopping.clear()

    async def _keep_running(
        self,
        run: Callable[[], Awaitable[None]],
        stop: WorkerStop,
        *,
        what: str,
        event: str,
        extra: dict[str, str],
        bound: Callable[[], AbstractContextManager[None]],
        after: tuple[asyncio.Task[None], ...] = (),
    ) -> None:
        """Run one worker or renderer until stopped, starting it again whenever it fails.

        `what` names it in the journal and `event` is the prefix of its events,
        `_crashed`, `_ended` and `_retire_failed`.

        **Everything it runs is logged inside `bound`**: the wall's id, or the
        label's and its wall's, bound for this task (`logs.wall_context`,
        `logs.label_context`). Bound here, inside the task, rather than around
        `create_task`, so the supervisor's own context never holds it: its lines
        about the client say no wall.

        **A stop that lands between runs, while the wait before a restart is
        under way, finds nothing running to see it**, so a retirement then is
        answered with the once-more call `WorkerStop` describes. A stop during a
        run is seen by that run, and a stop before the first run has nothing of
        this worker's on the output to leave.
        """
        with bound():
            if after:
                await asyncio.gather(*after, return_exceptions=True)
            if stop.is_set():
                return
            if not await self._restart_until_stopped(run, stop, what=what, event=event, extra=extra) or not stop.retired:
                return
            try:
                await run()
            except Exception:  # prawduct:allow prawduct/broad-except -- the wall is gone; its leaving costs only a line
                log.exception(
                    "%s could not leave its output after it was taken away",
                    what,
                    extra={"event": f"{event}_retire_failed", **extra},
                )

    async def _restart_until_stopped(
        self, run: Callable[[], Awaitable[None]], stop: asyncio.Event, *, what: str, event: str, extra: dict[str, str]
    ) -> bool:
        """`_keep_running`'s loop. True when the stop landed in a wait between runs, False when a run saw it."""
        wait = Backoff(minimum=self._restart_min, maximum=self._restart_max, monotonic=self._monotonic)
        while True:
            started = self._monotonic()
            try:
                await run()
            except Exception:  # prawduct:allow prawduct/broad-except -- logged and restarted
                log.exception(
                    "%s stopped on an error; starting it again",
                    what,
                    extra={"event": f"{event}_crashed", **extra},
                )
            else:
                if not stop.is_set():
                    log.error(
                        "%s ended without being asked to; starting it again",
                        what,
                        extra={"event": f"{event}_ended", **extra},
                    )
            if stop.is_set():
                return False
            if self._monotonic() - started > self._restart_max:
                # A worker that ran for longer than the longest wait has earned a
                # fresh ladder: this failure is a new one, not the next in a loop.
                wait.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=wait.hold())
            if stop.is_set():
                return True

    # -- the outputs ----------------------------------------------------------------

    async def _report_outputs(self) -> None:
        """POST the client heartbeat when what it says changed, or once per heartbeat interval.

        **Changed is news and is sent at once**: a screen plugged in is what a
        curator is waiting to see on the Clients page before choosing it, and so
        is another set at the Frame's address or a new address for the settings
        page. An unchanged report is sent once a minute, so the server can say
        how long ago this client was heard from. One the server refused is held
        (`pull.ClientPull.report` says for how long), so it is offered on every
        poll and the link decides whether to send it.
        """
        if self._frame_identity is not None:
            await self._frame_identity.refresh()
        reporting = (self._outputs(), self._label_outputs(), self._settings_url())
        elapsed = self._monotonic()
        due = self._reported_at is None or elapsed - self._reported_at >= heartbeat_module.INTERVAL_SECONDS
        if reporting == self._reported and not due:
            return
        made = self._now()
        heartbeat = client_heartbeat(reporting[0], label_outputs=reporting[1], settings_url=reporting[2], reported_at=made)
        if await self._link.report(heartbeat):
            self._reported = reporting
            self._reported_at = elapsed
            self._accepted_at = made
