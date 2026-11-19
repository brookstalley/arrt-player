"""The Frame, driven by the schedule: the timer is the feed's, and the television is whoever's using it.

The television's own slideshow can only be scoped to a whole category — no
content-id list, no album, no playlist — so it cannot be made to show a theme.
So this Player changes the picture itself, calling `select_image` when the
feed's schedule moves to another work, and switches the set's slideshow off once
so the two cannot fight.

The fixture feed (`conftest.Publisher`) puts its works in order, each up for
`interval_seconds` from the instant the test clock starts, so "the next slot" is
"the next work".

A curator's `show_now` and `next` arrive as a republished schedule whose
current slot names another work (`player-contract.md` § What happens to
`show_now` and `next`), so the tests at the end publish exactly that.
"""

import logging

import pytest
from fakes import FakeTv

from arrt_player.displays.frame import frame_wall
from arrt_player.manifest import Watcher
from arrt_player.state import DisplayState
from arrt_player.tv import WRITES, TvNotOurs
from arrt_player.wall import Wall


def _a_wall(settings, tv: FakeTv, state: DisplayState, clock) -> Wall:
    """A wall on the Frame as a restart builds one: a new loop over the same store and the same set."""
    return frame_wall(settings=settings, tv=tv, state=state, watcher=Watcher(settings.manifest_path), clock=clock.as_clock())


def _events(caplog, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "event", None) == event]


async def test_it_shows_the_slots_work_immediately(daemon: Wall, tv: FakeTv, publish):
    publish(["w1", "w2"])

    await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w1"


async def test_it_holds_a_work_until_its_slot_ends(daemon: Wall, tv: FakeTv, publish, clock):
    publish(["w1", "w2"], interval_seconds=180)
    await daemon.tick()

    clock.advance(179)
    await daemon.tick()
    assert len(tv.selected) == 1, "the wall moved before the slot was over"

    clock.advance(1)
    await daemon.tick()
    assert publish.work_of(tv.on_the_wall) == "w2"


async def test_the_schedule_comes_round_again(daemon: Wall, tv: FakeTv, publish, clock):
    publish(["w1", "w2"], interval_seconds=10)
    await daemon.tick()

    for _ in range(2):
        clock.advance(10)
        await daemon.tick()

    assert [publish.work_of(tv.holding[c]) for c in tv.selected] == ["w1", "w2", "w1"]


async def test_the_native_slideshow_is_disabled_once_and_survives_a_restart(
    settings, tv: FakeTv, state: DisplayState, clock, publish
):
    """Persisted rather than held in memory, because `Restart=always` makes
    restarts routine and the call is only correct to make once."""
    publish(["w1"])
    first = _a_wall(settings, tv, state, clock)
    await first.tick()
    await first.tick()
    assert tv.slideshow_disabled == 1

    await _a_wall(settings, tv, state, clock).tick()

    assert tv.slideshow_disabled == 1


async def test_a_work_whose_media_is_missing_is_passed_over_and_the_next_slot_shown(
    daemon: Wall, tv: FakeTv, publish, clock, caplog
):
    """Fatal-for-one-item. The wall going black is always worse than the wall
    being incomplete: it keeps the work it has through the missing one's slot."""
    publish(["w1", "w2", "w3"], interval_seconds=10)
    publish.withdraw("w2")
    await daemon.tick()

    clock.advance(10)
    with caplog.at_level(logging.WARNING):
        await daemon.tick()
    assert publish.work_of(tv.on_the_wall) == "w1", "the wall gave up its picture for a work it could not show"

    clock.advance(10)
    await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w3", "the missing work stopped the schedule instead of being passed over"
    assert len(_events(caplog, "schedule.media_missing")) == 1


async def test_a_feed_whose_every_master_is_missing_selects_nothing_and_says_so_once(daemon: Wall, tv: FakeTv, publish, caplog):
    """A pass looks at one work, the slot's, so a feed that can show nothing ends
    every pass at once rather than spinning."""
    publish(["w1", "w2"], media=False)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await daemon.tick()

    assert tv.selected == []
    assert len(_events(caplog, "schedule.media_missing")) == 1


