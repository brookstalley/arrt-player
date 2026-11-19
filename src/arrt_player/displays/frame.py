"""The Frame: a television that holds its own pictures, behind the `TvClient` seam.

Uploads go into the set's own store, a picture is selected by the set's content
id, and the set is asked whether it is in art mode before anything touches it.
The binding table that joins a work to its content id lives in the device's own
store (`state.DisplayState`), which outlives the process because the set holds
the picture between restarts.

Three behaviours are worth reading before changing anything here, because each
was chosen against an alternative that looks more obvious:

**Uploads are spread across passes rather than done in a batch on adoption.** A
fresh install has an empty binding table and a theme of forty works, and each
upload costs the set the better part of ten seconds. Doing them all before the
first `select_image` leaves the wall on yesterday's picture for five minutes and,
worse, leaves a curator pressing "next" with nothing happening for five minutes —
against a poll interval that is one second precisely because that wait is the one
the product may not have. So the wall shows what it can as soon as it can, and
carries one pending upload per pass until the theme is complete.

**The television going away is an expected operating condition.** The set is
asleep most of the time; a connection failure is a backoff, not an incident, and
the picture stays up regardless because the television holds it.

**It draws no label.** It reports what the screen is doing (`display_state`),
and every label of the wall — on this client or any other — follows that report
through the server (`labels-and-surfaces.md`; `label_renderer.py`).

**The household's rule is kept below this module, not by it.** Every verb that
changes what the set shows or stores reads the set's state immediately before it
is sent (`tv.client.WRITES`), and is refused with `TvNotOurs` when the set is
somebody else's. What this module decides is only what to do with a refusal, per
verb, and how soon to ask again; `is_ours` stays as the cheap early exit that
keeps a held change from reaching the seam at all.

**A fault of the device's own store ends the pass, not the wall.** The store
raises `StoreFault` for a fault of the disk or the file (`state.store._FAULTS`),
and the wall's loop treats it as it treats the set going away: the pass ends,
the next comes after a wait on the same ladder, and the journal says it once per
episode, at WARNING for a lock that may clear and at ERROR for a fault the wall
cannot outlive. The heartbeat's `last_error` names it. Anything else the store
raises is this code's mistake and still reaches the supervisor's restart.

**The set lets go of a work nobody hangs any more, and only of that.** The
set's one upload category is all the storage it has, so a picture left there
after its work leaves every theme stays for good. A binding is retired once no
feed this wall adopted has named its work for `RETIRED_AFTER_SECONDS` of the
wall running with a feed (`state.store`, `feed_seconds`): its image is removed
through the gated removal, the set's own list confirms it, and the row goes.
Absence from one feed is not retirement, because a theme hung for an evening or
a scene would otherwise cost the whole theme's uploads on the way back. The
work on the wall, and every work the adopted feed names (its schedule's whole
horizon, its scene and its staging), is never retired, and a removal is never
asked for an image the set does not list. The orphan path is the other half: it
removes what no binding accounts for, and a retirement removes what a binding
accounts for and no feed wants, in the same removal.

**A binding is used only on the set it was made on.** A set's content ids are
its own, so the wall reads which set it reaches (`TvClient.read_identity`, one
REST GET of the set's description, sent straight after the art channel opens
and before the first `get_artmode` of the pass, whatever the set is showing)
before any binding is used on a connection: when the wall starts, after every
time the set went away, and after a selection that dropped the connection.
The store compares it with the set its bindings were made on
(`DisplayState.belongs_to`): another set has them set aside, and the wall
starts on it as on a first run. **While the set will not say who it is, the
wall uses no binding**, said once and asked again on its own ladder. A set that
opens its art channel and has not said for `IDENTITY_PATIENCE_SECONDS` gets the
wall's art anyway, from a store held in memory that starts empty, as a first
run would; **nothing is removed from the set** until it says, because removal is
sent only from a store whose recorded identity is the set's (`_may_remove`).
"""

import functools
import hashlib
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

from arrt_player import brightness as brightness_module
from arrt_player.config import Settings
from arrt_player.episodes import Backoff, ReportOnce
from arrt_player.heartbeat import DisplayReport, ScreenState
from arrt_player.logs import work_context
from arrt_player.manifest import Watcher
from arrt_player.programmes.schedule import Schedule
from arrt_player.state import Binding, DisplayState, UploadStatus
from arrt_player.state.store import SetChange, SetMet, StoreFault
from arrt_player.tv import (
    IdentityUnreadable,
    PowerStateUnreadable,
    RemovalOutcome,
    SelectionAnnouncement,
    TvClient,
    TvNotOurs,
    TvRemovalUnconfirmed,
    TvUnavailable,
    TvUploadFailed,
)
from arrt_player.wall import Capabilities, Clock, DisplayRecord, Picture, Shown, Wall

log = logging.getLogger(__name__)

#: How long no feed this wall adopted may name a work before its image leaves
#: the set: fourteen days of the wall running with a feed. Long enough to outlast
#: a holiday theme and a round trip between themes; a work that returns after it
#: costs one upload.
RETIRED_AFTER_SECONDS: Final[float] = 14 * 24 * 3600.0

#: How often the wall's running time with a feed is written, with when each
#: work was last named, and the bindings looked over for one due to retire. In
#: that running time, so the card takes one small write an hour at most; a
#: Player that stops loses at most this much of the count, which can only make
#: a retirement later.
FEED_TIME_SAVED_EVERY_SECONDS: Final[float] = 3600.0

#: How long a set that opens its art channel may decline to say which set it is
#: before the wall shows art on it anyway, from an empty store held in memory and
#: with nothing removed. The set has answered its description at once in every
#: state measured (`samsung-tv-state-findings.md` § What works in each state), so
#: this is long against a passing stall of its REST port, and short against an
#: evening of a wall showing nothing new.
IDENTITY_PATIENCE_SECONDS: Final[float] = 15 * 60.0


class SetUnidentified(Exception):
    """The set reached on this connection would not say which set it is, so no binding may be used on it yet.

    One of the Frame's `unavailable`: the pass ends, and the wall asks again
    when its wait is over (`wait`).
    """

    def __init__(self, reason: str, *, wait: float) -> None:
        super().__init__(f"the television did not say which set it is ({reason}); the wall uses none of its pictures on it")
        self.wait = wait


def frame_wall(
    *,
    settings: Settings,
    tv: TvClient,
    state: DisplayState,
    watcher: Watcher,
    clock: Clock,
) -> Wall:
    """A wall on the Frame: the shared loop, this driver, and a programme per major, remembering in the store.

    **The programme remembers through the display**, not the store, so the work
    it believes is up is the one on the bindings the display is using: none on
    a set met for the first time (`FrameDisplay.last_selected_work_id`).
    """
    display = FrameDisplay(settings=settings, tv=tv, state=state, clock=clock)
    return Wall(
        wall=settings,
        display=display,
        programmes={
            2: Schedule(
                wall_id=settings.wall_id,
                render_root=settings.render_root,
                composed_root=settings.composed_root,
                geometry=lambda: settings.geometry,
                memory=display,
                clock=clock,
            ),
        },
        watcher=watcher,
        clock=clock,
    )


