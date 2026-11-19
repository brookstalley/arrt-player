"""The television, as the rest of the Player is allowed to see it.

**The interface exists because the client behind it is a liability.** The
`samsungtvws` fork this product depends on is unowned, four months static, and
carries a known defect in the verb that matters most; there is no maintained
alternative offering the async art client the Player is built on. The mitigation
was never "switch upstreams" — it is keeping the television boundary small and
behind a seam, so replacing the client is a swap rather than a rewrite.

So this module is a contract and holds no protocol knowledge. Everything the
library does badly is corrected on the other side of it, and what comes through
here is the corrected form:

* An **upload** returns a content id or raises. It never returns "no id, and no
  statement about whether the image landed", which is what the library does and
  what the 2024 loader turned into a duplicate on the wall.
* A **removal** reports what the television actually holds afterwards, and keeps
  *unconfirmable* apart from *failed*, because collapsing those two was the
  original defect.
* Everything that can go wrong with the connection arrives as `TvUnavailable`,
  regardless of which of the library's seven exception types produced it. An
  asleep television is an expected operating condition here, not an incident.

**And it is where the household's rule is kept.** The television belongs to
whoever is using it (Arrt's `nonfunctional-requirements.md`), so every verb that
changes what the set shows or stores is a concrete method here that reads the
set's state immediately before it is sent, and is refused with `TvNotOurs` when
that state is not one the verb was measured safe in (`WRITES`). A client
implements only what goes on the wire, as `_send_<verb>`; it cannot redefine the
verb itself (`__init_subclass__`), so a verb added later inherits the read rather
than relying on its caller to have asked a minute earlier.
"""

import enum
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, final

#: The category every image this product uploads lands in. Samsung exposes no way
#: to create another — 2 is my-pictures, 4 favourites, 8 the store — and the
#: upload verb takes no category argument at all. Changing the picture is therefore a pointer
#: into one flat list, which is why bindings and orphan removal exist.
UPLOADED_CATEGORY: Final[str] = "MY-C0002"


class TvUnavailable(Exception):
    """The television could not be reached, or stopped answering mid-conversation.

    One type for every underlying cause on purpose. The caller's response is the
    same to all of them — hold the wall where it is, back off, try again — and a
    daemon that branched on which library exception arrived would be encoding the
    library's internals into the loop that exists to be independent of them.
    """


class TvNotOurs(Exception):
    """The set is somebody else's right now, so a verb that would change it was not sent.

    **Not a `TvUnavailable`**, because the set answered and nothing is wrong: it
    is showing somebody's programme, or it could not say whether it is. The wall
    holds the change and asks again later, without the outage backoff and
    without recording the work as failed. Nothing was sent, so the set holds
    exactly what it held before.
    """

    def __init__(self, verb: str, reason: str) -> None:
        super().__init__(f"{verb} was not sent: {reason}")
        #: The verb that was held, as `WRITES` names it.
        self.verb = verb


class TvUploadFailed(Exception):
    """An image was not put on the television, confirmed rather than assumed."""


class PowerStateUnreadable(Exception):
    """The set's `PowerState` could not be read, though the art channel may have answered.

    **Not a `TvUnavailable`, and the difference is the wall's.** The read goes over
    a different transport from everything else here (REST, beside the art
    websocket), so its failing says nothing about whether the art channel is
    usable, and must not drop the connection or start the outage backoff.
    """


class IdentityUnreadable(Exception):
    """The set's device id could not be read from its REST device description.

    Not a `TvUnavailable`: the read is REST, needs no art channel, and a set
    asleep on the network may simply not answer it. The client reports its Frame
    without an identity and asks again on its next report.
    """


class TvRemovalUnconfirmed(Exception):
    """What the television holds after a removal could not be established.

    Distinct from "the images are still listed", which is a known failure. This
    is the unknown one: the caller knows only that it asked. The library discards
    the reply to a removal, so a caller that reported either outcome would be
    guessing.
    """