async def test_a_feed_that_can_show_nothing_warns_once_a_work_not_once_a_second(daemon: Wall, tv: FakeTv, publish, clock, caplog):
    """journald rate-limits, and the lines it drops are the ERRORs that are the
    Player's only failure channel: a missing master is said once per work per
    feed, however long its slot lasts."""
    publish(["w1", "w2", "w3"], interval_seconds=60, media=False)

    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            await daemon.tick()
            clock.advance(1)
    assert len(_events(caplog, "schedule.media_missing")) == 1, "the missing work was said again on every poll"

    with caplog.at_level(logging.WARNING):
        clock.advance(60)
        await daemon.tick()

    assert len(_events(caplog, "schedule.media_missing")) == 2


async def test_media_that_arrives_is_shown_at_once_rather_than_at_the_next_slot(daemon: Wall, tv: FakeTv, publish):
    """So a wall with nothing to show recovers when the pull brings its master."""
    publish(["w1"], interval_seconds=180, media=False)
    await daemon.tick()
    assert tv.selected == []

    publish(["w1"], interval_seconds=180)
    await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w1"


async def test_a_restart_keeps_the_picture_and_then_carries_on_from_it(settings, tv: FakeTv, state: DisplayState, clock, publish):
    """A restart must not lose its place — and must not move the wall either.

    `Restart=always` makes restarts routine. Re-selecting what is already
    showing is idempotent and invisible, and the next slot is still the next work.
    """
    publish(["w1", "w2", "w3"], interval_seconds=10)
    first = _a_wall(settings, tv, state, clock)
    await first.tick()
    clock.advance(10)
    await first.tick()
    assert publish.work_of(tv.on_the_wall) == "w2"

    restarted = _a_wall(settings, tv, state, clock)
    await restarted.tick()
    assert publish.work_of(tv.on_the_wall) == "w2", "the restart moved the wall"

    clock.advance(10)
    await restarted.tick()
    assert publish.work_of(tv.on_the_wall) == "w3", "the restart lost its place in the schedule"


async def test_repeated_restarts_do_not_walk_the_wall_forward(settings, tv: FakeTv, state: DisplayState, clock, publish):
    """The crash-loop case stated on its own, because it is the one that is ugly
    in the room rather than merely wrong in the store."""
    publish(["w1", "w2", "w3"], interval_seconds=180)
    await _a_wall(settings, tv, state, clock).tick()

    for _ in range(10):
        await _a_wall(settings, tv, state, clock).tick()

    assert {publish.work_of(tv.holding[c]) for c in tv.selected} == {"w1"}


async def test_a_republish_mid_slot_does_not_hand_the_current_work_a_second_turn(daemon: Wall, tv: FakeTv, publish, clock):
    """The server republishes on every catalogue edit; the schedule's times are absolute, so the slot still ends when it did."""
    publish(["w1", "w2", "w3"], interval_seconds=100)
    await daemon.tick()
    assert publish.work_of(tv.on_the_wall) == "w1"

    clock.advance(50)
    publish(["w1", "w2", "w3"], interval_seconds=100)
    await daemon.tick()

    clock.advance(50)
    await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w2", "a catalogue edit gave the current work a second slot"


# -- a television that takes selections and displays none of them --------------
#
# Observed on a real set on 2026-08-07 with its panel dark: `select_image`
# returned, raised nothing and emitted none of the three art-mode events, while
# what the set displayed did not change across repeated attempts over twelve
# seconds. Every call a wall can make succeeded; the only thing that failed was
# the picture changing. These tests exist because the failure is invisible from
# the call: nothing above the seam can infer it, and the set's silence — no
# `image_selected` announcement — is the only thing that distinguishes it from a
# change that worked.


async def test_a_selection_the_set_does_not_display_is_not_reported_as_shown(
    daemon: Wall, tv: FakeTv, state: DisplayState, publish, caplog
):
    publish(["w1", "w2"])
    tv.displays_nothing_selected = True

    with caplog.at_level(logging.INFO):
        await daemon.tick()

    assert tv.selected, "the wall never asked the set to show anything"
    assert tv.on_the_wall is None, "the fake put a picture up that the set never displayed"
    assert not _events(caplog, "rotation.selected"), "the wall reported a change the television did not perform"
    assert (
        state.last_selected_work_id is None
    ), "a work never displayed was recorded as the one on the wall, so a restart would re-show it"


