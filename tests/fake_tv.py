"""A television that behaves like the real one without being one.

**Subclasses `TvClient` rather than duck-typing it**, which is the whole reason
the interface is an abstract base: a verb added to the boundary fails this class
at import instead of quietly leaving the fake behind while every test stays green.

It models the set's *observable* behaviour, not its protocol: images live in one
flat category under ids the set invents, selecting one that is not there is an
error, and each failure the real client can produce can be armed on demand.

**It implements only the `_send_*` half of each write.** The art-mode gate is
`TvClient`'s and runs above this, against this double's own `showing_art` and
`power_state`, so every test through the double runs through the real gate. What
a selection does when it gets past the gate onto a set that is not showing art is
the set's measured behaviour, and is modelled in `_send_show`. What the other
writes do in the television state has never been measured, so they do here what
they do in art mode; nothing in this suite may depend on that.

**And it has time**, so a state can change while a verb is running: `seconds_for`
arms how long a verb takes, and `at` arms somebody picking up the remote part-way
through. What
it deliberately does **not** model is the library's misreporting — an upload that
lands while returning None is corrected inside `SamsungTv`, below this seam, and a
fake that reproduced it here would be testing the correction twice and the
daemon's behaviour not at all.
"""

from pathlib import Path
from typing import Final

from arrt_player.tv import (
    IdentityUnreadable,
    PowerStateUnreadable,
    RemovalOutcome,
    SelectionAnnouncement,
    SelectionObserver,
    TvClient,
    TvRemovalUnconfirmed,
    TvUnavailable,
    TvUploadFailed,
)

#: A picture the set holds that this product did not put there — an art-store
#: image, in the category the daemon neither uploads to nor removes from. The set
#: is displaying one of these before the daemon has shown anything.
FOREIGN_IMAGE: Final[str] = "SAM-F0000"

#: What somebody in the room can change about the set while it works: whether it
#: shows art (`art_mode`) and whether its panel is lit (`power`). Nothing else,
#: because nothing else changes without this product asking.
_CHANGEABLE: Final[frozenset[str]] = frozenset({"art_mode", "power"})