class FrameDisplay:
    """One television, as a wall's display."""

    #: **The store's faults as well as the set's absence**, because the wall's
    #: loop answers both the same way: end the pass, beat, wait, try again.
    #: `went_away` tells them apart.
    #: **And a set that will not say which it is** (`SetUnidentified`), which
    #: the loop answers the same way again.
    unavailable: tuple[type[Exception], ...] = (TvUnavailable, StoreFault, SetUnidentified)
    #: The Frame wall's start, stop and crash lines read `frame.started`,
    #: `frame.stopped`, `frame.crashed`.
    journal_name = "frame"
    description = "the Frame"

    def __init__(self, *, settings: Settings, tv: TvClient, state: DisplayState, clock: Clock) -> None:
        self._settings = settings
        self._tv = tv
        #: The device's store on disk, and the store whose bindings the wall is
        #: using: the same one, except while a set that will not say who it is
        #: is shown art from `_scratch`, an empty store held in memory.
        self._store = state
        self._state = state
        self._scratch: DisplayState | None = None
        self._clock = clock

        #: The identity of the set this connection reaches, once read and made
        #: the store's (`_know_the_set`). **None until then, and again whenever
        #: the connection goes**, because whatever answers at the address next
        #: may be another set.
        self._on: str | None = None
        #: When the set first declined to say who it is since it last did, on
        #: the elapsed clock; and the wait before it is asked again.
        self._unidentified_since: float | None = None
        self._identity_retry = Backoff(
            minimum=settings.tv_retry_min_seconds,
            maximum=settings.tv_retry_max_seconds,
            monotonic=clock.monotonic,
        )
        self._unidentified_reason = ""
        #: The set would not say who it is, and then the wall went on without it:
        #: each said once, both ended by the set saying.
        self._unidentified = ReportOnce()
        self._going_on_unidentified = ReportOnce()
        #: Whether the last pass ended for want of the set's identity, which says
        #: nothing about whether the set answers (`heartbeat_fields`).
        self._ended_unidentified = False

        #: What the set last announced about its own wall, which is the only
        #: honest account of it this product has. Written from the television's
        #: reader task, read here — a plain assignment either way, so no lock:
        #: it is one reference, and a reader that catches the previous value gets
        #: a stale id rather than a torn one.
        self._announced_content_id: str | None = None
        #: The announcement itself, kept so the wall's own task can tell a new
        #: one from one it has already taken — by identity, so the same picture
        #: announced twice is still two pieces of news.
        self._announcement: SelectionAnnouncement | None = None
        self._announcement_taken: SelectionAnnouncement | None = None
        tv.observe_selections(self._note_announcement)

        #: The set's id for the picture on the wall: the Player's own confirmed
        #: selection, or one the set announced as shown that somebody chose with
        #: the remote. None until either has happened.
        self._wall_content_id: str | None = None
        #: What the wall's screen is doing. **Unreachable until the set has been
        #: asked**, which is the contract's word for "the controller cannot tell"
        #: — a fresh process has not asked anything yet, and a guess would be a
        #: reading nobody took.
        self.record = DisplayRecord(
            clock,
            wall_id=settings.wall_id,
            initial=DisplayReport(state=ScreenState.UNREACHABLE, work_id=None, since=clock.now()),
        )
        #: `PowerState` would not answer while `get_artmode` did. The wall is
        #: reported in use (every label blanks either way); said once per episode.
        self._power_unreadable = ReportOnce()

        #: Whether the set was showing art the last time it was actually asked.
        #: **None until it has been**, rather than defaulting to either answer: the
        #: gate is only consulted when something is about to happen, so a fresh
        #: process genuinely does not know, and a heartbeat that guessed would be
        #: reporting a read nobody performed.
        self._showing_art: bool | None = None

        self._last_error: str | None = None

        #: Somebody is watching their own television. Reported at INFO rather than
        #: WARNING because that is not a fault — but still reported, because "the
        #: wall stopped" otherwise has no explanation in the journal at all.
        #: **Moved only by `_learned_showing_art`**, from the same news that sets
        #: `_showing_art`, so the episode ends whatever says the set is showing
        #: art again, not only the gate's read (`is_ours`), which the schedule
        #: consults only when it wants a different work up.
        self._not_our_wall = ReportOnce()

        #: When the wall may next be asked to change, once it has been found not
        #: changing. **The schedule has no timer of its own**: a slot whose work
        #: never reached the wall is still owed on the next poll, which is the
        #: right thing to do with a picture that never went up, so without this it
        #: would be re-asked on every poll: a selection a second, each waiting out
        #: the confirmation window, all night at a set that will ignore every one.
        #: It backs off on the same ladder as an unreachable
        #: television, because "the set is not doing what it is told" is that same
        #: situation arriving by a route that raises nothing.
        #:
        #: **Its episode is the wall taking selections and displaying none of
        #: them** (`rotation.wall_unchanged`), said once: a panel dark for hours
        #: would otherwise be a hundred lines a night. One object with the wait
        #: (`_WaitedEpisode`), so whatever clears the wait ends the episode.
        self._wall = _WaitedEpisode(
            Backoff(
                minimum=settings.tv_retry_min_seconds,
                maximum=settings.tv_retry_max_seconds,
                monotonic=clock.monotonic,
            ),
            ended=_say_the_wall_recovered,
        )

        #: Reconciling the binding table against the set is **owed until it is
        #: done**, not attempted once when a manifest lands. A new manifest is
        #: reported by the watcher on exactly one pass; if the set is asleep on
        #: that pass — which this module's own note says is most of them — the
        #: pass aborts, and tying the work to that one flag drops orphan removal
        #: entirely rather than deferring it.
        self._reconciliation_owed = False
        #: When reconciliation may next be attempted. It stays *owed* through a
        #: television that cannot say what it removed, and without this it would
        #: then be retried at the poll rate — one listing, one removal request and
        #: one INFO line a second.
        #:
        #: **The same `Backoff` the connection and the upload retry use**, at a
        #: fixed wait rather than a doubling one — equal bounds make `hold`'s
        #: ladder a constant.
        self._reconcile_wait = Backoff(
            minimum=settings.tv_retry_max_seconds,
            maximum=settings.tv_retry_max_seconds,
            monotonic=clock.monotonic,
        )
        self._brightness_at: float | None = None
        self._brightness_value: int | None = None

        #: The elapsed clock at the last pass that counted the wall's running
        #: time with a feed. **None in a fresh process**, so the time a Player
        #: was off or restarting is never counted: whatever the clocks read
        #: after a month off, the first pass counts nothing.
        self._feed_time_read_at: float | None = None
        #: The count when it was last written, or tried.
        self._feed_time_saved_at = state.feed_seconds
        #: The works the adopted feed names, and the works that stopped being
        #: named in this process with the count when they did. Kept here as
        #: well as in the store, so a work's leaving is exact while the Player
        #: runs whether or not the hourly write has happened or succeeded.
        self._named: frozenset[str] = frozenset()
        self._left_at: dict[str, float] = {}
        #: Works whose image the set was asked to remove, answered, and kept. Not
        #: asked for again until the next feed, as the orphan path does not ask
        #: again: asking at once learns nothing.
        self._kept_by_the_set: set[str] = set()

        #: The set could not be reached at all. Its own ladder, separate from the
        #: wall's: a television that is asleep and one that is awake and ignoring
        #: selections are different conditions, and recovering from one says
        #: nothing about the other.
        self._connection_retry = Backoff(
            minimum=settings.tv_retry_min_seconds,
            maximum=settings.tv_retry_max_seconds,
            monotonic=clock.monotonic,
        )
        self._unavailable = ReportOnce()

        #: The device's own store would not do what a pass asked of it. Its own
        #: ladder, on the same bounds as the set's, and its own episodes: one for
        #: a fault that may clear, said at WARNING, and one for a fault the wall
        #: cannot outlive, said at ERROR, so a lock that turns into a full disk
        #: is still said at the level that reaches somebody. Both end at the
        #: first pass that completes (`answering`).
        self._store_retry = Backoff(
            minimum=settings.tv_retry_min_seconds,
            maximum=settings.tv_retry_max_seconds,
            monotonic=clock.monotonic,
        )
        self._store_busy = ReportOnce()
        self._store_failing = ReportOnce()
        #: Whether the last pass ended on the store. The wall's loop beats such a
        #: pass as unreachable, which is a claim about the set this pass did not
        #: establish, so the heartbeat says it could not tell (`heartbeat_fields`).
        self._ended_on_the_store = False
        #: An art-mode announcement whose read has not yet been acted on to the
        #: end. The set's flag is consumed when it is collected, so a pass that a
        #: store fault ends after collecting it would otherwise lose the news,
        #: and the display state would wait for the next selection to be right.
        self._art_mode_news_owed = False

        #: When a write the seam held may next be tried: an upload, a removal, a
        #: brightness change or the slideshow switch, refused (`TvNotOurs`) while
        #: the set is lit and not showing art. **Each try costs two reads of the
        #: set** (`get_artmode`, then `PowerState`), and a pending upload is due on
        #: every pass, so without the wait an evening of television is two
        #: requests a second. Its episode is the hold (`tv.writes_held`, then
        #: `tv.writes_resumed`), one object with the wait (`_WaitedEpisode`).
        self._writes = _WaitedEpisode(
            Backoff(
                minimum=settings.tv_retry_min_seconds,
                maximum=settings.tv_retry_max_seconds,
                monotonic=clock.monotonic,
            ),
            ended=_say_the_writes_resumed,
        )

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # -- the programme's memory, which is the bindings' -------------------------

    @property
    def last_selected_work_id(self) -> str | None:
        """The work last put on the wall, from the store whose bindings the wall is using."""
        return self._state.last_selected_work_id

    def set_last_selected_work_id(self, work_id: str) -> None:
        self._state.set_last_selected_work_id(work_id)

    def start_lines(self) -> dict[str, object]:
        return self._settings.startup_lines()

    # -- the pass ----------------------------------------------------------

    def adopted(self, pictures: Sequence[Picture]) -> None:
        named = frozenset(picture.work_id for picture in pictures)
        for work_id in self._named - named:
            self._left_at[work_id] = self._state.feed_seconds
        self._named = named
        self._kept_by_the_set.clear()
        self._reconciliation_owed = True
        # A new manifest clears any wait: it is new information, and it is
        # usually what arrives after somebody has fixed whatever the set was
        # unhappy about. The wait exists to stop a *retry* loop, not to make
        # the wall ignore news.
        self._reconcile_wait.clear()

    async def prepare(self, pictures: Sequence[Picture]) -> None:
        self._count_feed_time()
        await self._tv.connect()
        # **Before anything that reads a binding**: the news resolves an
        # announced id to a work, and everything after it selects, uploads or
        # removes by one.
        await self._know_the_set()
        # The set's news first, because it can end a hold on the writes below:
        # the slideshow switch, the reconciliation and the brightness all read
        # the writes' wait, and a set back in art mode should get them this pass.
        await self._take_the_sets_news()
        await self._switch_off_the_slideshow()
        if self._reconciliation_owed and self._reconcile_wait.is_due() and self._writes.is_due():
            # Cleared only when the work actually settled: a set that goes
            # away halfway through raises, and one that cannot say what it
            # removed reports so — both leave the work owed for a later pass
            # rather than recorded as done.
            try:
                self._reconciliation_owed = not await self._reconcile_with_the_set(pictures)
            except TvNotOurs as exc:
                # Still owed, and not a failure of the set's to say what it
                # removed: the removal was never sent. Its own wait is left
                # alone, so the set returning to art mode resumes it at once.
                self._hold_the_writes(exc)
            else:
                if self._reconciliation_owed:
                    self._reconcile_wait.hold()
                else:
                    self._reconcile_wait.clear()
        await self._apply_brightness()

    async def after(self, pictures: Sequence[Picture]) -> None:
        await self._upload_one_pending(pictures)
        self._save_feed_time(pictures)

    async def idle(self) -> None:
        return

    def went_away(self, exc: Exception) -> float:
        """Report the television going away once, then wait longer each time.

        Once, because an asleep set is the normal overnight condition and one
        WARNING a second until morning buries every other line in the journal. The
        recovery is logged too, so the pair reads as an episode with a length.
        """
        # **Not cleared by a good pass.** A Player that dropped its error the
        # moment anything succeeded would report itself fine while failing every
        # other minute. It is overwritten by the next failure, so it is always
        # the latest rather than the first.
        self._last_error = str(exc)
        if isinstance(exc, StoreFault):
            self._ended_unidentified = False
            return self._the_store_failed(exc)
        self._ended_on_the_store = False
        if isinstance(exc, SetUnidentified):
            self._ended_unidentified = True
            return exc.wait
        self._ended_unidentified = False
        # The connection is gone, and with it what is known of the set behind it.
        self._forget_the_set()
        self.record.moved_to(ScreenState.UNREACHABLE)
        if self._unavailable.begin():
            log.warning("%s; holding the wall where it is and retrying", exc, extra={"event": "tv.unavailable"})
        return self._connection_retry.hold()

    def answering(self) -> None:
        self._ended_on_the_store = False
        self._ended_unidentified = False
        if self._unavailable.end():
            log.info("the television is answering again", extra={"event": "tv.recovered"})
        self._connection_retry.clear()
        # Both, not either-or: a pass that completes ends whichever was said.
        busy_ended, failing_ended = self._store_busy.end(), self._store_failing.end()
        if busy_ended or failing_ended:
            log.info("the display store is answering again", extra={"event": "store.recovered"})
        self._store_retry.clear()

    def _the_store_failed(self, exc: StoreFault) -> float:
        """A pass ended on the store: say it once per episode, at the level its class earns, and wait.

        **The screen's state is left where it was**, unlike an unreachable set's:
        the store failing says nothing about what the wall shows, and the
        heartbeat's `last_error` carries the fault to the server.
        """
        self._ended_on_the_store = True
        fields = {"operation": exc.operation, "fault": exc.fault}
        if exc.may_clear:
            if self._store_busy.begin():
                log.warning(
                    "%s; the wall goes on at its next pass",
                    exc,
                    extra={"event": "store.busy", **fields},
                )
        elif self._store_failing.begin():
            log.error(
                "%s; the wall cannot record what it does until the disk or the file is fixed, and tries again",
                exc,
                extra={"event": "store.failed", **fields},
            )
        return self._store_retry.hold()

    def heartbeat_fields(self, *, reachable: bool | None) -> dict[str, Any]:
        return {
            "announced_content_id": self._announced_content_id,
            # A pass that ended on the store, or for want of the set's identity,
            # did not find out whether the set answers: the loop's
            # "unreachable" is about the pass, not the set.
            "television_reachable": None if self._ended_on_the_store or self._ended_unidentified else reachable,
            "television_showing_art": self._showing_art,
            # **The Frame draws no label**, so it reports none, as a screen does.
            # A client's panel is reported as a label output in the client
            # heartbeat, connected or not (`label_renderer.LabelPanel`).
            "has_label_surface": False,
            "label_surface_working": None,
        }

    def capabilities(self) -> Capabilities:
        """The Frame's configured panel, which this Player composes every picture for.

        It draws no text of its own until this Player burns a caption into the
        picture it uploads (wave 6+), so `none` is the only label mode it can
        claim.
        """
        return Capabilities(screen=self._settings.geometry.screen, backend="frame", label_modes=("none",))

    async def close(self) -> None:
        self._leave_the_scratch_store()
        await self._tv.close()

    # -- which set this is -------------------------------------------------

    async def _know_the_set(self) -> None:
        """Learn which set this connection reaches, and make the bindings in use that set's, or end the pass.

        Read once per connection (`_on`), so a pass on a connection already
        identified sends nothing. A set that will not say ends the pass
        (`SetUnidentified`) and is asked again on its own ladder, until
        `IDENTITY_PATIENCE_SECONDS` have gone by: then the wall goes on from an
        empty store in memory, still asking on the ladder, and returns to the
        device's store once the set says. **`_on` is set only after the store
        has made the bindings that set's**, so a store fault there ends the pass
        with nothing used, and the next reads the set again.
        """
        if self._on is not None:
            return
        if not self._identity_retry.is_due():
            if self._scratch is not None:
                return
            raise SetUnidentified(self._unidentified_reason, wait=self._settings.tv_retry_min_seconds)
        try:
            identity = await self._tv.read_identity()
        except IdentityUnreadable as exc:
            wait = self._identity_retry.hold()
            self._set_would_not_say(str(exc))
            if self._scratch is not None:
                return
            if self._clock.monotonic() - (self._unidentified_since or 0.0) < IDENTITY_PATIENCE_SECONDS:
                raise SetUnidentified(self._unidentified_reason, wait=wait) from exc
            self._go_on_unidentified()
            return
        met = self._store.belongs_to(identity)
        if self._scratch is not None:
            # The picture on the wall is one the store in memory holds: the
            # programme must put this store's up, rather than believe it is.
            self._store.forget_the_wall()
            self._leave_the_scratch_store()
        self._on = identity
        self._identity_retry.clear()
        self._unidentified_since = None
        self._going_on_unidentified.end()
        if self._unidentified.end():
            log.info("the television says which set it is again", extra={"event": "tv.identified"})
        self._met(met, identity)

    def _met(self, met: SetMet, identity: str) -> None:
        """Say what the store found the set to be, and start afresh on a set the bindings were not made on."""
        if met.change is SetChange.ADOPTED:
            log.info(
                "the display store did not record which television its pictures were put on; they are taken as this one's",
                extra={"event": "binding.set_adopted", "set_now": _known_as(identity)},
            )
        elif met.change is SetChange.CHANGED:
            log.warning(
                "the television at this address is not the one this wall's pictures were put on; its %d binding(s) are "
                "kept for that set, %d of this set's are back, and the wall starts on this set as on a first run. "
                "The other set keeps this Player's pictures until it is met again or they are removed by hand",
                met.set_aside,
                met.restored,
                extra={
                    "event": "binding.set_changed",
                    "set_was": _known_as(met.previous),
                    "set_now": _known_as(identity),
                    "set_aside": met.set_aside,
                    "restored": met.restored,
                },
            )
            self._forget_what_the_set_showed()
            # As a first run reconciles: the set's own list against its bindings.
            self._reconciliation_owed = True

    def _set_would_not_say(self, reason: str) -> None:
        """The identity read failed: note when the episode began, and say it once."""
        self._unidentified_reason = reason
        if self._unidentified_since is None:
            self._unidentified_since = self._clock.monotonic()
        if self._unidentified.begin():
            log.warning(
                "the television did not say which set it is (%s); the wall uses none of its pictures on it until it does",
                reason,
                extra={"event": "tv.unidentified"},
            )

    def _go_on_unidentified(self) -> None:
        """Show art on a set that will not say who it is, from an empty store in memory, removing nothing."""
        self._scratch = DisplayState(Path(":memory:"), now=self._clock.now)
        self._state = self._scratch
        self._forget_what_the_set_showed()
        if self._going_on_unidentified.begin():
            log.warning(
                "the television has not said which set it is for %d minutes; the wall shows art on it without the "
                "pictures it put on a set before, uploading what it shows, and removes nothing from it until it says",
                IDENTITY_PATIENCE_SECONDS // 60,
                extra={"event": "tv.unidentified_going_on"},
            )

    def _the_connection_may_have_gone(self) -> None:
        """A failure the seam reports as something else may have dropped the connection: identify the next one.

        **The store in memory is kept**: a refused upload is no outage, and it
        is only on one (`_forget_the_set`) that what was put up while the set
        would not say is let go.
        """
        self._on = None

    def _forget_the_set(self) -> None:
        """The connection went: whatever answers next is identified before a binding is used on it."""
        self._on = None
        if self._scratch is not None:
            # What was uploaded while the set would not say is on a set nobody
            # can name: not carried to whatever answers next.
            self._leave_the_scratch_store()

    def _leave_the_scratch_store(self) -> None:
        if self._scratch is not None:
            self._scratch.close()
            self._scratch = None
        self._state = self._store

    def _forget_what_the_set_showed(self) -> None:
        """Drop what this process learned of the set's wall, its brightness and its keeping, which were another set's."""
        self._wall_content_id = None
        self._announced_content_id = None
        self._announcement_taken = self._announcement
        self._kept_by_the_set.clear()
        self._brightness_at = None
        self._brightness_value = None

    def _may_remove(self) -> bool:
        """Whether a removal may be sent: only from a store whose recorded identity is the set's on this connection."""
        return self._on is not None and self._state.tv_identity == self._on

    # -- changing the wall -------------------------------------------------

    def may_attempt(self) -> bool:
        """Whether the wall may be asked to change again yet.

        False after an attempt the television took and did not act on, and after
        finding somebody else using the set. A television behaving normally in art
        mode has no wait, so nothing here delays a rotation or a jump in normal
        operation.

        **An announcement from the set clears the wait**, because it is news
        rather than a retry — the same rule a new manifest gets. Without it,
        somebody switching from a programme back to art mode would watch a blank
        wall for the remainder of a backoff that had grown to five minutes; with
        it, the next poll asks again and the picture comes back in about a second.
        The clearing happens in `_take_the_sets_news`, at the top of every pass
        that reaches the set, so this only reads the wait.
        """
        return self._wall.is_due()

    async def is_ours(self) -> bool:
        """Whether the set is showing art, and may therefore be asked to change it.

        **The television belongs to whoever is using it**
        (Arrt's `nonfunctional-requirements.md`). Selecting an image on a set showing a
        programme does not fail politely: it switches the set into art mode and
        takes the screen off the person watching. So nothing reaches the wall
        without asking first, and a no freezes everything — no selection, and the
        wall's memory of what is up left as it was — exactly as a wall that would
        not change does.

        **Asked only when something is about to happen**, which is what keeps a
        one-second poll from becoming a request per second: the schedule consults
        this only when the work it says to show is not the one on the wall. A no
        then backs off on the shared ladder, so a whole evening of television costs
        a handful of reads rather than thousands.
        """
        if await self._read_art_mode():
            return True
        self._hold_off_the_wall()
        return False

    async def show(self, picture: Picture) -> Shown:
        """Select one picture on the set, uploading it first if it has no binding.

        **`is_ours` said yes before this began, and that is not enough.** An
        upload here can take a minute, and somebody can switch to a programme
        inside it; the seam reads the set again immediately before every write,
        the selection included, and a refusal ends the attempt as a wall that did
        not change: nothing selected, nothing remembered, and the slot still owed
        when the set is showing art again.
        """
        try:
            content_id = await self._content_id_for(picture)
            if content_id is None:
                return Shown.SKIP

            try:
                shown = await self._tv.show(content_id)
            except TvUnavailable:
                content_id = await self._rebind_or_reraise(picture, content_id)
                if content_id is None:
                    return Shown.SKIP
                shown = await self._tv.show(content_id)
        except TvNotOurs as exc:
            await self._not_ours_after_all(exc)
            return Shown.WALL_UNCHANGED

        if not shown:
            await self._report_wall_unchanged(content_id)
            self._hold_off_the_wall()
            return Shown.WALL_UNCHANGED
        self._wall_is_answering()

        self._wall_content_id = content_id
        self._learned_showing_art(showing=True)
        self.record.moved_to(ScreenState.SHOWING_ART, picture.work_id)
        log.info(
            "showing %s",
            picture.title,
            extra={
                "event": "rotation.selected",
                "tv_content_id": content_id,
                "theme_id": picture.theme_id,
            },
        )
        return Shown.YES

    async def _not_ours_after_all(self, exc: TvNotOurs) -> None:
        """The seam held a write while the wall was being changed: the set is somebody's.

        Answered as `is_ours` answers a no, because it is the same news arriving
        later: said once, the wall's wait lengthened, and the display state moved
        to what the set is doing. A held selection reads `PowerState` to tell a
        programme from a dark panel, as `is_ours` does; a held upload already
        knows the panel is lit, or could not tell, which the display state
        reports as in use either way.
        """
        self._learned_showing_art(showing=False)
        if exc.verb == "show":
            await self._screen_is_not_ours()
        else:
            self._hold_the_writes(exc)
            self.record.moved_to(ScreenState.IN_USE)
        self._hold_off_the_wall()

    def _learned_showing_art(self, *, showing: bool) -> None:
        """Record what the set was found doing, and open or close the wall's not-ours episode with it.

        The one place `_showing_art` is set, so the field and the episode cannot
        disagree. It moves no wait: when the wall is asked again stays where it
        was (`_hold_off_the_wall`, `_wall_is_answering`).
        """
        self._showing_art = showing
        if not showing:
            if self._not_our_wall.begin():
                # Said once for the same reason a dark wall is: somebody watches
                # television for hours, and a line per attempt would bury the
                # ERRORs that are the Player's only failure channel.
                log.info(
                    "the television is not in art mode; leaving the wall alone until it is",
                    extra={"event": "rotation.wall_not_ours"},
                )
        elif self._not_our_wall.end():
            log.info("the television is showing art again; resuming the rotation", extra={"event": "rotation.wall_returned"})

    def _hold_the_writes(self, exc: TvNotOurs) -> None:
        """The seam held a write for somebody watching: wait before trying any write again, said once per hold."""
        self._writes.hold()
        if self._writes.begins():
            # Said once, at INFO, for the same reason a wall not ours is.
            log.info(
                "%s; holding the set's uploads, removals and brightness until it is showing art or switched off",
                exc,
                extra={"event": "tv.writes_held", "verb": exc.verb},
            )

    def _write_went_through(self) -> None:
        """A write reached the set, so the set is taking them: end any hold."""
        self._writes.clear()

    def _hold_off_the_wall(self) -> None:
        """Back off before asking again, and lengthen the wait each time.

        Bounded by the same ceiling as a reconnection, so a night with the panel
        off costs a handful of attempts rather than one a second — and the wall
        resumes within that ceiling of somebody switching the set back on.
        """
        self._wall.hold()

    def _wall_is_answering(self) -> None:
        """Forget the backoff, because the set is acting on what it is told."""
        self._wall.clear()
        self._writes.clear()

    async def _report_wall_unchanged(self, content_id: str) -> None:
        """Say once that the set is taking selections and displaying none of them.

        **The flag is read here for the operator, and separately before every
        selection for the wall's own safety.** The two readings answer different
        questions and neither replaces the other: the gate asks *may this
        television be touched at all*, and this line answers *why is it not
        changing* for somebody reading the journal. This one costs a call on a
        rotation that has already failed, so it is free in the ordinary case.
        """
        if not self._wall.begins():
            return
        mode = await self._tv.reported_art_mode()
        if mode is None:
            # The read may have failed and taken the connection with it.
            self._the_connection_may_have_gone()
        log.warning(
            "the television accepted %s and is not displaying it; "
            "it reports art mode %s. The next picture waits until the wall changes",
            content_id,
            mode if mode is not None else "nothing at all",
            extra={
                "event": "rotation.wall_unchanged",
                "tv_content_id": content_id,
                "art_mode": mode,
            },
        )

    # -- what the set says -------------------------------------------------

    def _note_announcement(self, announcement: SelectionAnnouncement) -> None:
        """Remember what the set says is on its wall. Runs on the client's reader task.

        Deliberately the cheapest thing that could work: one assignment, no I/O,
        no lock, nothing that can raise. Everything this could trigger happens on
        the wall's own task instead — see `_take_the_sets_news`.

        **It records announcements the Player did not cause**, which is the point
        of subscribing at all: somebody using the remote changes the wall, and
        the display state — which every label of the wall follows — should say
        what is actually up rather than what we last put there.
        """
        self._announced_content_id = announcement.content_id
        self._announcement = announcement

    async def _take_the_sets_news(self) -> None:
        """Act, on the wall's own task, on what the set has said since the last pass.

        Two kinds of news, both already heard and neither a new question put to
        the set on a timer. **A picture announced as shown** is the wall changing
        — the Player's own selection echoed back, or somebody with the remote —
        and it is art on the wall, resolved to a work through the bindings. **An
        art-mode announcement** (which connecting also counts as) is the set
        saying its mode may have changed: it clears the wall's wait, as it always
        has, and is now also answered with one fresh `get_artmode` read, so the
        display state follows the set within a pass of the set saying so rather
        than at the next rotation. One read per announcement, never per poll.
        """
        announcement = self._announcement
        if announcement is not None and announcement is not self._announcement_taken:
            # The work is resolved before the news is marked taken: the store
            # can fail that read, and news marked taken first would be lost.
            work_id = self._work_bound_to(announcement.content_id) if announcement.is_shown else None
            self._announcement_taken = announcement
            if announcement.is_shown:
                self._wall_content_id = announcement.content_id
                self._learned_showing_art(showing=True)
                self.record.moved_to(ScreenState.SHOWING_ART, work_id)
        # Collected every pass, whether or not news is already owed, so the set's
        # flag never waits to cost a second read for the same news.
        announced = self._tv.art_mode_announcement_pending()
        if announced or self._art_mode_news_owed:
            self._art_mode_news_owed = True
            # Clears the writes' wait too (`_wall_is_answering`): the set saying
            # its mode changed is news to a held upload as much as to the wall.
            self._wall_is_answering()
            await self._read_art_mode()
            self._art_mode_news_owed = False

    def _work_bound_to(self, content_id: str) -> str | None:
        """The work this device put on the set under `content_id`, or None for a picture it did not put there."""
        for binding in self._state.bindings():
            if binding.tv_content_id == content_id:
                return binding.artwork_id
        return None

    async def _read_art_mode(self) -> bool:
        """Ask the set whether it is showing art, and let the display state follow the answer.

        A yes is art on the wall: the picture last known to be there, which may
        be one nothing here can name. A no is somebody else's screen or a dark
        one, and only `PowerState` tells those apart — see `_screen_is_not_ours`.
        """
        showing = await self._tv.showing_art()
        self._learned_showing_art(showing=showing)
        if showing:
            # Art on the wall admits every write, so nothing held need wait.
            self._writes.clear()
            content_id = self._wall_content_id
            self.record.moved_to(ScreenState.SHOWING_ART, self._work_bound_to(content_id) if content_id is not None else None)
        else:
            await self._screen_is_not_ours()
        return showing

    async def _screen_is_not_ours(self) -> None:
        """`get_artmode` said no: report the set in use, or dark if `PowerState` says standby.

        **Read only here, straight after a `get_artmode` no**, so it adds no
        cadence of its own: it rides a read this loop was already taking. Both
        states blank the label, so the read decides only what Walls says; a
        failed read therefore reports `in_use` — the screen is somebody's or off,
        and of the two, "in use" is the one that cannot wrongly tell a curator a
        lit screen is off — and says so once per episode.
        """
        try:
            power = await self._tv.power_state()
        except PowerStateUnreadable as exc:
            if self._power_unreadable.begin():
                log.info(
                    "could not read whether the television's panel is lit (%s); reporting it in use",
                    exc,
                    extra={"event": "display.power_unreadable"},
                )
            self.record.moved_to(ScreenState.IN_USE)
            return
        if self._power_unreadable.end():
            log.info("the television's panel power can be read again", extra={"event": "display.power_readable"})
        self.record.moved_to(ScreenState.DARK if power == "standby" else ScreenState.IN_USE)

    # -- bindings ----------------------------------------------------------

    async def _content_id_for(self, picture: Picture) -> str | None:
        """This work's id on the television, uploading it now if it has none."""
        binding = self._state.binding_for(picture.work_id)
        if binding is not None and _is_current(binding, picture.path):
            return binding.tv_content_id
        if self._too_soon_to_retry(binding):
            return None
        return await self._upload(picture)

    def _too_soon_to_retry(self, binding: Binding | None) -> bool:
        """Whether a work that failed to upload should be left alone this pass.

        **A set that is reachable and refuses one image does not back off with the
        connection**, which is the other failure and has its own retry. Without
        this, one bad render is a round trip, a WARNING and a rewritten row every
        second for as long as it stays in the theme — an unbounded small-write
        source on the SD card that Arrt's `observability-strategy.md` says not to build,
        and a journal in which the rate limiter starts dropping the lines that
        matter.

        Measured against wall time, because the failure is recorded in the store
        and must outlive the process: under `Restart=always` an elapsed-time wait
        would reset on every restart, so a crash loop would become a retry loop.
        """
        if binding is None or binding.upload_status is not UploadStatus.FAILED:
            return False
        waited = (self._clock.now() - binding.uploaded_at).total_seconds()
        return waited < self._settings.upload_retry_seconds

    async def _rebind_or_reraise(self, picture: Picture, refused: str) -> str | None:
        """Work out whether the set is gone or the *binding* is, and fix the second.

        **A refused selection is ambiguous and the library cannot disambiguate
        it** — a dead websocket and an id the set has never heard of arrive as the
        same failure. Guessing "outage" is the expensive mistake: somebody
        removing one image from the phone app would freeze the wall on a backoff
        that retries the same doomed id forever, because the manifest never
        changes and nothing else would ever re-check that binding.

        So the question is put to the television, which is this codebase's
        standing answer to a client whose return values cannot be trusted in
        either direction: if the set still lists the id, the fault is not the
        binding and the original failure stands. If the listing itself fails,
        that is a real outage and it propagates from here.

        **The connection is re-established first, and without that this whole
        method is unreachable.** The failure that sends us here arrives from the
        television client, which drops and closes its connection on *any* failure
        — it holds a websocket whose state after an error is not knowable, so it
        refuses to reason on it. The very next request would therefore raise "not
        connected" rather than reaching the set, the outage arm would swallow it,
        and every branch below would be dead code. Reconnecting costs nothing when
        the set is there and raises the real outage when it is not, which is
        exactly the distinction being drawn.
        """
        await self._connected()
        listed = await self._tv.listed_content_ids()
        if refused in listed:
            raise TvUnavailable(f"the television refused to select {refused}, which it says it is holding")

        self._state.mark_orphaned(picture.work_id)
        log.warning(
            "the television refused %s and does not list it; re-uploading %s",
            refused,
            picture.work_id,
            extra={"event": "binding.orphaned", "tv_content_id": refused},
        )
        return await self._upload(picture)

    async def _upload(self, picture: Picture) -> str | None:
        """Send one render to the television and record what happened.

        A failure is recorded, not merely logged: the store keeps `failed` apart
        from "no row at all", so a work that fails every pass is visible in the
        device's own state rather than only in a journal that does not survive a
        reboot. **A held upload is not a failure** and is not recorded: the set
        was never sent it, and `TvNotOurs` goes to the caller, whose answer
        differs (the theme waits; a selection ends).
        """
        fingerprint = _fingerprint(picture.path)
        try:
            content_id = await self._tv.upload(picture.path)
        except TvUploadFailed as exc:
            # A set that went away while the upload was read back is reported
            # this way, and its connection with it.
            self._the_connection_may_have_gone()
            self._state.record_upload_failure(picture.work_id)
            log.warning(
                "could not put %s on the television (%s)",
                picture.work_id,
                exc,
                extra={"event": "binding.upload_failed"},
            )
            return None

        self._state.record_upload(picture.work_id, content_id, render_fingerprint=fingerprint)
        self._write_went_through()
        log.info(
            "uploaded %s to the television as %s",
            picture.work_id,
            content_id,
            extra={"event": "binding.uploaded", "tv_content_id": content_id},
        )
        return content_id

    async def _upload_one_pending(self, pictures: Sequence[Picture]) -> None:
        """Carry one not-yet-uploaded work per pass, so the theme fills in behind us.

        Deliberately one, not all: see this module's opening note. A pass that
        uploaded the whole theme would hold the loop — and every slot change and
        republished schedule — for as long as the theme is long.
        """
        if not self._writes.is_due():
            return
        for picture in pictures:
            binding = self._state.binding_for(picture.work_id)
            if _is_current(binding, picture.path):
                continue
            if self._too_soon_to_retry(binding):
                continue
            if not picture.path.is_file():
                # Not a failure to record: nothing was attempted, and writing a
                # `failed` row for a file not there yet (a major 1 render the pull
                # has not cached, or a major 2 work this Player has not composed)
                # would make the store report an upload problem for a preparation one.
                continue
            with work_context(picture.work_id):
                try:
                    await self._upload(picture)
                except TvNotOurs as exc:
                    self._hold_the_writes(exc)
            return

    async def _reconcile_with_the_set(self, pictures: Sequence[Picture]) -> bool:
        """Compare what this device believes against what the television lists.

        Runs when a manifest is adopted, and when the hourly look over the
        bindings finds one due to retire (`_save_feed_time`), never every pass.
        The comparison costs a real request to the set, and at a one-second poll
        that would be a call per second forever — traffic that buys nothing,
        since nothing changes what the television holds except this process.
        """
        listed = await self._tv.listed_content_ids()

        for picture in pictures:
            binding = self._state.binding_for(picture.work_id)
            if binding is None or not binding.is_on_the_television:
                continue
            if binding.tv_content_id in listed:
                continue
            # The set stopped listing an image this device uploaded — removed from
            # the phone app, or forgotten across a factory reset. Recorded as
            # orphaned so the row cannot be mistaken for a live binding, which
            # would otherwise send `select_image` at an id the set does not know.
            self._state.mark_orphaned(picture.work_id)
            log.warning(
                "the television no longer lists %s for work %s; it will be uploaded again",
                binding.tv_content_id,
                picture.work_id,
                extra={"event": "binding.orphaned", "tv_content_id": binding.tv_content_id, "work_id": picture.work_id},
            )

        return await self._remove_orphans(listed, pictures)

    async def _remove_orphans(self, listed: frozenset[str], pictures: Sequence[Picture]) -> bool:
        """Take off the television everything this device cannot account for, and every work due to retire.

        **The binding table is the whole authority**, which is why a fresh install
        clears the set: images uploaded by a previous generation of this product
        are not assets to adopt, because nothing joins them to a work — the 2024
        tree addressed images by source URL and by a resized filename, and a
        manifest entry names an artwork id and a UUID render. Adopting one would
        bind a work to a picture that is not the render its entry names, and the
        wall would show one composition while the catalogue recorded another.

        The television has exactly one user-upload category, so the alternative to
        removing them is two generations of the same corpus accumulating in it.

        **One removal for both**, so the gate reads the set once. A retiring
        work's image is asked for only while the set lists it and no other
        binding names the same id; otherwise only its row goes. So a removal
        whose row could not be dropped is never sent twice (the set's list says
        it is gone, which is the success it looks like), and a row left behind
        under an id the set has since given another work never takes that work's
        picture with it. The rows go after the set's answer, never before, so a
        removal held or unconfirmed leaves every one in place, still accounted
        for and still owed.
        """
        if not self._may_remove():
            # A store that does not know it is this set's accounts for nothing
            # here: its ids may name another set's pictures, and an id it does
            # not hold may be one of this set's the Player put there.
            return True
        accounted = self._state.accounted_content_ids()
        orphans = sorted(listed - accounted)
        retiring, claimed = self._retirements(pictures)
        on_the_set = {
            binding.tv_content_id: binding
            for binding in retiring
            if binding.tv_content_id is not None and binding.tv_content_id in listed and binding.tv_content_id not in claimed
        }
        retiring_ids = sorted(on_the_set)
        if not orphans and not retiring_ids:
            self._retire(retiring, removed=(), kept=())
            return True

        try:
            outcome: RemovalOutcome = await self._tv.remove([*orphans, *retiring_ids])
        except TvRemovalUnconfirmed as exc:
            # A list that could not be read back may have cost the connection.
            self._the_connection_may_have_gone()
            # Unknown is reported as unknown. The images stay listed and the next
            # pass tries again; claiming either outcome here would be a guess, and
            # this is the verb whose reply the library discards.
            if orphans:
                log.warning(
                    "could not establish whether %d unaccounted-for images were removed (%s)",
                    len(orphans),
                    exc,
                    extra={"event": "tv.orphan_removal_unconfirmed", "requested": orphans},
                )
            if retiring_ids:
                log.warning(
                    "could not establish whether the images of %d work(s) no feed names any more were removed (%s)",
                    len(retiring_ids),
                    exc,
                    extra={"event": "binding.retirement_unconfirmed", "requested": retiring_ids},
                )
            return False

        self._write_went_through()
        surviving = set(outcome.surviving)
        if orphans:
            log.info(
                "removed %d image(s) the binding table does not account for",
                len([content_id for content_id in orphans if content_id not in surviving]),
                extra={
                    "event": "tv.orphans_removed",
                    "removed": [content_id for content_id in orphans if content_id not in surviving],
                    "surviving": [content_id for content_id in orphans if content_id in surviving],
                },
            )
        self._retire(
            retiring,
            removed=tuple(content_id for content_id in retiring_ids if content_id not in surviving),
            kept=tuple(on_the_set[content_id] for content_id in retiring_ids if content_id in surviving),
        )
        # **Settled even when some survived.** An incomplete removal is a *known*
        # outcome — the set was asked, answered, and is keeping them — and it is
        # already reported at WARNING by the client. Asking again immediately
        # learns nothing; the next manifest re-arms the work. Only an outcome
        # nobody could establish stays owed.
        return True

    def _retirements(self, pictures: Sequence[Picture]) -> tuple[tuple[Binding, ...], frozenset[str]]:
        """The bindings due to retire now, and the content ids every other binding names.

        Due: no adopted feed has named the work for `RETIRED_AFTER_SECONDS` of the
        wall running with a feed. **Never due**: a work the feed names now (the
        schedule's whole horizon, its scene, its staging: everything the wall is
        about to show), the work the Player last put up, the picture the set
        last said is on its wall, a work whose image the set kept when asked
        since the last feed, and a binding whose last naming is unknown.
        """
        named = {picture.work_id for picture in pictures}
        on_the_wall = {self._wall_content_id, self._announced_content_id} - {None}
        now = self._state.feed_seconds
        retiring: list[Binding] = []
        claimed: set[str] = set()
        for binding in self._state.bindings():
            if self._is_due_to_retire(binding, named=named, on_the_wall=on_the_wall, now=now):
                retiring.append(binding)
            elif binding.tv_content_id is not None:
                claimed.add(binding.tv_content_id)
        return tuple(retiring), frozenset(claimed)

    def _is_due_to_retire(self, binding: Binding, *, named: set[str], on_the_wall: set[str | None], now: float) -> bool:
        if binding.artwork_id in named or binding.artwork_id == self._state.last_selected_work_id:
            return False
        if binding.tv_content_id in on_the_wall or binding.artwork_id in self._kept_by_the_set:
            return False
        last = binding.last_named_feed_seconds
        if last is None:
            return False
        left = self._left_at.get(binding.artwork_id)
        if left is not None:
            last = max(last, left)
        return now - last >= RETIRED_AFTER_SECONDS

    def _retire(self, retiring: Sequence[Binding], *, removed: Sequence[str], kept: Sequence[Binding]) -> None:
        """Say what left the set, then drop the rows of every retiring work whose image is off it or never was.

        Said before the rows go, so the journal records what was taken off the
        set even when the store then fails to drop a row; that row goes at the
        next reconciliation, said then as already off the set.
        """
        if not retiring:
            return
        keeping = {binding.artwork_id for binding in kept}
        going = [binding for binding in retiring if binding.artwork_id not in keeping]
        self._kept_by_the_set.update(keeping)
        log.info(
            "retired %d work(s) no feed has named for %d days; took %d image(s) off the television",
            len(going),
            RETIRED_AFTER_SECONDS // 86400,
            len(removed),
            extra={
                "event": "binding.retired",
                "retired": [binding.artwork_id for binding in going],
                "removed": list(removed),
                "kept_by_the_set": sorted(keeping),
            },
        )
        for binding in going:
            self._state.retire(binding.artwork_id, binding.tv_content_id)
            self._left_at.pop(binding.artwork_id, None)

    # -- how long the wall has had a feed ------------------------------------

    def _count_feed_time(self) -> None:
        """Count the elapsed time since the last pass as the wall running with a feed. Memory only.

        Called only by a pass with a feed adopted, and **on the elapsed clock**,
        which neither a Player switched off nor an NTP step can move: the first
        pass of a process counts nothing, however far either clock has moved.
        """
        elapsed = self._clock.monotonic()
        if self._feed_time_read_at is not None:
            self._state.count_feed_time(max(0.0, elapsed - self._feed_time_read_at))
        self._feed_time_read_at = elapsed

    def _save_feed_time(self, pictures: Sequence[Picture]) -> None:
        """Once an hour of the count, write it, and look the bindings over for one due to retire.

        **Tried once per interval, whether or not the write succeeds**, so a
        store that refuses writes costs one failed pass an hour rather than
        every pass: the count and the leavings are kept in memory meanwhile,
        and a retirement found due is made at the next reconciliation, through
        the set's gate like any other removal.
        """
        if self._state.feed_seconds - self._feed_time_saved_at < FEED_TIME_SAVED_EVERY_SECONDS:
            return
        self._feed_time_saved_at = self._state.feed_seconds
        self._state.save_feed_time(named=[picture.work_id for picture in pictures], left=self._left_at)
        self._left_at.clear()
        if self._retirements(pictures)[0]:
            self._reconciliation_owed = True

    # -- the television ----------------------------------------------------

    async def _connected(self) -> None:
        """Ensure there is a live connection, and that the set's own slideshow is off.

        A slideshow switch the seam holds stays unmarked and is tried again once
        the hold is over; it never stops the pass, because nothing else here
        depends on it having been sent.
        """
        await self._tv.connect()
        # A dropped connection may come back to another set, which is known
        # before the slideshow switch, or any binding, is sent to it.
        self._forget_the_set()
        await self._know_the_set()
        await self._switch_off_the_slideshow()

    async def _switch_off_the_slideshow(self) -> None:
        """Turn the set's own slideshow off, once, unless the seam is holding writes."""
        if not self._state.native_slideshow_disabled and self._writes.is_due():
            try:
                await self._tv.disable_native_slideshow()
            except TvNotOurs as exc:
                self._hold_the_writes(exc)
                return
            self._state.mark_native_slideshow_disabled()
            self._write_went_through()
            log.info(
                "disabled the television's own slideshow, which would otherwise change the picture underneath us",
                extra={"event": "tv.native_slideshow_disabled"},
            )

    async def _apply_brightness(self) -> None:
        """Follow the sun, and write to the set only when the value actually changes.

        A change the seam holds is not remembered as written, so the next
        interval tries it again.
        """
        elapsed = self._clock.monotonic()
        if self._brightness_at is not None and elapsed - self._brightness_at < self._settings.brightness_interval_seconds:
            return
        self._brightness_at = elapsed

        value, state = brightness_module.level_at(
            self._clock.now(),
            latitude=self._settings.latitude,
            longitude=self._settings.longitude,
            location_name=self._settings.location_name,
            location_region=self._settings.location_region,
            minimum=self._settings.tv_min_brightness,
            maximum=self._settings.tv_max_brightness,
        )
        if value == self._brightness_value or not self._writes.is_due():
            return

        try:
            await self._tv.set_brightness(value)
        except TvNotOurs as exc:
            self._hold_the_writes(exc)
            return
        self._brightness_value = value
        self._write_went_through()
        log.info(
            "set panel brightness to %d",
            value,
            extra={
                "event": "brightness.set",
                "brightness": value,
                "solar_angle": round(state.solar_angle_degrees, 2),
            },
        )