async def test_a_wall_that_displays_nothing_ends_the_pass_rather_than_trying_other_works(daemon: Wall, tv: FakeTv, publish):
    """A missing master means pass this work over; a dark wall means try no work.

    Every other work would be attempted against a set that displays none of
    them, each waiting out the whole confirmation window.
    """
    publish(["w1", "w2", "w3", "w4"])
    tv.displays_nothing_selected = True

    await daemon.tick()

    assert len(tv.selected) == 1, f"the pass tried {len(tv.selected)} works against a wall that displays none"


async def test_it_says_the_wall_is_not_changing_once_not_once_a_slot(daemon: Wall, tv: FakeTv, publish, clock, caplog):
    """A panel stays dark for hours. One line per slot is a hundred a night
    saying the one thing that has not changed, and journald drops what it
    rate-limits — which would be the ERRORs the Player's only failure channel
    carries."""
    publish(["w1", "w2"], interval_seconds=10)
    # Art mode stays *on*: a set that says it is showing art and then does not
    # change is the only route left to this branch, now that a set reporting art
    # mode off is never asked to select at all.
    tv.displays_nothing_selected = True

    with caplog.at_level(logging.INFO):
        for _ in range(6):
            await daemon.tick()
            clock.advance(10)

    reports = _events(caplog, "rotation.wall_unchanged")
    assert len(reports) == 1, f"the still wall was reported {len(reports)} times"
    assert reports[0].art_mode == "on", "the one line an operator reads does not say what the set claims about itself"


async def test_the_wall_coming_back_is_reported_and_the_schedule_resumes(daemon: Wall, tv: FakeTv, publish, clock, caplog):
    publish(["w1", "w2"], interval_seconds=10)
    tv.displays_nothing_selected = True
    await daemon.tick()

    with caplog.at_level(logging.INFO):
        tv.displays_nothing_selected = False
        clock.advance(10)
        await daemon.tick()

    assert tv.on_the_wall is not None, "the schedule did not resume when the set started displaying again"
    assert _events(
        caplog, "rotation.wall_recovered"
    ), "the wall came back and nothing said so, so the WARNING above it stands unresolved in the log"


async def test_a_wall_that_comes_back_shows_the_slots_work_now(daemon: Wall, tv: FakeTv, publish, clock):
    """Neither the work it could not show nor a catch-up through the slots it missed.

    The schedule is state, not a queue: the wall coming back shows what the
    schedule says now, in one selection. Four works and two dark passes, so the
    work now (w3) is neither the first nor the one deferred.
    """
    publish(["w1", "w2", "w3", "w4"], interval_seconds=10)
    tv.displays_nothing_selected = True

    for _ in range(2):
        await daemon.tick()
        clock.advance(10)

    tv.displays_nothing_selected = False
    attempts = len(tv.selected)
    await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w3", "an evening of a dark panel left the wall on the wrong work"
    assert len(tv.selected) == attempts + 1, "the wall walked the slots it missed"


async def test_a_set_that_says_it_took_the_image_and_is_not_showing_it_is_believed(daemon: Wall, tv: FakeTv, publish, caplog):
    """The set has two ways of not moving the wall, and both mean the same thing.

    It can stay silent, which is the dark panel, or it can announce the selection
    while saying `is_shown: "No"`. The second is the one a wall that merely
    counted announcements would get wrong, reporting a change from the set's own
    word that it had not performed one.
    """
    publish(["w1", "w2"])
    tv.admits_not_showing = {"MY-F0001"}

    with caplog.at_level(logging.INFO):
        await daemon.tick()

    assert tv.selected, "the wall never asked the set to show anything"
    assert not _events(caplog, "rotation.selected"), "the set said it was not showing the image and the wall reported it as shown"
    assert _events(caplog, "rotation.wall_unchanged")


# -- the television belongs to whoever is using it -----------------------------
#
# Measured on a real set on 2026-08-07, with the operator watching a programme: a
# due change sent `select_image`, the set **switched itself into art mode**, and
# the picture they were watching was gone. It is not a polite refusal like the
# dark state's, and somebody watching television is a daily event rather than an
# edge case. So nothing reaches the wall unless the set says it is showing art —
# and `get_artmode` is what says so, reading `off` for both a dark panel and a
# programme, and `on` only for art mode.