@dataclass(frozen=True)
class RemovalOutcome:
    """What the television held after a removal was requested, read back from it."""

    requested: tuple[str, ...]
    surviving: tuple[str, ...]

    @property
    def removed(self) -> tuple[str, ...]:
        """The requested ids the television no longer lists."""
        still_there = set(self.surviving)
        return tuple(content_id for content_id in self.requested if content_id not in still_there)

    @property
    def complete(self) -> bool:
        return not self.surviving


@dataclass(frozen=True)
class SelectionAnnouncement:
    """The set's own word about what is on the wall now.

    **The only honest account of the wall this product has.** The set reports the
    picture it is displaying by emitting this; the read that looks like it should
    answer the same question — `get_current_artwork` — reports the art-store slot
    instead, and named one unchanging id across every observation ever made here
    while the picture visibly changed (`samsung-tv-state-findings.md`).

    Announcements arrive for selections nobody here made, because somebody using
    the remote produces one too. A consumer must therefore treat the id as news
    about the wall rather than as an echo of its own request.
    """

    content_id: str
    #: The set's `is_shown` flag, already read off the wire, where it is the
    #: string "Yes" rather than a boolean.
    is_shown: bool


#: What a selection observer is handed. Called on the television's reader task,
#: so it must be cheap, must not block, and must not assume anything about which
#: task it runs on.
SelectionObserver = Callable[[SelectionAnnouncement], None]


class Gate(enum.Enum):
    """The set's states a write may be sent in, as `samsung-tv-state-findings.md` § What works in each state measured them."""

    #: Only when `get_artmode` says `on`. A selection sent to a set showing a
    #: programme switches it into art mode and takes the screen; sent to a dark
    #: one, it is accepted and ignored.
    ART_MODE = "art_mode"
    #: When `get_artmode` says `on`, or says `off` while `PowerState` says
    #: `standby` (the dark state), the two states these verbs were measured
    #: working in. **Held in the television state**, where they were never tried,
    #: and whenever `PowerState` cannot be read, which cannot tell the two apart.
    ART_MODE_OR_DARK = "art_mode_or_dark"


#: **Every verb that changes what the set shows or stores, and the gate it
#: passes.** The one place a write is declared: `TvClient` gives each a concrete
#: method that reads the set and then calls the client's `_send_<verb>`, and a
#: test ranges over this to check every one is held. A verb added to the
#: boundary that writes goes here, or it is a read in `READS`; a method that is
#: neither fails a test by name.
WRITES: Final[Mapping[str, Gate]] = MappingProxyType(
    {
        "show": Gate.ART_MODE,
        "upload": Gate.ART_MODE_OR_DARK,
        "remove": Gate.ART_MODE_OR_DARK,
        "set_brightness": Gate.ART_MODE_OR_DARK,
        "disable_native_slideshow": Gate.ART_MODE_OR_DARK,
    }
)

#: Every other verb a client implements: reads of the set, and the connection's
#: own coming and going. **None of them changes what the set shows or stores**,
#: which is why none is gated: they are measured answering in every state
#: (`samsung-tv-state-findings.md`), and the gate itself is built from two of them.
READS: Final[frozenset[str]] = frozenset(
    {
        "connect",
        "close",
        "listed_content_ids",
        "showing_art",
        "art_mode_announcement_pending",
        "power_state",
        "read_identity",
        "reported_art_mode",
        "observe_selections",
    }
)

#: What the set says `PowerState` is when it is switched off at the remote.
_STANDBY: Final[str] = "standby"