def _fingerprint(render: Path) -> str | None:
    """What this picture's bytes are: their SHA-256, read once per change of the file.

    **The bytes, not the file's modification time.** A picture whose work left
    the feed is removed from the composed directory, and composed again under
    the same name when the work returns: the same pixels in a new file. Keyed on
    the file's stamp, that read as a new picture and re-uploaded every work of a
    theme hung again after an evening away. A file rewritten in place with
    different pixels still reads as changed.

    **Hashed only when the file's stamp moves** (`_digest` is cached on it), so a
    pass over every picture costs a `stat` each, as it did; a hash is paid once
    per file written, and the upload reads the same bytes anyway.

    None when the file cannot be read, which is treated as "unknown" and never as
    "unchanged" — see `_render_changed`.
    """
    try:
        stat = render.stat()
        return _digest(render, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


@functools.lru_cache(maxsize=512)
def _digest(render: Path, mtime_ns: int, size: int) -> str:  # noqa: ARG001 -- the stamp is the cache's key
    """The file's SHA-256, cached on its stamp; a theme holds far fewer pictures than the cache."""
    with render.open("rb") as picture:
        return f"sha256:{hashlib.file_digest(picture, 'sha256').hexdigest()}"


def _stamp(render: Path) -> str | None:
    """The fingerprint a Player before schema 3 recorded: the file's modification time and size."""
    try:
        stat = render.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _is_current(binding: Binding | None, render: Path) -> bool:
    """Whether this work's picture is already on the television, and still right.

    **One question with two callers**, which is why it is a function rather than
    a condition written twice: one caller decides whether a content id can be
    reused, the other whether an upload still needs carrying, and they are the
    same question asked from opposite ends. Written out twice they drifted once
    already — the fingerprint half was added because the first reading was
    incomplete, and had it been added to only one of the two the wall would show
    a stale composition for exactly as long as nobody looked.
    """
    return binding is not None and binding.is_on_the_television and not _render_changed(binding, render)


def _render_changed(binding: Binding, render: Path) -> bool:
    """Whether the file on disk is no longer the one the television was given.

    **The defect this closes is invisible from the wall.** A binding names a
    work, not a file, so a picture rewritten under a name the binding already
    holds would leave it reading `uploaded` while the television went on showing
    the old one for ever. A composed picture is named by its composition key, so
    a new mat or new media arrives under a new name instead; the fingerprint is
    what notices a change either way, including a file rewritten in place.

    A binding with no recorded fingerprint — every row written before the column
    existed — counts as changed. That costs one re-upload per work on the first
    pass after an upgrade, which is the honest price of never having looked.

    **A row recorded by an earlier Player holds the file's stamp** (`_stamp`),
    and is unchanged while the file still has it, so the upgrade itself uploads
    nothing; once the file is written again, the stamp no longer matches and the
    work is sent once, as that Player would have sent it.
    """
    recorded = binding.render_fingerprint
    if recorded is None:
        return True
    if recorded == _fingerprint(render):
        return False
    return recorded != _stamp(render)


def _known_as(identity: str | None) -> str | None:
    """A set's identity as the journal names it: enough to tell two sets apart, and not the set's own id.

    The full id goes to the server in the client heartbeat; a journal is what
    gets pasted into an issue in a public repository.
    """
    return None if identity is None else f"set-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"


def _say_the_wall_recovered() -> None:
    log.info(
        "the wall may be asked to change again: the television took a selection or said its mode changed",
        extra={"event": "rotation.wall_recovered"},
    )


def _say_the_writes_resumed() -> None:
    log.info("the television's uploads are no longer held", extra={"event": "tv.writes_resumed"})


class _WaitedEpisode:
    """A wait before trying again, and the journal's episode of the condition that set it: one object.

    **Whatever clears the wait ends the episode**, once, and the next time the
    condition arises it is said again. Kept as two fields, a wait cleared by the
    set's news and an episode ended only by a write (or a selection that took)
    disagreed: the set came back with nothing left to send, the episode stayed
    open, and the next evening's line was never written. The wall's wait
    (`rotation.wall_unchanged`) and the writes' (`tv.writes_held`) are both one
    of these. The line that begins an episode is the caller's, because each needs
    something only the caller has (the refused verb; a read of the set), so
    `begins` answers whether to write it; the line that ends one is `ended`.

    `hold` and `begins` are separate so a caller keeps the order it had: the
    still wall's line reads the set before the wait is taken.
    """

    __slots__ = ("_ended", "_episode", "_wait")

    def __init__(self, wait: Backoff, *, ended: Callable[[], None]) -> None:
        self._wait = wait
        self._ended = ended
        self._episode = ReportOnce()

    def is_due(self) -> bool:
        """Whether the thing waited on may be tried now: no wait is on, or it is over."""
        return self._wait.is_due()

    def hold(self) -> None:
        """Wait before trying again, and longer next time."""
        self._wait.hold()

    def begins(self) -> bool:
        """True when this opens an episode, so the caller says it; False while one is open."""
        return self._episode.begin()

    def clear(self) -> None:
        """Forget the wait, and end an episode that was said."""
        self._wait.clear()
        if self._episode.end():
            self._ended()