async def test_a_television_somebody_is_watching_is_left_alone(daemon: Wall, tv: FakeTv, publish, caplog):
    publish(["w1", "w2"])
    tv.art_mode = "off"

    with caplog.at_level(logging.INFO):
        await daemon.tick()

    assert tv.selected == [], "the wall took the screen off whoever was watching"
    assert _events(caplog, "rotation.wall_not_ours")


async def test_an_evening_of_television_ends_on_the_slots_work_in_one_selection(daemon: Wall, tv: FakeTv, publish, clock):
    """Nothing is walked against a wall nobody could see.

    Five works and four passes of television, so the work the schedule names
    when the set comes back (w5) is not the first one.
    """
    publish(["w1", "w2", "w3", "w4", "w5"], interval_seconds=10)
    tv.art_mode = "off"

    for _ in range(4):
        await daemon.tick()
        clock.advance(10)

    tv.art_mode = "on"
    await daemon.tick()

    assert [publish.work_of(tv.holding[c]) for c in tv.selected] == ["w5"], "an evening of television walked the schedule"


async def test_the_wall_comes_back_the_moment_the_set_announces_art_mode(daemon: Wall, tv: FakeTv, publish, clock, caplog):
    """Recovery is by the set's own announcement, not by waiting out the backoff.

    Without this, switching off a programme would leave the wall blank for the
    remainder of a wait that has doubled its way up to five minutes — and unlike
    a panel left dark overnight, this transition happens every time somebody
    finishes watching something.
    """
    publish(["w1", "w2"], interval_seconds=180)
    tv.art_mode = "off"
    for _ in range(6):
        await daemon.tick()
        clock.advance(5)
    assert tv.selected == []

    # The set says it has changed, and the clock barely moves: the point is that
    # none of the remaining wait — the backoff — has to elapse before the picture
    # comes back.
    tv.art_mode = "on"
    tv.art_mode_announced = True
    with caplog.at_level(logging.INFO):
        await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w1", "the wall waited out a backoff the set had already ended"
    assert _events(caplog, "rotation.wall_returned")


async def test_it_says_the_wall_is_not_ours_once_not_once_a_slot(daemon: Wall, tv: FakeTv, publish, clock, caplog):
    """Somebody watches television for hours. A line per attempt would bury the
    ERRORs that are the Player's only failure channel."""
    publish(["w1", "w2"], interval_seconds=10)
    tv.art_mode = "off"

    with caplog.at_level(logging.INFO):
        for _ in range(6):
            await daemon.tick()
            clock.advance(10)

    reports = _events(caplog, "rotation.wall_not_ours")
    assert len(reports) == 1, f"a television in use was reported {len(reports)} times"


async def test_asking_whether_the_wall_is_ours_is_not_done_once_a_second(daemon: Wall, tv: FakeTv, publish, clock):
    """The check costs a request, and the poll interval is one second.

    Backing off is what makes a gate on every change affordable; without it an
    evening of television is tens of thousands of round trips at the set.
    """
    publish(["w1", "w2"], interval_seconds=10)
    tv.art_mode = "off"

    for _ in range(30):
        await daemon.tick()
        clock.advance(1)

    assert tv.art_mode_reads < 10, f"the set was asked {tv.art_mode_reads} times in thirty seconds"


async def test_the_backoff_starts_over_once_the_set_behaves_again(daemon: Wall, tv: FakeTv, publish, clock):
    """Otherwise unrelated dark spells compound.

    The wait doubles while the set ignores selections, which is right for one
    evening with the panel off. Carrying the grown wait past a recovery would
    mean the third brief spell in a week backing off five minutes — a wall that
    takes longer and longer to come back for no reason anyone could observe.

    One-second slots over thirty works, so no slot names the work already up,
    which the schedule rightly leaves alone without asking the set.
    """
    publish([f"w{n}" for n in range(30)], interval_seconds=1)
    tv.displays_nothing_selected = True

    await daemon.tick()  # attempt, then wait 5
    clock.advance(5)
    await daemon.tick()  # attempt, then wait 10

    tv.displays_nothing_selected = False
    clock.advance(10)
    await daemon.tick()
    assert tv.on_the_wall is not None, "the set started behaving and the wall did not come back"

    tv.displays_nothing_selected = True
    clock.advance(1)
    await daemon.tick()  # attempt, and the wait must be the floor again
    attempts = len(tv.selected)

    clock.advance(5)
    await daemon.tick()

    assert len(tv.selected) > attempts, "the wait carried its grown value across a recovery"