class FakeTv(TvClient):
    """One television's worth of observable state, plus arming for each failure."""

    def __init__(self) -> None:
        #: content id -> the file that was uploaded under it.
        self.holding: dict[str, Path] = {}
        self.selected: list[str] = []
        self.brightness: list[int] = []
        self.removed: list[tuple[str, ...]] = []
        self.connects = 0
        #: How many times the connection was let go. The art channel being closed
        #: on the way out is load-bearing: the set holds an abandoned client's slot
        #: for minutes, so a daemon that skipped it could not reconnect after a crash.
        self.closed = 0
        self.slideshow_disabled = 0
        #: Every file the set accepted an upload of, in order. Kept apart from
        #: `holding`, which a removal or a forgetting set shrinks.
        self.uploaded: list[Path] = []
        #: How many selections took the screen off somebody watching a programme.
        #: **Measured on 2026-08-07**: `select_image` sent to a set showing a
        #: programme switches it into art mode (`samsung-tv-state-findings.md`
        #: § Selecting an image takes the screen). A double that just recorded the
        #: selection let a suite assert "nothing was selected" without being able
        #: to see what selecting would have cost.
        self.took_the_screen = 0
        self._next_id = 0
        self._connected = False

        #: **Time, so that "while the upload ran" is a thing a test can say.** The
        #: set's own working seconds, advanced only by verbs armed to take time in
        #: `seconds_for`, never by the wall's clock: an upload costs the real set
        #: the better part of ten seconds, and somebody can pick up the remote
        #: inside them. Every verb is instantaneous unless armed.
        self.seconds_for: dict[str, float] = {}
        self.elapsed = 0.0
        #: Changes somebody makes to the set, each at a point in its working time.
        #: Armed through `at`.
        self._changes: list[tuple[float, dict[str, object]]] = []

        #: Everyone listening for what this set says about its wall, and every
        #: announcement it has made. **Both survive `close()`**, matching the real
        #: client, where observers belong to the object and the library callback
        #: is re-registered per connection — a fake that dropped them on
        #: reconnection would hide a subscriber lost to an overnight outage.
        self.selection_observers: list[SelectionObserver] = []
        #: How many observers raised while being told. Counted rather than
        #: swallowed silently, so a test can assert the isolation happened rather
        #: than merely that nothing exploded.
        self.observer_failures = 0
        self.announced: list[SelectionAnnouncement] = []

        #: Armed failures. Each is the real client's behaviour, named for what a
        #: test is trying to reproduce rather than for the exception it raises.
        self.unavailable = False
        self.refuse_uploads = False
        self.removal_unconfirmable = False
        #: Ids the set will not part with, so "removal reported, image survived"
        #: is reachable without making the whole call fail.
        self.unremovable: set[str] = set()
        #: Ids the set lists and still refuses to select. A real one has been
        #: observed refusing calls it should accept, and the daemon has to tell
        #: that apart from an id the set has simply forgotten — the two arrive as
        #: the same exception and want opposite responses.
        self.refuse_selection_of: set[str] = set()

        #: The set takes every selection and displays none of them. **Observed on
        #: a real television on 2026-08-07**, with its panel dark: `select_image`
        #: returned, raised nothing and emitted no event, while what the set
        #: displayed did not change over twelve seconds and repeated attempts.
        #: Armed here because the failure is invisible from the call — a fake that
        #: could not reproduce it let the daemon report changes of picture that never
        #: happened, and every test passed.
        self.displays_nothing_selected = False
        #: What the set is displaying. It starts on a picture of the set's own
        #: rather than on nothing, because **a Frame is never displaying nothing**
        #: — the one observed refusing selections went on showing an art-store
        #: image throughout. That is what makes the failure detectable: the
        #: observable is that the wall did not *change*, not that it is blank.
        self.displaying: str | None = FOREIGN_IMAGE
        #: Ids the set announces as selected while reporting `is_shown: "No"`. A
        #: distinct arming from `displays_nothing_selected`, which is silence: the
        #: set answering "I took it and I am not showing it" is a different wire
        #: behaviour from it never answering, and both mean the wall did not move.
        self.admits_not_showing: set[str] = set()
        #: The set's own art-mode flag, as it would report it. `"on"` by default
        #: because art mode is the deployment's normal condition; set it to
        #: `"off"` to model a television somebody is watching or a dark panel,
        #: **both of which the wall must not touch** — selecting on a set showing
        #: a programme switches it into art mode and takes the screen.
        self.art_mode: str | None = "on"
        #: Whether the set has announced an art-mode change nobody has collected
        #: yet. Armed by a test to model the set saying "I am in art mode now".
        self.art_mode_announced = False
        #: How many times the set has been asked whether it is showing art.
        self.art_mode_reads = 0
        #: The set's REST `PowerState`: `"on"` for a lit panel (art mode or a
        #: programme alike), `"standby"` for a set switched off at the remote.
        self.power = "on"
        #: Armed to make the `PowerState` read fail while the art channel answers,
        #: which the real seam reports as unreadable rather than as an outage.
        self.power_unreadable = False
        self.power_reads = 0
        #: The set's `device.duid`, or None for a set whose description carries
        #: none. Read without a connection, as the real read is.
        self.device_id: str | None = "uuid:frame-in-the-living-room"
        self.identity_reads = 0

    async def connect(self) -> None:
        """Cheap once connected, exactly as the real client is.

        **This is load-bearing and was got wrong first.** `SamsungTv.connect`
        returns immediately when it already holds a client, so in production a set
        that goes away *after* a connection is established is discovered by the
        next real call — `select_image`, `upload` — and not by `connect`. A fake
        that re-checked reachability every pass made the outage arrive at the top
        of the tick instead, so everything a pass does after the top of the tick
        was unreachable while the set was away: two tests asserting that a change
        survives an outage passed because the code under test never ran. A
        mutation sweep found it by swapping two statements neither test executed.
        """
        if self._connected:
            return
        self._check_reachable()
        self.connects += 1
        self._connected = True
        # **A connection is itself news, as the real client says it is**: the set
        # may have changed state while nobody could hear it, so `SamsungTv.connect`
        # leaves an art-mode announcement pending, and so does this.
        self.art_mode_announced = True

    async def close(self) -> None:
        self.closed += 1
        self._connected = False

    async def _send_disable_native_slideshow(self) -> None:
        self._check_usable()
        self.slideshow_disabled += 1

    async def listed_content_ids(self) -> frozenset[str]:
        self._check_usable()
        return frozenset(self.holding)

    async def _send_upload(self, path: Path) -> str:
        self._check_usable()
        if self.refuse_uploads:
            raise TvUploadFailed(f"{path.name} is not on the television after an upload attempt")
        self._spend("upload")
        self._next_id += 1
        content_id = f"MY-F{self._next_id:04d}"
        self.holding[content_id] = path
        self.uploaded.append(path)
        return content_id

    async def _send_show(self, content_id: str) -> bool:
        self._check_usable()
        if content_id not in self.holding:
            # The real set answers an unknown id with an error rather than a
            # blank wall, and a fake that accepted anything would let a daemon
            # bug — selecting an orphaned binding — pass every test.
            raise self._dropping(TvUnavailable(f"the television does not hold {content_id}"))
        if content_id in self.refuse_selection_of:
            raise self._dropping(TvUnavailable(f"the television refused {content_id}"))
        self.selected.append(content_id)
        if self.art_mode != "on":
            if self.power == "standby":
                # The dark state: accepted and ignored, with no announcement —
                # measured, and the same silence as `displays_nothing_selected`.
                return False
            # The television state: the set switches itself into art mode and
            # the person watching loses their programme. Measured, not assumed.
            self.art_mode = "on"
            self.took_the_screen += 1
        if self.displays_nothing_selected:
            # Silence: the set took the request, emitted nothing, and the wall did
            # not move. No announcement, because the real one made none.
            return False
        if content_id in self.admits_not_showing:
            # The set answers "I took it and I am not showing it" — an
            # announcement, unlike the silence above, so observers hear it.
            self.announce(content_id, is_shown=False)
            return False
        self.displaying = content_id
        self.announce(content_id, is_shown=True)
        return True

    def announce(self, content_id: str, *, is_shown: bool) -> None:
        """Emit what the real set emits, to everyone listening.

        Public so a test can model the announcement this plane did **not** cause:
        somebody picking up the remote makes the set announce a selection nobody
        here asked for, and that is the case an observer exists to hear.
        """
        announcement = SelectionAnnouncement(content_id=content_id, is_shown=is_shown)
        self.announced.append(announcement)
        for observer in self.selection_observers:
            try:
                observer(announcement)
            except Exception:  # noqa: BLE001  # prawduct:allow prawduct/broad-except -- mirrors the real client; see below
                # **Isolated because the real client isolates**, and a fake that
                # did not would be stricter than the thing it stands in for — the
                # direction that hurts. `SamsungTv._tell_observers` catches per
                # observer, on the grounds that observers are strangers to each
                # other and this runs on the socket's reader task. A fake that let
                # one raise through would fail a test describing behaviour the
                # product actually has, and the fix would have been to weaken the
                # test.
                self.observer_failures += 1

    def at(self, seconds: float, **change: object) -> None:
        """Somebody changes the set `seconds` into its working time: `art_mode=` or `power=`.

        The change lands inside whichever verb is running when the set's time
        passes that point, which is how "art mode went off during the upload" is
        armed: `seconds_for["upload"] = 10`, then `at(5, art_mode="off")`. A name
        outside what a person can change fails here by name, rather than setting
        an attribute nothing reads.
        """
        unknown = set(change) - _CHANGEABLE
        if unknown:
            raise TypeError(f"FakeTv.at cannot change {sorted(unknown)}; a person changes only {sorted(_CHANGEABLE)}")
        self._changes.append((seconds, change))

    def _spend(self, verb: str) -> None:
        """Let the time `verb` takes pass, and apply whatever somebody changed inside it."""
        self.elapsed += self.seconds_for.get(verb, 0.0)
        due = sorted((change for change in self._changes if change[0] <= self.elapsed), key=lambda change: change[0])
        self._changes = [change for change in self._changes if change[0] > self.elapsed]
        for _, change in due:
            for name, value in change.items():
                setattr(self, name, value)

    def _dropping(self, exc: TvUnavailable) -> TvUnavailable:
        """Mark the connection gone, the way the real client does on any failure.

        **This is not decoration, and a fake without it hides a whole dead
        branch.** `SamsungTv._call` abandons and closes its client on every
        failure — it holds a websocket whose state after an error is not knowable
        — so the caller's *next* request raises "not connected" rather than
        reaching the set. A fake that kept answering after raising let the
        daemon's rebind path look reachable when against the real client it could
        never run: the listing it depends on would have raised first.
        """
        self._connected = False
        return exc

    async def showing_art(self) -> bool:
        self._check_usable()
        # Counted because this costs a request against the real set, and the poll
        # interval is one second: "the gate is affordable" is a claim about how
        # often it is asked, and nothing else here can express it.
        self.art_mode_reads += 1
        return self.art_mode == "on"

    async def power_state(self) -> str:
        # Never `TvUnavailable`: the real read rides REST beside the art channel,
        # and its failing neither drops the connection nor is an outage.
        self.power_reads += 1
        if self.power_unreadable or self.unavailable or not self._connected:
            raise PowerStateUnreadable("the set's device description did not answer")
        return self.power

    async def read_identity(self) -> str:
        # **Needs no connection**, as the real read opens its own REST client and
        # never the art channel; it fails where that one fails, on a set that
        # does not answer and on a description with no duid.
        self.identity_reads += 1
        if self.unavailable:
            raise IdentityUnreadable("the set's device description did not answer")
        if self.device_id is None:
            raise IdentityUnreadable("the set's device description carries no device.duid")
        return self.device_id

    def observe_selections(self, observer: SelectionObserver) -> None:
        # Idempotent, as the real client is: subscribing twice must not mean being
        # told twice, or a double that tolerated it would let a caller ship a
        # duplicate registration this suite could never see.
        if observer not in self.selection_observers:
            self.selection_observers.append(observer)

    def art_mode_announcement_pending(self) -> bool:
        announced, self.art_mode_announced = self.art_mode_announced, False
        return announced

    async def reported_art_mode(self) -> str | None:
        return self.art_mode

    async def _send_remove(self, content_ids: tuple[str, ...]) -> RemovalOutcome:
        self._check_usable()
        if self.removal_unconfirmable:
            raise TvRemovalUnconfirmed("the removal request was refused; what the set holds is unknown")
        requested = content_ids
        self.removed.append(requested)
        for content_id in requested:
            if content_id not in self.unremovable:
                self.holding.pop(content_id, None)
        return RemovalOutcome(requested=requested, surviving=tuple(c for c in requested if c in self.holding))

    async def _send_set_brightness(self, value: int) -> None:
        self._check_usable()
        self.brightness.append(value)

    def _check_usable(self) -> None:
        """What every call except `connect` must pass, and both halves matter.

        **A dropped connection refuses work until somebody reconnects.** The real
        client raises "not connected to the television" whenever it is holding no
        handle, and it lets go of that handle on *any* failure. A fake that went
        on answering after raising made a whole branch of the daemon look
        reachable that could never run against the shipped client — the rebind
        path, whose first act is to ask the set a question the real client would
        have refused.
        """
        self._check_reachable()
        if not self._connected:
            raise TvUnavailable("not connected to the television")

    def _check_reachable(self) -> None:
        if self.unavailable:
            # The real client drops its handle on any failed call, so the next
            # attempt reconnects rather than retrying on a websocket whose state
            # nobody can know. A fake that stayed "connected" through a failure
            # would let a daemon skip the reconnect and still pass.
            self._connected = False
            raise TvUnavailable("the television is asleep")

    # -- what a test asserts against ---------------------------------------

    @property
    def on_the_wall(self) -> Path | None:
        """The file the wall is *displaying*, or None if it is displaying none.

        Read from what the set displays rather than from the last id requested,
        so that a test asserting a picture reached the wall cannot be satisfied by
        a request the set ignored. Those were the same thing until a television
        was found accepting selections and displaying nothing.
        """
        return self.holding.get(self.displaying) if self.displaying else None