class TvClient(ABC):
    """What the daemon may ask of a television.

    An abstract base rather than a structural protocol, deliberately: the test
    double subclasses it, so a method added here fails the fake loudly at import
    instead of passing every test while the real client grew a capability nothing
    exercised.

    **The writes are concrete, and final.** Each reads the set, then sends; a
    subclass supplies `_send_<verb>` and may not define the verb itself, which is
    refused when the class is made rather than discovered when somebody's
    programme is interrupted.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        bypassing = sorted(verb for verb in WRITES if verb in vars(cls))
        if bypassing:
            raise TypeError(
                f"{cls.__name__} defines {bypassing}, which would send past the art-mode gate; " "implement _send_<verb> instead"
            )

    # -- the writes, each behind the gate ------------------------------------

    @final
    async def show(self, content_id: str) -> bool:
        """Put an image the set already holds on the wall, and say whether it appeared.

        Refused with `TvNotOurs` unless the set says, now, that it is showing
        art. See `_send_show` for what the return value means.
        """
        await self._permit("show")
        return await self._send_show(content_id)

    @final
    async def upload(self, path: Path) -> str:
        """Put an image on the television and return the id it was given. Held in the television state."""
        await self._permit("upload")
        return await self._send_upload(path)

    @final
    async def remove(self, content_ids: Sequence[str]) -> RemovalOutcome:
        """Remove images, and report what the television holds afterwards. Held in the television state.

        Removing nothing sends nothing, so it asks the set nothing either.
        """
        requested = tuple(content_ids)
        if not requested:
            return RemovalOutcome(requested=(), surviving=())
        await self._permit("remove")
        return await self._send_remove(requested)

    @final
    async def set_brightness(self, value: int) -> None:
        """Set the panel's brightness on the set's own scale. Held in the television state."""
        await self._permit("set_brightness")
        await self._send_set_brightness(value)

    @final
    async def disable_native_slideshow(self) -> None:
        """Stop the set's own slideshow, so it cannot fight host-driven selection. Held in the television state."""
        await self._permit("disable_native_slideshow")
        await self._send_disable_native_slideshow()

    async def _permit(self, verb: str) -> None:
        """Read the set now, and raise `TvNotOurs` unless `verb`'s gate admits what it says.

        **Immediately before the send, every time, and never from a cache.** A
        read taken earlier in the pass is not this read: an upload can run for a
        minute, and somebody can pick up the remote inside it. `showing_art` is
        asked first because it is the one read that admits every write; only a
        no is followed by `PowerState`, and only for a verb whose gate it can
        open, so a wall in art mode spends one read per write and no more.

        `TvUnavailable` from either read propagates: a set that cannot be asked is
        the outage path, not a refusal.
        """
        gate = WRITES[verb]
        if await self.showing_art():
            return
        if gate is Gate.ART_MODE:
            raise TvNotOurs(verb, "the set is not showing art")
        try:
            power = await self.power_state()
        except PowerStateUnreadable as exc:
            raise TvNotOurs(verb, f"the set is not showing art and its panel power could not be read ({exc})") from exc
        if power != _STANDBY:
            raise TvNotOurs(verb, f"the set is not showing art and its panel is lit (PowerState {power!r})")

    # -- what a client implements ----------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Reach the set and open the art channel, or raise `TvUnavailable`."""

    @abstractmethod
    async def close(self) -> None:
        """Let the connection go. Never raises — this runs on the way out."""

    @abstractmethod
    async def _send_disable_native_slideshow(self) -> None:
        """Stop the set's own slideshow. Reached only through `disable_native_slideshow`."""

    @abstractmethod
    async def listed_content_ids(self) -> frozenset[str]:
        """Every id the television currently lists in the uploaded category."""

    @abstractmethod
    async def _send_upload(self, path: Path) -> str:
        """Put an image on the television and return the id it was given. Reached only through `upload`.

        Raises `TvUploadFailed` only when the image is confirmed *not* to be
        there — never merely because the library said so.
        """

    @abstractmethod
    async def _send_show(self, content_id: str) -> bool:
        """Put an image the set already holds on the wall, and say whether it appeared. Reached only through `show`.

        **Asking and confirming are one call because they cannot safely be two.**
        The television reports a selection by *emitting* an event, not by
        answering a later question, so the only sound confirmation listens from
        before the request is sent. Split across two calls, every caller races
        the set — and the shape invites the confirming *read* that this seam
        previously carried, which was measured answering about something else
        entirely (see `samsung.py`).

        Returns False for the failure the return value of a bare selection cannot
        express: a set that accepts the request, raises nothing, and goes on
        displaying what it had. Raises `TvUnavailable` if the set could not be
        asked at all — which is a different event, and the caller treats it so.
        """

    @abstractmethod
    async def showing_art(self) -> bool:
        """Whether the set is showing art, and may therefore be asked to change it.

        **This is a permission question, not a diagnostic.** Selecting an image on
        a set that is showing a television programme does not fail politely — it
        switches the set into art mode and takes the screen off the person
        watching (measured; `samsung-tv-state-findings.md`). So nothing may be put
        on the wall without asking this first, and `_permit` asks it before every
        write.

        **False on anything short of a clear yes**, including a reply this seam
        cannot read. The two errors are not symmetric: a wall that waits is late,
        and a wall that does not wait interrupts somebody's evening. Raises
        `TvUnavailable` when the set could not be asked at all, which is the
        existing outage path and a different event again.
        """

    @abstractmethod
    def art_mode_announcement_pending(self) -> bool:
        """Whether the set has announced an art-mode change since this was last asked.

        **A hint that it is worth asking again, never an answer in itself.** The
        set announces its own transitions, so a wall that backed off because
        nobody was in art mode can resume the moment somebody is, instead of
        sitting out the remainder of a wait. What it must not become is a cached
        view of the set's state: a missed announcement would then freeze the wall
        for ever or, worse, license a selection into somebody's programme. Missing
        one here costs only promptness, because the decision is still a fresh
        read.

        Synchronous and consuming — it reports an edge, and clears it.
        """

    @abstractmethod
    async def power_state(self) -> str:
        """The set's REST `PowerState` — `on` or `standby` — or raise `PowerStateUnreadable`.

        **The one thing that tells somebody watching television from a set that is
        off**, both of which `showing_art` answers no to
        (`samsung-tv-state-findings.md` § The states). It says whether the panel is
        lit and nothing else, so it is never asked on its own: only after
        `showing_art` has already said no, to report which of the two the wall is
        in, and in `_permit`, where `standby` is what opens the writes measured
        working in the dark state.

        **A read, never a press.** It is a GET of the set's device description, the
        same one `power_probe.py` samples; it changes nothing on the set, so the
        rule that the television belongs to whoever is using it is not engaged.

        Bounded by a short timeout of its own, and never raises `TvUnavailable`:
        its transport is not the art channel's, and its failing is not an outage.
        """

    @abstractmethod
    async def read_identity(self) -> str:
        """The set's own device id (`device.duid` from `/api/v2/`), or raise `IdentityUnreadable`.

        **Independent of the art channel, deliberately.** The client reports its
        Frame's identity whether or not a wall is on it, because a Frame moved to a
        new client has no wall there yet and must still say who it is for its walls
        to follow it (`player-contract.md` § Transport). So this opens no
        websocket, presses no key and needs no pairing: one GET of the device
        description, bounded by a timeout, and nothing that could contend with a
        running worker's art connection.
        """

    @abstractmethod
    async def reported_art_mode(self) -> str | None:
        """The set's own art-mode flag, or None if it would not answer.

        Diagnosis, never control flow — nothing decides what to do from this.
        It is asked only when a selection has already been shown not to have
        landed, so that the one line an operator reads says *why* the wall is not
        changing rather than merely that it is not. The flag is the set's own word
        and is not a capability test: this television serves uploads, removals and
        brightness perfectly well while reporting art mode off.
        """

    @abstractmethod
    async def _send_remove(self, content_ids: tuple[str, ...]) -> RemovalOutcome:
        """Remove images, never none, and report what the set holds afterwards. Reached only through `remove`."""

    @abstractmethod
    async def _send_set_brightness(self, value: int) -> None:
        """Set the panel's brightness. Reached only through `set_brightness`."""

    @abstractmethod
    def observe_selections(self, observer: SelectionObserver) -> None:
        """Be told, from now on, whatever the set says about the picture on the wall.

        **A method rather than a callback anyone may register, because the library
        underneath keeps one handler per event.** A second subscriber registered
        directly does not join the selection confirmation, it *replaces* it — after
        which every rotation falls to its timeout and reports a wall that will not
        move, silently, while the new subscriber works perfectly. This fans out
        instead, so adding a listener cannot break the one that was already there.

        Observers run on the client's own reader task and must be cheap. Anything
        expensive belongs on the observer's own task, driven by what it learns.
        """