async def test_a_new_slot_does_not_re_ask_a_wall_the_backoff_is_holding_off(daemon: Wall, tv: FakeTv, publish, clock):
    """The other half of the wait, and the half that bites in the deployment.

    The two clocks are independent: the schedule moves on at its slots, the
    wall's wait on a ladder that doubles to five minutes. Once the ladder is
    longer than a slot, every slot in between would ask a television already
    known to be ignoring selections, which is the flood the ladder exists to stop.
    """
    publish(["w1", "w2"], interval_seconds=1)
    tv.displays_nothing_selected = True

    await daemon.tick()  # attempt, then wait 5
    clock.advance(5)
    await daemon.tick()  # attempt, then wait 10
    attempts = len(tv.selected)
    assert attempts == 2, "the ladder did not let the second attempt through"

    # A new slot on every one of these; the wall's wait is not up until t=15.
    for _ in range(9):
        clock.advance(1)
        await daemon.tick()

    assert len(tv.selected) == attempts, "a new slot asked a wall the backoff was holding off"


# -- a republished schedule: how `show_now` and `next` arrive --------------------
#
# A curator's `show_now` or `next` reaches the wall as a new feed whose slot now
# names another work. It is state, not a command: nothing is consumed, so a
# change the wall could not make is still owed on the next pass, and is made
# once the wall can — never lost, and never asked for once a second.


async def test_a_republished_schedule_reaches_the_wall_at_once(daemon: Wall, tv: FakeTv, publish):
    publish(["w1", "w2", "w3"])
    await daemon.tick()

    publish(["w3", "w1", "w2"])
    await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w3"


async def test_a_republished_schedule_is_not_lost_while_the_television_is_asleep(
    daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
):
    """An outage delays the change, never eats it."""
    publish(["w1", "w2", "w3"], interval_seconds=3600)
    await daemon.tick()

    tv.unavailable = True
    publish(["w3", "w1", "w2"], interval_seconds=3600)
    await daemon.tick()
    assert state.last_selected_work_id == "w1"

    tv.unavailable = False
    clock.advance(301)
    await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w3"
    assert state.last_selected_work_id == "w3"


async def test_a_change_is_not_lost_when_the_set_drops_mid_selection(daemon: Wall, tv: FakeTv, publish, state: DisplayState):
    """The set answers at the top of the pass and is gone by the selection.

    The wall's memory is written only once the set has shown the work, so the
    next pass, with the set back, still owes it.
    """
    publish(["w1", "w2", "w3"])
    await daemon.tick()
    assert publish.work_of(tv.on_the_wall) == "w1"
    await daemon.tick()  # the pass that uploads w2, one upload per pass
    doomed = state.binding_for("w2").tv_content_id

    tv.refuse_selection_of.add(doomed)
    publish(["w2", "w3", "w1"])
    await daemon.tick()
    assert state.last_selected_work_id == "w1", "a change the set never performed was recorded as made"

    tv.refuse_selection_of.clear()
    for _ in range(3):
        await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w2", "the change was lost to a momentary drop"


async def test_a_republished_schedule_is_not_put_on_a_television_somebody_is_watching(
    daemon: Wall, tv: FakeTv, publish, state: DisplayState
):
    """A change is still a selection, and a selection takes the screen. Held, and delivered when the set returns to art mode."""
    publish(["w1", "w2", "w3"])
    await daemon.tick()
    tv.art_mode = "off"
    publish(["w3", "w1", "w2"])
    await daemon.tick()
    assert [publish.work_of(tv.holding[c]) for c in tv.selected] == ["w1"], "the wall took the screen off whoever was watching"

    tv.art_mode = "on"
    tv.art_mode_announced = True
    await daemon.tick()

    assert publish.work_of(tv.on_the_wall) == "w3", "the change was lost while the television was in use"


async def test_a_held_back_change_does_not_ask_the_set_once_a_second(daemon: Wall, tv: FakeTv, publish, clock):
    """A change still owed is still owed on the next poll, so the wait has to be read before the ask.

    Asking whether the wall is ours costs a real request — putting it in front
    of the backoff spends thousands of them across one programme.
    """
    publish(["w1", "w2", "w3"])
    await daemon.tick()

    tv.art_mode = "off"
    publish(["w3", "w1", "w2"])
    before = tv.art_mode_reads
    for _ in range(30):
        await daemon.tick()
        clock.advance(1)

    asked = tv.art_mode_reads - before
    assert asked < 10, f"a change held back asked the set {asked} times in thirty seconds"


async def test_a_change_against_a_dark_wall_is_not_re_attempted_every_poll(daemon: Wall, tv: FakeTv, publish, clock):
    """Each attempt is a selection the television is going to ignore, followed by
    the whole confirmation window spent waiting for an announcement that will
    never come — all night, at one second apart, without the ladder."""
    publish(["w1", "w2", "w3"])
    await daemon.tick()

    tv.displays_nothing_selected = True
    publish(["w3", "w1", "w2"])
    before = len(tv.selected)
    for _ in range(10):
        await daemon.tick()
        clock.advance(1)

    # Ten polls, and the retry floor is five seconds: two attempts, not ten. The
    # assertion is against the *ladder* rather than a number of seconds, because
    # what is being fixed is the cadence being the poll's.
    attempts = len(tv.selected) - before
    assert attempts == 2, f"the change was attempted {attempts} times in ten one-second polls"


# -- the rule holds up to the moment of the write ---------------------------------
#
# Asking the set once and then working for a minute is not asking it: an upload
# costs the set the better part of ten seconds, and the timeout allows sixty, so
# somebody can pick up the remote between the question and the selection. The
# television seam therefore reads the set's state immediately before every verb
# that changes what it shows or stores (`tv.client.WRITES`), and these drive that
# through the wall, with the double changing state *during* a verb.


async def test_art_mode_going_off_during_the_upload_stops_the_selection(daemon: Wall, tv: FakeTv, publish, state: DisplayState):
    """The reviewer's probe, made permanent: a fresh wall's first work is uploaded,
    somebody switches to a programme five seconds into that upload, and the
    selection that would follow it is never sent."""
    publish(["w1", "w2"])
    tv.seconds_for["upload"] = 10
    tv.at(5, art_mode="off")

    await daemon.tick()

    assert tv.selected == [], "the selection after the upload took the screen off whoever was watching"
    assert tv.took_the_screen == 0
    assert tv.art_mode == "off"
    assert state.last_selected_work_id is None, "a selection that was never made was recorded as made"


async def test_art_mode_going_off_during_a_re_upload_stops_the_second_selection(
    daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
):
    """The rebind path: the set refuses a selection because it has forgotten the
    image, the work is uploaded again, and somebody switches to a programme
    during that upload. The second `select_image` is never sent."""
    publish(["w1", "w2"], interval_seconds=10)
    await daemon.tick()  # w1 up, and w2 uploaded behind it
    forgotten = state.binding_for("w2").tv_content_id
    del tv.holding[forgotten]
    selected_before = list(tv.selected)

    tv.seconds_for["upload"] = 10
    tv.at(tv.elapsed + 5, art_mode="off")
    clock.advance(10)
    await daemon.tick()

    assert tv.selected == selected_before, "the selection after the re-upload took the screen off whoever was watching"
    assert tv.took_the_screen == 0
    assert tv.art_mode == "off"
    assert state.last_selected_work_id == "w1"


async def test_a_set_somebody_is_watching_is_sent_no_write_at_all(daemon: Wall, tv: FakeTv, publish, settings):
    """Upload, removal, brightness and the slideshow switch were never tried with a
    programme on (`samsung-tv-state-findings.md` § What works in each state), so
    none is sent while the panel is lit and not showing art. Every one of them is
    owed here: a fresh wall with three works to upload, a picture on the set
    nothing accounts for, a brightness never written and a slideshow never
    switched off."""
    publish(["w1", "w2", "w3"])
    tv.holding["MY-LEGACY-1"] = settings.wall_dir / "legacy.jpg"
    tv.art_mode = "off"
    tv.power = "on"

    for _ in range(3):
        await daemon.tick()

    assert tv.uploaded == []
    assert tv.removed == []
    assert tv.brightness == []
    assert tv.slideshow_disabled == 0
    assert tv.selected == []


async def test_a_dark_set_is_still_sent_the_writes_it_was_measured_taking(daemon: Wall, tv: FakeTv, publish, settings):
    """The other half of the same rule: switched off at the remote (`PowerState`
    standby), the set serves uploads, removals, brightness and the slideshow
    switch, all measured. Holding them there would leave a wall that spends its
    nights dark with nothing uploaded by morning."""
    publish(["w1", "w2", "w3"])
    tv.holding["MY-LEGACY-1"] = settings.wall_dir / "legacy.jpg"
    tv.art_mode = "off"
    tv.power = "standby"

    for _ in range(3):
        await daemon.tick()

    assert len(tv.uploaded) == 3
    assert tv.removed == [("MY-LEGACY-1",)]
    assert len(tv.brightness) == 1
    assert tv.slideshow_disabled == 1
    assert tv.selected == [], "a dark set was selected on, which it ignores"


async def test_a_set_whose_power_cannot_be_read_is_sent_no_write_either(daemon: Wall, tv: FakeTv, publish, settings):
    """Unreadable `PowerState` cannot tell a programme from a dark panel, so it is held as the programme is."""
    publish(["w1", "w2", "w3"])
    tv.holding["MY-LEGACY-1"] = settings.wall_dir / "legacy.jpg"
    tv.art_mode = "off"
    tv.power_unreadable = True

    for _ in range(3):
        await daemon.tick()

    assert (tv.uploaded, tv.removed, tv.brightness, tv.slideshow_disabled) == ([], [], [], 0)


async def test_held_writes_are_made_once_the_set_is_showing_art_again(
    daemon: Wall, tv: FakeTv, publish, settings, state: DisplayState, clock
):
    """Held, never dropped: an evening of television costs the uploads their
    evening, and the set returning to art mode gets every one of them, with none
    of the works recorded as a failed upload in between."""
    publish(["w1", "w2", "w3"], interval_seconds=3600)
    tv.holding["MY-LEGACY-1"] = settings.wall_dir / "legacy.jpg"
    tv.art_mode = "off"
    for _ in range(3):
        await daemon.tick()
    assert all(state.binding_for(work) is None for work in ("w1", "w2", "w3")), "a held upload was recorded as attempted"

    tv.art_mode = "on"
    tv.art_mode_announced = True
    await daemon.tick()

    assert tv.slideshow_disabled == 1
    assert tv.removed == [("MY-LEGACY-1",)]
    assert publish.work_of(tv.on_the_wall) == "w1"
    assert len(tv.uploaded) == 2, "the theme did not resume filling in behind the first picture"

    clock.advance(settings.brightness_interval_seconds)
    await daemon.tick()
    assert len(tv.brightness) == 1, "the held brightness change was not retried at the next interval"


async def test_an_upload_held_for_a_programme_is_not_recorded_as_failed(
    daemon: Wall, tv: FakeTv, publish, state: DisplayState, caplog
):
    """The upload is the only write owed when the programme starts, so it is the
    one the seam holds. Nothing was sent, so nothing failed: a `failed` row would
    hold the work back for the upload retry wait after the set came back."""
    publish(["w1", "w2", "w3"], interval_seconds=3600)
    await daemon.tick()  # w1 up, w2 uploaded behind it; the slideshow, brightness and reconciliation done
    tv.art_mode = "off"

    with caplog.at_level(logging.INFO):
        await daemon.tick()

    assert len(tv.uploaded) == 2, "w3 was uploaded to a set showing a programme"
    assert state.binding_for("w3") is None, "a held upload was recorded as attempted"
    assert not _events(caplog, "binding.upload_failed")
    assert _events(caplog, "tv.writes_held")

    tv.art_mode = "on"
    tv.art_mode_announced = True
    await daemon.tick()

    assert len(tv.uploaded) == 3, "the held upload was not made once the set was showing art again"


async def test_held_writes_do_not_ask_the_set_once_a_second(daemon: Wall, tv: FakeTv, publish, clock):
    """Each attempt at a held write is two reads of the set, and a pending upload
    is due on every pass. Thirty one-second passes of a programme with three
    uploads owed must not be sixty requests."""
    publish(["w1", "w2", "w3"], interval_seconds=3600)
    tv.art_mode = "off"

    for _ in range(30):
        await daemon.tick()
        clock.advance(1)

    assert tv.power_reads < 10, f"held writes read PowerState {tv.power_reads} times in thirty passes"
    assert tv.art_mode_reads < 10, f"held writes read art mode {tv.art_mode_reads} times in thirty passes"


# -- the double, as the set was measured --------------------------------------


async def test_the_double_takes_the_screen_when_a_selection_reaches_a_set_showing_a_programme(tv: FakeTv, tmp_path):
    """Below the gate, where nothing in the product may reach: what the set does
    with a selection sent in the television state (measured 2026-08-07). Without
    it, a test asserting "nothing was selected" could not see what selecting
    would have cost."""
    await tv.connect()
    tv.holding["MY-F0001"] = tmp_path / "a.jpg"
    tv.art_mode = "off"
    tv.power = "on"

    assert await tv._send_show("MY-F0001") is True

    assert tv.art_mode == "on"
    assert tv.took_the_screen == 1
    assert tv.on_the_wall == tmp_path / "a.jpg"


async def test_the_double_ignores_a_selection_sent_to_a_dark_set(tv: FakeTv, tmp_path):
    """Measured too: accepted, no announcement, and the wall does not change."""
    await tv.connect()
    tv.holding["MY-F0001"] = tmp_path / "a.jpg"
    tv.art_mode = "off"
    tv.power = "standby"

    assert await tv._send_show("MY-F0001") is False

    assert (tv.art_mode, tv.took_the_screen, tv.announced) == ("off", 0, [])
    assert tv.on_the_wall is None


async def test_the_double_changes_state_inside_a_verb_that_takes_time(tv: FakeTv, tmp_path):
    await tv.connect()
    tv.seconds_for["upload"] = 10
    tv.at(5, art_mode="off")
    tv.at(15, power="standby")

    await tv.upload(tmp_path / "a.jpg")

    assert (tv.elapsed, tv.art_mode, tv.power) == (10, "off", "on"), "a change was applied outside the time it was armed for"


def test_the_double_refuses_to_arm_a_change_nobody_could_make(tv: FakeTv):
    with pytest.raises(TypeError, match="holding"):
        tv.at(1, holding={})


@pytest.mark.parametrize("verb", sorted(WRITES))
async def test_every_write_is_held_from_a_set_showing_a_programme(tv: FakeTv, tmp_path, verb: str):
    """Ranged over the seam's own declaration of what writes, so a verb added there is covered here by name."""
    await tv.connect()
    tv.holding["MY-F0001"] = tmp_path / "a.jpg"
    tv.art_mode = "off"
    tv.power = "on"
    before = _what_the_set_holds_and_shows(tv)

    with pytest.raises(TvNotOurs) as held:
        await WRITE_CALLS[verb](tv, tmp_path)

    assert held.value.verb == verb
    assert _what_the_set_holds_and_shows(tv) == before, f"{verb} changed the set while it was held"


def test_every_write_the_seam_declares_has_a_call_here():
    assert set(WRITE_CALLS) == set(WRITES)


#: One call of each write, by the name `tv.client.WRITES` gives it.
WRITE_CALLS = {
    "show": lambda tv, tmp: tv.show("MY-F0001"),
    "upload": lambda tv, tmp: tv.upload(tmp / "b.jpg"),
    "remove": lambda tv, tmp: tv.remove(["MY-F0001"]),
    "set_brightness": lambda tv, tmp: tv.set_brightness(3),
    "disable_native_slideshow": lambda tv, tmp: tv.disable_native_slideshow(),
}


def _what_the_set_holds_and_shows(tv: FakeTv) -> tuple:
    return (
        dict(tv.holding),
        list(tv.selected),
        list(tv.uploaded),
        list(tv.removed),
        list(tv.brightness),
        tv.slideshow_disabled,
        tv.displaying,
        tv.art_mode,
        tv.took_the_screen,
    )
