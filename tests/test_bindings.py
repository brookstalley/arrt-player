"""What this television holds, what this device believes, and the gap between them.

Two defects shape everything here. The 2024 loader recorded a successful upload
with no content id, so a failure read as a success; and the television's single
user-upload category means anything this device cannot account for is an image
nobody will ever remove if this process does not.
"""

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fakes import FakeTv
from PIL import Image

from arrt_player.displays.frame import frame_wall
from arrt_player.manifest import Watcher
from arrt_player.state import DisplayState, UploadStatus
from arrt_player.wall import Wall


class TestTheStoreRefusesTheOldDefect:
    def test_a_row_cannot_claim_an_upload_without_naming_its_id(self, state: DisplayState):
        """The 2024 defect made unwritable rather than merely avoided.

        That version caught every exception, logged, and still reported success —
        recording no content id while its retry loop set `success = True`. Here
        the write itself is refused.
        """
        with pytest.raises(sqlite3.IntegrityError):
            state._connection.execute(
                "INSERT INTO tv_binding (id, artwork_id, tv_content_id, uploaded_at, upload_status) "
                "VALUES ('1', 'w1', NULL, '2026-01-01T00:00:00+00:00', 'uploaded')"
            )

    def test_a_failed_upload_is_a_different_state_from_never_having_tried(self, state: DisplayState):
        assert state.binding_for("w1") is None

        failed = state.record_upload_failure("w1")

        assert failed.upload_status is UploadStatus.FAILED
        assert failed.is_on_the_television is False
        assert state.binding_for("w1") is not None, "a failed upload left no trace to distinguish it from an untried one"

    def test_a_store_from_before_the_fingerprint_column_is_widened_not_rebuilt(self, settings, clock):
        """The migration path, exercised rather than assumed.

        A store written by the version before `render_fingerprint` existed must
        open, keep its rows, and gain the column. It must also survive being
        reopened — but see the comment below for what that second open does and
        does not prove, since the two are easy to conflate and only one of them is
        what this test buys.
        """
        path = settings.state_path
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE tv_binding (
                id TEXT PRIMARY KEY, artwork_id TEXT NOT NULL UNIQUE, tv_content_id TEXT,
                tv_thumb_md5 TEXT, uploaded_at TEXT NOT NULL, upload_status TEXT NOT NULL
            );
            CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO tv_binding VALUES ('1', 'w1', 'MY-OLD', NULL, '2026-01-01T00:00:00+00:00', 'uploaded');
            PRAGMA user_version = 1;
            """)
        old.commit()
        old.close()

        # Opened twice because a store must survive being reopened, not because the
        # second pass reaches the `ALTER`: the first stamps `user_version`, so the
        # second returns at the version guard. What covers the ALTER's idempotency
        # is the column-list check, which every fresh store in this suite exercises.
        for _ in range(2):
            with DisplayState(path, now=lambda: clock.as_clock().now()) as store:
                binding = store.binding_for("w1")
                assert binding.tv_content_id == "MY-OLD", "an existing row was lost by the widening"
                assert binding.render_fingerprint is None, "a row from before the column claimed a fingerprint"

    def test_an_upload_replaces_the_failure_before_it(self, state: DisplayState):
        state.record_upload_failure("w1")

        state.record_upload("w1", "MY-F0001")

        binding = state.binding_for("w1")
        assert binding.upload_status is UploadStatus.UPLOADED
        assert binding.tv_content_id == "MY-F0001"
        assert len(state.bindings()) == 1, "the retry left a second row for the same work"


class TestUploading:
    async def test_a_work_is_uploaded_before_it_is_shown(self, daemon: Wall, tv: FakeTv, publish, state: DisplayState):
        publish(["w1"])

        await daemon.tick()

        binding = state.binding_for("w1")
        assert binding.is_on_the_television
        assert publish.work_of(tv.holding[binding.tv_content_id]) == "w1"

    async def test_an_upload_that_fails_is_recorded_and_the_work_skipped(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, caplog
    ):
        publish(["w1", "w2"])
        tv.refuse_uploads = True

        with caplog.at_level(logging.WARNING):
            await daemon.tick()

        assert tv.selected == [], "a work was shown that is not on the television"
        assert state.binding_for("w1").upload_status is UploadStatus.FAILED
        assert "binding.upload_failed" in {r.__dict__.get("event") for r in caplog.records}

    async def test_the_theme_fills_in_behind_the_first_picture(self, daemon: Wall, tv: FakeTv, publish, state):
        """Uploads are carried one per pass rather than done in a batch.

        A fresh install with forty works and ten seconds an upload would leave the
        wall on yesterday's picture — and a curator pressing "next" with nothing
        happening — for five minutes, against a poll interval that is one second
        precisely because that wait is the one the product may not have.
        """
        publish(["w1", "w2", "w3", "w4"], interval_seconds=3600)

        await daemon.tick()
        assert len(tv.holding) == 2, "the first pass should show one work and carry one more"

        for _ in range(5):
            await daemon.tick()

        assert len(tv.holding) == 4
        assert len(tv.selected) == 1, "filling the theme in moved the wall"

    async def test_a_work_that_keeps_failing_is_not_retried_every_second(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock, settings, caplog
    ):
        """A reachable set refusing one image is not the same fault as an asleep one.

        Only the second backs off with the connection. Without a wait of its own,
        the first costs a round trip, a WARNING and a rewritten row **every poll**
        — an unbounded small-write source on the SD card the observability
        strategy sizes writes for, and a journal whose rate limiter then drops the
        lines that matter.
        """
        publish(["w1"], interval_seconds=3600)
        tv.refuse_uploads = True

        with caplog.at_level(logging.WARNING):
            for _ in range(20):
                await daemon.tick()
                clock.advance(1)

        failures = [r for r in caplog.records if r.__dict__.get("event") == "binding.upload_failed"]
        assert len(failures) == 1, "a failing upload was retried on every pass"
        assert state.binding_for("w1").upload_status is UploadStatus.FAILED

    async def test_a_work_that_failed_is_tried_again_once_the_wait_is_up(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock, settings
    ):
        """The wait is a wait, not a giving up: whatever was wrong may be fixed."""
        publish(["w1"], interval_seconds=3600)
        tv.refuse_uploads = True
        await daemon.tick()
        assert state.binding_for("w1").upload_status is UploadStatus.FAILED

        tv.refuse_uploads = False
        clock.advance(settings.upload_retry_seconds + 1)
        await daemon.tick()

        assert state.binding_for("w1").is_on_the_television

    async def test_a_re_rendered_work_is_sent_again(
        self, daemon: Wall, tv: FakeTv, publish, wall_dir, state: DisplayState, clock
    ):
        """**The wall showing a picture that is no longer the file it was given.**

        A binding names a work, so a picture rewritten under a name the binding
        already holds would leave it reading `uploaded` while the television went
        on showing the old picture indefinitely — and nothing in the product would
        ever say so, because every record agrees. A composed picture's name is its
        composition key, so this is a file rewritten in place: the fingerprint is
        what catches it.
        """
        publish(["w1"], interval_seconds=10)
        await daemon.tick()
        first = state.binding_for("w1").tv_content_id

        Image.new("RGB", (8, 8), (255, 0, 0)).save(publish.picture("w1"), format="JPEG")
        different = publish.picture("w1").read_bytes()
        clock.advance(10)
        await daemon.tick()

        rebound = state.binding_for("w1")
        assert rebound.tv_content_id != first, "the television was never told the render changed"
        assert tv.holding[rebound.tv_content_id].read_bytes() == different

    async def test_a_work_whose_render_is_untouched_is_not_sent_again(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
    ):
        """The other half, and the one that costs money if wrong: a fingerprint
        that changed spuriously would re-upload the whole theme every pass."""
        publish(["w1"], interval_seconds=10)
        await daemon.tick()
        first = state.binding_for("w1").tv_content_id

        for _ in range(5):
            clock.advance(10)
            await daemon.tick()

        assert state.binding_for("w1").tv_content_id == first
        assert len(tv.holding) == 1

    async def test_a_picture_composed_again_under_its_name_is_not_uploaded_again(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
    ):
        """A work leaving the feed loses its composed file, and gets the same pixels in a new file when it returns.

        The file's stamp moves and its bytes do not, so the set's copy is still
        the right one: re-sending it is the upload of several megabytes that a
        theme hung for one evening would cost every other work the next morning.
        """
        publish(["w1", "w2"], interval_seconds=10)
        await daemon.tick()
        first = state.binding_for("w2").tv_content_id
        picture = publish.picture("w2")
        same = picture.read_bytes()
        picture.unlink()
        picture.write_bytes(same)

        for _ in range(3):
            clock.advance(10)
            await daemon.tick()

        assert state.binding_for("w2").tv_content_id == first, "a picture composed again with the same pixels was re-sent"
        assert len(tv.uploaded) == 2

    async def test_a_row_an_earlier_player_recorded_by_the_files_stamp_uploads_nothing_after_the_upgrade(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
    ):
        """A Player before schema 3 recorded the file's modification time and size; while the file keeps them, it is current."""
        publish(["w1"], interval_seconds=10)
        await daemon.tick()
        first = state.binding_for("w1").tv_content_id
        stat = publish.picture("w1").stat()
        state._connection.execute(
            "UPDATE tv_binding SET render_fingerprint = ? WHERE artwork_id = 'w1'", (f"{stat.st_mtime_ns}:{stat.st_size}",)
        )
        state._connection.commit()

        for _ in range(3):
            clock.advance(10)
            await daemon.tick()
        assert state.binding_for("w1").tv_content_id == first, "the upgrade re-sent a picture whose file had not changed"

        Image.new("RGB", (8, 8), (255, 0, 0)).save(publish.picture("w1"), format="JPEG")
        clock.advance(10)
        await daemon.tick()
        assert state.binding_for("w1").tv_content_id != first, "a file rewritten under an earlier Player's row was not re-sent"

    def test_composing_a_work_again_gives_the_same_bytes(self, tmp_path: Path, settings):
        """What the fingerprint above relies on: a picture removed and composed again under its key is the same file."""
        from conftest import a_master

        from arrt_player.compose import DEFAULT_MAT_MODE, compose

        master = a_master(tmp_path / "master.jpg", size=(640, 480))
        work = {"master_sha256": "ab" * 32, "mat_color": "#336699", "mode": DEFAULT_MAT_MODE, "geometry": settings.geometry}
        first = compose(master, **work, directory=tmp_path)
        before = first.read_bytes()
        first.unlink()
        again = compose(master, **work, directory=tmp_path)

        assert again == first
        assert again.read_bytes() == before, "composing the same work again gave different bytes"

    async def test_a_work_already_on_the_television_is_not_uploaded_again(self, daemon: Wall, tv: FakeTv, publish, clock):
        publish(["w1", "w2"], interval_seconds=10)
        await daemon.tick()
        clock.advance(10)
        await daemon.tick()
        uploads = len(tv.holding)

        for _ in range(3):
            clock.advance(10)
            await daemon.tick()

        assert len(tv.holding) == uploads


class TestOrphans:
    async def test_an_upload_the_binding_table_cannot_account_for_is_removed(self, daemon: Wall, tv: FakeTv, publish, caplog):
        """A fresh install clears the set, and that is the correct behaviour.

        Images uploaded by a previous generation are not assets to adopt: nothing
        joins them to a work, so adopting one would bind a work to a picture that
        is not the render its manifest entry names.
        """
        tv.holding["MY-LEGACY-1"] = Path("/gone/legacy-1.jpg")
        tv.holding["MY-LEGACY-2"] = Path("/gone/legacy-2.jpg")
        publish(["w1"])

        with caplog.at_level(logging.INFO):
            await daemon.tick()

        assert "MY-LEGACY-1" not in tv.holding
        assert "MY-LEGACY-2" not in tv.holding
        assert tv.removed == [("MY-LEGACY-1", "MY-LEGACY-2")]

    async def test_a_work_the_manifest_still_names_is_never_removed(self, daemon: Wall, tv: FakeTv, publish, clock):
        publish(["w1", "w2"], interval_seconds=10)
        for _ in range(4):
            await daemon.tick()
            clock.advance(10)
        held_before = set(tv.holding)

        publish(["w1", "w2"], interval_seconds=10)  # a `sync` rewrite
        await daemon.tick()

        assert set(tv.holding) == held_before
        assert tv.removed == []

    async def test_a_binding_the_set_no_longer_lists_is_re_uploaded(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, caplog
    ):
        """Somebody removed it from the phone app. Selecting the stale id would
        fail against the set, so the row is marked orphaned and re-uploaded."""
        publish(["w1"])
        await daemon.tick()
        vanished = state.binding_for("w1").tv_content_id
        del tv.holding[vanished]

        publish(["w1"])
        with caplog.at_level(logging.WARNING):
            await daemon.tick()
            await daemon.tick()

        assert "binding.orphaned" in {r.__dict__.get("event") for r in caplog.records}
        rebound = state.binding_for("w1")
        assert rebound.is_on_the_television
        assert rebound.tv_content_id != vanished
        assert rebound.tv_content_id in tv.holding

    async def test_a_binding_the_set_refuses_mid_schedule_is_rebound_rather_than_retried_forever(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock, caplog
    ):
        """The wall-freezing case, and the reason a refusal is not read as an outage.

        Somebody removes one image from the phone app. The manifest does not
        change, so nothing re-checks the binding — and a daemon that treated the
        refusal as "the television is asleep" would back off and retry the same
        doomed id forever, with the wall stuck on the previous picture.
        """
        publish(["w1", "w2"], interval_seconds=10)
        await daemon.tick()
        await daemon.tick()
        stale = state.binding_for("w2").tv_content_id
        del tv.holding[stale]  # removed from the phone app; no manifest rewrite

        clock.advance(10)
        with caplog.at_level(logging.WARNING):
            await daemon.tick()

        assert publish.work_of(tv.on_the_wall) == "w2", "the wall froze on a binding the set had forgotten"
        assert state.binding_for("w2").tv_content_id != stale
        assert "binding.orphaned" in {r.__dict__.get("event") for r in caplog.records}

    async def test_a_refusal_the_set_cannot_explain_is_still_an_outage(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock, settings
    ):
        """The other side of that judgement, so the fix cannot swallow a real fault.

        If the set still lists the id it just refused, the binding is not the
        problem and inventing a re-upload would paper over whatever is.
        """
        publish(["w1", "w2"], interval_seconds=10)
        await daemon.tick()
        await daemon.tick()  # the pass that uploads w2, one upload per pass
        content_id = state.binding_for("w2").tv_content_id

        clock.advance(10)
        tv.refuse_selection_of.add(content_id)  # still listed, still refused
        wait = await daemon.tick()

        assert wait == settings.tv_retry_min_seconds, "a refusal the set could not explain was not treated as an outage"
        assert state.binding_for("w2").tv_content_id == content_id, "a live binding was thrown away"

    async def test_an_unconfirmable_removal_is_reported_as_unknown_and_retried(self, daemon: Wall, tv: FakeTv, publish, caplog):
        """The library discards the reply to a removal, so claiming either outcome
        would be a guess. Unknown is reported as unknown."""
        tv.holding["MY-LEGACY-1"] = Path("/gone/legacy-1.jpg")
        tv.removal_unconfirmable = True
        publish(["w1"])

        with caplog.at_level(logging.WARNING):
            await daemon.tick()

        assert "MY-LEGACY-1" in tv.holding
        assert "tv.orphan_removal_unconfirmed" in {r.__dict__.get("event") for r in caplog.records}

        tv.removal_unconfirmable = False
        publish(["w1"])
        await daemon.tick()
        assert "MY-LEGACY-1" not in tv.holding, "the next adoption did not try again"


async def test_a_store_from_a_newer_player_is_refused_rather_than_opened(tmp_path: Path):
    """The same rule as a manifest major from the future, and for the same reason.

    A reader that guesses at a shape it does not know writes damage, and this is
    the one file on the device whose loss is not free — the bindings are what
    stand between a restart and re-uploading the whole theme. Refusing at open is
    also the only moment it can be refused: after that the writes have happened.
    """
    from arrt_player.state import StateSchemaTooNew
    from arrt_player.state.store import SCHEMA_VERSION

    path = tmp_path / "display-state.sqlite"
    with DisplayState(path) as store:
        store.set_last_selected_work_id("w1")
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(StateSchemaTooNew) as refusal:
        DisplayState(path).__enter__()

    assert str(SCHEMA_VERSION) in str(refusal.value), "the refusal does not say which schema the Player understands"


async def test_a_store_written_by_a_player_that_read_major_1_still_opens_and_the_wall_carries_on(settings, tv, clock, publish):
    """Its record of the last directive acted on is a key nothing reads now, left where it is.

    A Pi upgraded from a Player that read major 1 holds this store, and its
    bindings and the work on the wall are what spare it re-uploading the theme
    and moving the picture on the first pass.
    """
    with DisplayState(settings.state_path) as store:
        store.set_last_selected_work_id("w1")
    with sqlite3.connect(settings.state_path) as connection:
        connection.execute("INSERT INTO daemon_state (key, value) VALUES ('last_acted_sequence', '41')")
    publish(["w1", "w2"], interval_seconds=10)

    with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
        assert store.last_selected_work_id == "w1"
        wall = frame_wall(settings=settings, tv=tv, state=store, watcher=Watcher(settings.manifest_path), clock=clock.as_clock())
        await wall.tick()
        clock.advance(10)
        await wall.tick()

    assert publish.work_of(tv.on_the_wall) == "w2"
    with sqlite3.connect(settings.state_path) as connection:
        assert connection.execute("SELECT value FROM daemon_state WHERE key = 'last_acted_sequence'").fetchone() == ("41",)


# -- a fault of the store's file or disk (issue #19) ---------------------------
#
# Driven at the sqlite3 connection, the boundary the store owns: the faults are
# the errors the sqlite3 module raises, with SQLite's result code and its name,
# as a real one carries them. One test meets a genuine fault of SQLite's own.


def _sqlite_error(name: str, message: str, kind: type[sqlite3.Error] = sqlite3.OperationalError) -> sqlite3.Error:
    """An error as the sqlite3 module raises it: SQLite's message, result code and code name."""
    error = kind(message)
    error.sqlite_errorcode = getattr(sqlite3, name)
    error.sqlite_errorname = name
    return error


class _FailingConnection:
    """The store's connection, raising `fault` from the statements `failing` picks, `times` times (None: always)."""

    def __init__(self, connection: sqlite3.Connection, fault, *, failing, times: int | None = None) -> None:
        self._connection = connection
        self._fault = fault
        self._failing = failing
        self.times = times
        self.raised = 0

    def _due(self, statement: str) -> bool:
        if self.times is not None and self.raised >= self.times:
            return False
        if not self._failing(statement):
            return False
        self.raised += 1
        return True

    def execute(self, statement: str, parameters=()):
        if self._due(statement):
            raise self._fault()
        return self._connection.execute(statement, parameters)

    def commit(self) -> None:
        if self._due("COMMIT"):
            raise self._fault()
        self._connection.commit()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def _arm(state: DisplayState, fault, *, failing=lambda _statement: True, times: int | None = None) -> _FailingConnection:
    armed = _FailingConnection(state._connection, fault, failing=failing, times=times)
    state._connection = armed
    return armed


def _disarm(state: DisplayState, armed: _FailingConnection) -> None:
    state._connection = armed._connection


def _a_binding_write(statement: str) -> bool:
    return "INSERT INTO tv_binding" in statement


def _store_events(caplog, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "event", None) == event]


class TestAFaultOfTheFileIsClassified:
    @pytest.mark.parametrize(
        ("name", "message", "may_clear"),
        [
            ("SQLITE_BUSY", "database is locked", True),
            ("SQLITE_LOCKED", "database table is locked", True),
            ("SQLITE_FULL", "database or disk is full", False),
            ("SQLITE_READONLY", "attempt to write a readonly database", False),
            ("SQLITE_IOERR", "disk I/O error", False),
            ("SQLITE_IOERR_WRITE", "disk I/O error", False),
            ("SQLITE_CORRUPT", "database disk image is malformed", False),
            ("SQLITE_NOTADB", "file is not a database", False),
        ],
    )
    def test_a_fault_of_the_disk_or_the_file_is_a_store_fault_of_its_class(
        self, state: DisplayState, name: str, message: str, may_clear: bool
    ):
        from arrt_player.state.store import StoreFault

        _arm(state, lambda: _sqlite_error(name, message))

        with pytest.raises(StoreFault) as raised:
            state.record_upload("w1", "MY-F0001")

        assert raised.value.may_clear is may_clear
        assert raised.value.operation == "record_upload"
        assert raised.value.fault == name
        assert message in str(raised.value), "the fault does not say what it was"
        assert "w1" in str(raised.value), "the fault does not say what it met"

    @pytest.mark.parametrize(
        "fault",
        [
            lambda: _sqlite_error("SQLITE_CONSTRAINT", "CHECK constraint failed", sqlite3.IntegrityError),
            lambda: _sqlite_error("SQLITE_ERROR", "no such table: tv_binding"),
            lambda: sqlite3.OperationalError("raised without a result code"),
        ],
        ids=["a-constraint", "this-codes-own-statement", "no-result-code"],
    )
    def test_anything_else_sqlite_raises_reaches_the_restart_unchanged(self, state: DisplayState, fault):
        """This code's own mistakes are the supervisor's to restart on, with a traceback that points at them."""
        from arrt_player.state.store import StoreFault

        expected = fault()
        _arm(state, lambda: expected)

        with pytest.raises(sqlite3.Error) as raised:
            state.record_upload("w1", "MY-F0001")

        assert raised.value is expected
        assert not isinstance(raised.value, StoreFault)

    def test_a_genuine_fault_of_sqlites_own_is_classified_by_its_code(self, state: DisplayState):
        """Not a constructed error: SQLite itself refuses the write, and the code it carries decides."""
        from arrt_player.state.store import StoreFault

        state._connection.execute("PRAGMA query_only = 1")

        with pytest.raises(StoreFault) as raised:
            state.set_last_selected_work_id("w1")

        assert (raised.value.fault, raised.value.may_clear) == ("SQLITE_READONLY", False)
        assert raised.value.operation == "set_last_selected_work_id"


class TestAWriteThatRaisedDidNotHappen:
    def test_a_refused_commit_is_not_read_back_or_published_by_the_next(self, state: DisplayState):
        """The transaction a refused commit leaves open is rolled back, so the
        store answers what is on disk and a later commit publishes only its own."""
        armed = _arm(state, lambda: _sqlite_error("SQLITE_BUSY", "database is locked"), failing=lambda s: s == "COMMIT", times=1)

        with pytest.raises(Exception):  # noqa: B017, PT011 -- the class is the classification test's; this is about what is left
            state.record_upload("w1", "MY-F0001")
        _disarm(state, armed)

        assert state.binding_for("w1") is None, "a write that raised is read back as kept"
        state.record_upload("w2", "MY-F0002")
        with sqlite3.connect(state._path) as other:
            rows = other.execute("SELECT artwork_id FROM tv_binding").fetchall()
        assert rows == [("w2",)], "the next commit published the write that raised"

    def test_the_work_on_the_wall_is_answered_while_the_file_cannot_be_read(self, state: DisplayState):
        """The heartbeat reads it on the pass a store fault ended, so it must not raise that fault."""
        state.set_last_selected_work_id("w1")
        _arm(state, lambda: _sqlite_error("SQLITE_IOERR", "disk I/O error"))

        assert state.last_selected_work_id == "w1"

    def test_the_work_on_the_wall_does_not_move_on_a_write_that_raised(self, state: DisplayState):
        state.set_last_selected_work_id("w1")
        armed = _arm(state, lambda: _sqlite_error("SQLITE_FULL", "database or disk is full"))

        with pytest.raises(Exception):  # noqa: B017, PT011 -- what is left is the subject here
            state.set_last_selected_work_id("w2")
        _disarm(state, armed)

        assert state.last_selected_work_id == "w1"
        with DisplayState(state._path) as reopened:
            assert reopened.last_selected_work_id == "w1"


class TestTheWallOutlivesAStoreFault:
    async def test_a_lock_met_once_is_one_warning_and_the_wall_goes_on(self, daemon: Wall, tv: FakeTv, publish, state, caplog):
        """The plan's first case: the store is locked once, at the write that records
        w1's upload. The pass ends with one WARNING and no crash, and the next pass
        uploads w1 again, records it and shows it. The first copy is on the set
        under an id nothing accounts for, so the orphan path removes it."""
        publish(["w1", "w2"], interval_seconds=3600)
        _arm(state, lambda: _sqlite_error("SQLITE_BUSY", "database is locked"), failing=_a_binding_write, times=1)

        with caplog.at_level(logging.INFO):
            await daemon.tick()
            assert tv.on_the_wall is None
            await daemon.tick()

        busy = _store_events(caplog, "store.busy")
        assert len(busy) == 1, f"a lock met once was said {len(busy)} times"
        assert busy[0].levelno == logging.WARNING
        assert busy[0].operation == "record_upload"
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert publish.work_of(tv.on_the_wall) == "w1", "the wall did not go on after the lock"
        assert len(_store_events(caplog, "store.recovered")) == 1
        assert "MY-F0001" not in state.accounted_content_ids(), "the copy whose record failed is accounted for"

    async def test_a_disk_that_fails_every_pass_is_one_error_and_the_heartbeat_names_it(
        self, daemon: Wall, tv: FakeTv, publish, state, caplog, clock, wall_dir
    ):
        """The plan's second case: every statement meets a disk I/O error, pass
        after pass. One ERROR for the episode, naming the operation; the wall's
        heartbeat names the fault; the screen's state is not reported unreachable;
        and when the disk answers again the wall carries on."""
        from test_display_state import heartbeat

        from arrt_player.heartbeat import INTERVAL_SECONDS

        publish(["w1", "w2"], interval_seconds=3600)
        await daemon.tick()  # w1 up
        armed = _arm(state, lambda: _sqlite_error("SQLITE_IOERR", "disk I/O error"))

        with caplog.at_level(logging.INFO):
            for _ in range(5):
                clock.advance(INTERVAL_SECONDS + 1)
                await daemon.tick()

        failed = _store_events(caplog, "store.failed")
        assert len(failed) == 1, f"a failing disk was said {len(failed)} times in five passes"
        assert failed[0].levelno == logging.ERROR
        # The pass's first use of the store: resolving the set's echo of w1's selection.
        assert failed[0].operation == "bindings"
        assert failed[0].fault == "SQLITE_IOERR"
        document = heartbeat(wall_dir)
        assert "disk I/O error" in document["last_error"]
        assert "SQLITE_IOERR" in document["last_error"]
        assert document["display_state"]["state"] == "showing_art", "a store fault was reported as an unreachable screen"
        assert document["television_reachable"] is None, "a pass that ended on the store claimed the set was unreachable"
        assert document["current_work_id"] == "w1"
        assert not _store_events(caplog, "tv.unavailable"), "a store fault was said as the set going away"

        _disarm(state, armed)
        with caplog.at_level(logging.INFO):
            clock.advance(INTERVAL_SECONDS + 1)
            await daemon.tick()
        assert len(_store_events(caplog, "store.recovered")) == 1
        assert heartbeat(wall_dir)["television_reachable"] is True

    async def test_a_lock_that_becomes_a_full_disk_is_said_at_both_levels(self, daemon: Wall, publish, state, caplog):
        publish(["w1", "w2"], interval_seconds=3600)
        faults = iter(
            [_sqlite_error("SQLITE_BUSY", "database is locked")] * 2
            + [_sqlite_error("SQLITE_FULL", "database or disk is full")] * 2
        )
        _arm(state, lambda: next(faults), times=4)

        with caplog.at_level(logging.INFO):
            for _ in range(4):
                await daemon.tick()

        assert len(_store_events(caplog, "store.busy")) == 1
        assert len(_store_events(caplog, "store.failed")) == 1

    async def test_the_sets_news_a_store_fault_cut_short_is_acted_on_at_the_next_pass(
        self, daemon: Wall, tv: FakeTv, publish, state, clock, wall_dir
    ):
        """The set announces art mode while the store cannot be read. The read is
        taken, and the store fails resolving the work on the wall; the news is not
        lost, so the next pass reads once more and the display state follows the
        set. The restart a store fault used to cost made the same read, because
        connecting is news."""
        from test_display_state import display

        publish(["w1", "w2"], interval_seconds=3600)
        await daemon.tick()  # w1 up
        tv.art_mode = "off"
        tv.art_mode_announced = True
        await daemon.tick()  # in use
        assert display(wall_dir)[0] == "in_use"

        tv.art_mode = "on"
        tv.art_mode_announced = True
        armed = _arm(
            state,
            lambda: _sqlite_error("SQLITE_BUSY", "database is locked"),
            failing=lambda s: s.startswith("SELECT artwork_id"),
            times=1,
        )
        reads = tv.art_mode_reads
        await daemon.tick()  # the read says art; the work cannot be resolved
        _disarm(state, armed)
        clock.advance(1)
        await daemon.tick()

        assert display(wall_dir) == ("showing_art", "w1"), "the set's news was lost to the store fault"
        assert tv.art_mode_reads == reads + 2

    async def test_a_picture_announced_while_the_store_cannot_be_read_is_not_lost(
        self, daemon: Wall, tv: FakeTv, publish, state, wall_dir
    ):
        """The sibling of the case above, for the other kind of news: somebody
        picks the wall's picture with the remote while the store fails the read
        that names its work. The announcement is still untaken at the next pass,
        so the display state follows it, with no read of the set for it."""
        from test_display_state import display

        publish(["w1", "w2"], interval_seconds=3600)
        await daemon.tick()  # w1 up, as MY-F0001
        tv.art_mode = "off"
        tv.art_mode_announced = True
        await daemon.tick()  # in use

        tv.art_mode = "on"
        tv.announce("MY-F0001", is_shown=True)
        armed = _arm(
            state,
            lambda: _sqlite_error("SQLITE_BUSY", "database is locked"),
            failing=lambda s: s.startswith("SELECT artwork_id"),
            times=1,
        )
        reads = tv.art_mode_reads
        await daemon.tick()
        _disarm(state, armed)
        await daemon.tick()

        assert display(wall_dir) == ("showing_art", "w1"), "the announced picture was lost to the store fault"
        assert tv.art_mode_reads == reads


# -- works nobody hangs any more (issue #18) -----------------------------------
#
# A binding is retired when no feed this wall adopted has named its work for 14
# days of the wall running with a feed: its image is removed from the set through
# the gated removal, confirmed by the set's own list, and then its row is
# dropped. The days are counted on the elapsed clock while the Player runs, so a
# Player switched off, or a wall clock that jumps, retires nothing.

#: One day, in the seconds the test clock moves by.
DAY = 86400.0


def _a_frame(settings, tv: FakeTv, store: DisplayState, clock) -> Wall:
    """A wall on the Frame over `store`, as `run_frame_wall` builds one."""
    return frame_wall(settings=settings, tv=tv, state=store, watcher=Watcher(settings.manifest_path), clock=clock.as_clock())


async def _a_theme_then_another(daemon: Wall, tv: FakeTv, publish, state: DisplayState) -> dict[str, str]:
    """Hang w1 and w2, then a theme of w3 alone: w3 is on the wall, w1 and w2 are on the set and named by no feed.

    Returns the content ids of w1 and w2.
    """
    publish(["w1", "w2"], interval_seconds=3600)
    await daemon.tick()  # w1 uploaded and shown, w2 uploaded behind it
    left = {work_id: state.binding_for(work_id).tv_content_id for work_id in ("w1", "w2")}
    assert set(left.values()) <= set(tv.holding)
    publish(["w3"], interval_seconds=3600)
    await daemon.tick()  # w3 uploaded and shown
    assert publish.work_of(tv.on_the_wall) == "w3"
    return left


def _a_schema_2_store(path: Path, rows: list[tuple], *, interrupted: bool = False) -> None:
    """A store as the previous release left it: schema 2, these `tv_binding` rows, one work on the wall.

    `interrupted` leaves it as a migration to 3 cut short after its columns were
    added: still at schema 2, the new columns present and null.
    """
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE tv_binding (
            id TEXT PRIMARY KEY, artwork_id TEXT NOT NULL UNIQUE, tv_content_id TEXT,
            tv_thumb_md5 TEXT, render_fingerprint TEXT, uploaded_at TEXT NOT NULL,
            upload_status TEXT NOT NULL CHECK (upload_status IN ('uploaded', 'failed', 'orphaned')),
            CHECK (upload_status <> 'uploaded' OR tv_content_id IS NOT NULL)
        );
        CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO daemon_state VALUES ('last_selected_work_id', 'w1');
        PRAGMA user_version = 2;
        """)
    connection.executemany(
        "INSERT INTO tv_binding (id, artwork_id, tv_content_id, tv_thumb_md5, render_fingerprint, uploaded_at, upload_status) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?)",
        rows,
    )
    if interrupted:
        connection.execute("ALTER TABLE tv_binding ADD COLUMN last_named_feed_seconds REAL")
        connection.execute("ALTER TABLE tv_binding ADD COLUMN last_named_at TEXT")
    connection.commit()
    connection.close()


class TestAWorkNobodyHangsLeavesTheSet:
    async def test_a_work_no_feed_has_named_for_fifteen_days_leaves_the_set_and_the_store(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock, caplog
    ):
        """The issue's case: the images of works the household took down stay on the set for good."""
        left = await _a_theme_then_another(daemon, tv, publish, state)
        removals = len(tv.removed)

        clock.advance(15 * DAY)
        with caplog.at_level(logging.INFO):
            await daemon.tick()
            await daemon.tick()

        assert not set(left.values()) & set(tv.holding), "a work no feed named for 15 days is still on the set"
        assert state.binding_for("w1") is None, "a retired work's row is still in the store"
        assert state.binding_for("w2") is None, "a retired work's row is still in the store"
        assert tv.removed[removals:] == [tuple(sorted(left.values()))], "the images were not taken off in one removal"
        kept = state.binding_for("w3").tv_content_id
        assert kept in tv.holding, "the work on the wall was taken off the set"
        assert state.accounted_content_ids() == {kept}
        retired = _store_events(caplog, "binding.retired")
        assert len(retired) == 1, f"one retirement was said {len(retired)} times"
        assert sorted(retired[0].retired) == ["w1", "w2"]
        assert sorted(retired[0].removed) == sorted(left.values())

    async def test_a_theme_switched_away_and_back_within_thirteen_days_uploads_nothing_again(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
    ):
        """Absence from one feed is not retirement: the way back costs no upload of the theme."""
        left = await _a_theme_then_another(daemon, tv, publish, state)
        uploads = len(tv.uploaded)

        for _ in range(13):
            clock.advance(DAY)
            await daemon.tick()
        publish(["w1", "w2"], interval_seconds=3600)
        for _ in range(3):
            await daemon.tick()

        assert len(tv.uploaded) == uploads, "a work back within 13 days was uploaded again"
        assert tv.removed == [], "something was taken off the set within 13 days"
        assert {work_id: state.binding_for(work_id).tv_content_id for work_id in left} == left
        assert publish.work_of(tv.on_the_wall) == "w1"

    async def test_a_retirement_due_while_the_set_shows_a_programme_is_held_and_done_later(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock, caplog
    ):
        """The removal is a write, so the gate holds it while somebody watches, once, without a line a pass."""
        left = await _a_theme_then_another(daemon, tv, publish, state)
        clock.advance(15 * DAY)
        await daemon.tick()  # the retirement falls due

        tv.art_mode = "off"  # somebody's programme; the panel is lit
        with caplog.at_level(logging.INFO):
            for _ in range(30):
                await daemon.tick()
                clock.advance(1)

        assert set(left.values()) <= set(tv.holding), "a removal reached a set somebody is watching"
        assert tv.took_the_screen == 0
        rows = {work_id: binding.tv_content_id for work_id in left if (binding := state.binding_for(work_id)) is not None}
        assert rows == left, "a held retirement lost its rows"
        assert set(left.values()) <= state.accounted_content_ids(), "a held retirement's images became orphans"
        held = _store_events(caplog, "tv.writes_held")
        assert len(held) == 1, f"a held removal was said {len(held)} times in thirty passes"
        assert held[0].verb == "remove"
        assert not _store_events(caplog, "binding.retired")
        assert not [
            record for record in caplog.records if record.levelno >= logging.WARNING
        ], "a held removal was said as a fault"

        tv.art_mode = "on"
        tv.art_mode_announced = True
        await daemon.tick()

        assert not set(left.values()) & set(tv.holding), "the held retirement was not made once the set showed art"
        assert state.binding_for("w1") is None
        assert state.binding_for("w2") is None

    async def test_a_work_named_again_while_its_retirement_waits_is_kept(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
    ):
        """A retirement held for a programme, and a feed naming w1 again before the set shows art: w1 stays.

        Its last naming is fifteen days old when the feed names it again, so
        only the feed's naming it now keeps it.
        """
        left = await _a_theme_then_another(daemon, tv, publish, state)
        clock.advance(15 * DAY)
        await daemon.tick()  # the retirement falls due
        tv.art_mode = "off"
        await daemon.tick()  # held

        publish(["w1", "w3"], interval_seconds=3600)
        await daemon.tick()
        tv.art_mode = "on"
        tv.art_mode_announced = True
        await daemon.tick()

        assert left["w1"] in tv.holding, "a work the feed names again was retired"
        assert left["w2"] not in tv.holding

    async def test_the_work_on_the_wall_is_never_retired(self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock):
        """A feed that names nothing leaves the wall on what it shows; neither that nor a picture the remote chose goes.

        w1 is the work the Player last put up; w2 is the picture somebody chose
        with the remote. No feed names either for fifteen days.
        """
        publish(["w1", "w2"], interval_seconds=3600)
        await daemon.tick()
        chosen = state.binding_for("w2").tv_content_id
        tv.announce(chosen, is_shown=True)
        publish([], interval_seconds=3600)
        await daemon.tick()

        clock.advance(15 * DAY)
        for _ in range(3):
            await daemon.tick()

        put_up = state.binding_for("w1")
        assert put_up is not None, "the work the Player put up lost its row"
        assert put_up.tv_content_id in tv.holding, "the work the Player put up was retired"
        assert chosen in tv.holding, "the picture on the wall was retired"
        assert tv.removed == []

    async def test_an_orphan_and_a_retirement_are_one_removal_and_the_orphan_is_said_as_today(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock, caplog
    ):
        left = await _a_theme_then_another(daemon, tv, publish, state)
        clock.advance(15 * DAY)
        await daemon.tick()  # the retirement falls due
        tv.holding["MY-LEGACY-1"] = Path("/gone/legacy-1.jpg")
        removals = len(tv.removed)

        with caplog.at_level(logging.INFO):
            await daemon.tick()

        assert tv.removed[removals:] == [("MY-LEGACY-1", *sorted(left.values()))]
        orphans = _store_events(caplog, "tv.orphans_removed")
        assert [record.removed for record in orphans] == [["MY-LEGACY-1"]], "the orphan's line names a retired work's image"
        assert [sorted(record.removed) for record in _store_events(caplog, "binding.retired")] == [sorted(left.values())]

    async def test_an_image_the_set_keeps_is_asked_for_again_only_with_the_next_feed(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
    ):
        """The orphan path's rule: a removal the set answered and refused is a known outcome, re-armed by news."""
        left = await _a_theme_then_another(daemon, tv, publish, state)
        tv.unremovable.add(left["w1"])
        clock.advance(15 * DAY)
        await daemon.tick()
        await daemon.tick()
        assert left["w1"] in tv.holding
        assert state.binding_for("w1").tv_content_id == left["w1"], "the row of an image the set kept was dropped"
        assert state.binding_for("w2") is None
        removals = len(tv.removed)

        for _ in range(5):
            clock.advance(3600)
            await daemon.tick()
        assert len(tv.removed) == removals, "an image the set refused to part with was asked for again every hour"

        tv.unremovable.clear()
        publish(["w3"], interval_seconds=3600)
        await daemon.tick()
        await daemon.tick()
        assert left["w1"] not in tv.holding, "the next feed did not ask again"
        assert state.binding_for("w1") is None


class TestRetirementIsMeasuredInTimeTheWallWatched:
    async def test_the_first_start_after_the_upgrade_retires_nothing_for_fourteen_days(
        self, settings, tv: FakeTv, publish, clock
    ):
        """A migration that read every existing binding as never named would empty the set on day one."""
        rows = [(f"{n}", f"w{n}", f"MY-OLD-{n}", "1:1", "2026-01-01T00:00:00+00:00", "uploaded") for n in range(1, 6)]
        library = {f"MY-OLD-{n}" for n in range(2, 6)}
        _a_schema_2_store(settings.state_path, rows)
        for _, _, content_id, *_ in rows:
            tv.holding[content_id] = Path(f"/old/{content_id}.jpg")
        publish(["w1"], interval_seconds=3600)

        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, tv, store, clock)
            for _ in range(13):
                await wall.tick()
                clock.advance(DAY)
            await wall.tick()
            assert library <= set(tv.holding), "the upgrade cost the set its library"
            assert len(store.bindings()) == 5

            for _ in range(2):
                clock.advance(DAY)
                await wall.tick()
            await wall.tick()
            assert not library & set(tv.holding), "fourteen days after the upgrade the works no feed named are still there"
            assert [binding.artwork_id for binding in store.bindings()] == ["w1"]

    async def test_a_player_switched_off_for_a_month_counts_none_of_it(self, settings, tv: FakeTv, publish, clock):
        """Thirty days off is not thirty days of nobody hanging the work, and a restart keeps the days already counted."""
        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, tv, store, clock)
            left = await _a_theme_then_another(wall, tv, publish, store)
            for _ in range(10):
                clock.advance(DAY)
                await wall.tick()

        clock.advance(30 * DAY)  # switched off: both clocks move, and a new boot's elapsed clock starts again

        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, tv, store, clock)
            for _ in range(3):
                await wall.tick()
                clock.advance(DAY)
            await wall.tick()
            assert set(left.values()) <= set(tv.holding), "days the Player was off were counted as days nobody hung the work"

            for _ in range(2):
                clock.advance(DAY)
                await wall.tick()
            await wall.tick()
            assert not set(left.values()) & set(tv.holding), "the days counted before the restart were lost"

    async def test_a_wall_clock_that_jumps_forward_retires_nothing(self, daemon: Wall, tv: FakeTv, publish, state, clock):
        """An NTP step on a Pi with no clock of its own moves wall time by years; no work has gone unnamed for them."""
        left = await _a_theme_then_another(daemon, tv, publish, state)

        clock.move_to(clock.as_clock().now() + timedelta(days=30))
        publish(["w3"], interval_seconds=3600)  # a republish, so the reconciliation that would retire them runs
        for _ in range(3):
            await daemon.tick()

        assert set(left.values()) <= set(tv.holding), "a jump of the wall clock retired works"
        assert tv.removed == []

    async def test_a_binding_whose_last_naming_is_unknown_is_never_retired(self, daemon: Wall, tv: FakeTv, publish, state, clock):
        """A row an older Player wrote carries no time; it is kept rather than read as never named."""
        left = await _a_theme_then_another(daemon, tv, publish, state)
        clock.advance(7200)
        await daemon.tick()  # the time w1 and w2 left the feed is saved
        state._connection.execute("UPDATE tv_binding SET last_named_feed_seconds = NULL WHERE artwork_id = 'w1'")
        state._connection.commit()

        clock.advance(15 * DAY)
        for _ in range(3):
            await daemon.tick()

        assert left["w1"] in tv.holding, "a binding with no time was read as never named"
        assert left["w2"] not in tv.holding


class TestTheStoreKeepsWhenAWorkWasLastNamed:
    @pytest.mark.parametrize("interrupted", [False, True], ids=["at-schema-2", "a-migration-cut-short"])
    def test_a_store_at_the_previous_schema_migrates_forward_and_keeps_every_binding(self, settings, clock, interrupted: bool):
        from arrt_player.state.store import SCHEMA_VERSION

        rows = [
            ("1", "w1", "MY-F0001", "10:20", "2026-01-01T00:00:00+00:00", "uploaded"),
            ("2", "w2", None, None, "2026-01-02T00:00:00+00:00", "failed"),
            ("3", "w3", None, None, "2026-01-03T00:00:00+00:00", "orphaned"),
        ]
        _a_schema_2_store(settings.state_path, rows, interrupted=interrupted)

        for _ in range(2):
            with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
                kept = {binding.artwork_id: binding for binding in store.bindings()}
                assert store.last_selected_work_id == "w1"
                assert store.feed_seconds == 0.0

        assert sorted(kept) == ["w1", "w2", "w3"], "a binding was lost by the migration"
        for _, work_id, content_id, fingerprint, uploaded_at, status in rows:
            binding = kept[work_id]
            assert (
                binding.tv_content_id,
                binding.render_fingerprint,
                binding.uploaded_at.isoformat(),
                str(binding.upload_status),
            ) == (
                content_id,
                fingerprint,
                uploaded_at,
                status,
            )
            assert binding.last_named_feed_seconds == 0.0, "an existing binding was not read as named at the upgrade"
            assert binding.last_named_at == clock.as_clock().now()
        with sqlite3.connect(settings.state_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert SCHEMA_VERSION == 4

    def test_the_wall_time_a_store_counted_survives_reopening_and_a_new_row_is_named_at_it(self, settings, clock):
        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            store.count_feed_time(5000.0)
            store.save_feed_time(named=[], left={})
        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            assert store.feed_seconds == 5000.0
            store.count_feed_time(10.0)
            binding = store.record_upload("w1", "MY-F0001")
            assert binding.last_named_feed_seconds == 5010.0
            assert store.binding_for("w1").last_named_feed_seconds == 5010.0

    def test_saving_names_what_the_feed_names_and_keeps_the_later_of_two_times(self, state: DisplayState):
        state.record_upload("w1", "MY-F0001")
        state.record_upload("w2", "MY-F0002")
        state.record_upload("w3", "MY-F0003")
        state.count_feed_time(100.0)

        state.save_feed_time(named=["w1"], left={"w2": 40.0, "w3": -1.0})

        assert state.binding_for("w1").last_named_feed_seconds == 100.0
        assert state.binding_for("w2").last_named_feed_seconds == 40.0
        assert state.binding_for("w3").last_named_feed_seconds == 0.0, "an earlier time replaced a later one"

    def test_a_count_that_does_not_read_as_a_number_counts_from_nought(self, settings, clock, caplog):
        """Nought can only make a work look named more recently than it was, so it delays a retirement and never hastens one."""
        with DisplayState(settings.state_path) as store:
            store.count_feed_time(9000.0)
            store.save_feed_time(named=[], left={})
        with sqlite3.connect(settings.state_path) as connection:
            connection.execute("UPDATE daemon_state SET value = 'lots' WHERE key = 'feed_seconds'")

        with caplog.at_level(logging.WARNING), DisplayState(settings.state_path) as store:
            assert store.feed_seconds == 0.0
        assert len(_store_events(caplog, "store.feed_time_unreadable")) == 1

    def test_a_row_is_retired_only_while_it_still_names_the_image_removed(self, state: DisplayState):
        """The row goes only if it still names what was taken off the set, so a re-upload in between is never lost."""
        state.record_upload("w1", "MY-F0002")

        assert state.retire("w1", "MY-F0001") is False
        assert state.binding_for("w1").tv_content_id == "MY-F0002"
        assert state.retire("w1", "MY-F0002") is True
        assert state.binding_for("w1") is None
        state.record_upload_failure("w2")
        assert state.retire("w2", None) is True


class TestTheCountCostsTheCardOneWriteAnHour:
    async def test_a_steady_wall_writes_the_count_once_an_hour_and_not_once_a_pass(
        self, daemon: Wall, publish, state: DisplayState, clock
    ):
        """The store is on an SD card: a pass that changes nothing writes nothing, and the count is written hourly."""
        publish(["w1", "w2"], interval_seconds=7200)
        await daemon.tick()
        await daemon.tick()
        spy = _arm(state, lambda: AssertionError("not raised"), failing=lambda _statement: False)
        statements: list[str] = []
        spy.execute = lambda statement, parameters=(): statements.append(statement) or spy._connection.execute(
            statement, parameters
        )

        for _ in range(59):
            clock.advance(60)
            await daemon.tick()
        writes = [statement for statement in statements if not statement.lstrip().upper().startswith("SELECT")]
        assert writes == [], f"a steady wall wrote to the store within the hour: {writes}"

        clock.advance(60)
        await daemon.tick()
        writes = [statement for statement in statements if not statement.lstrip().upper().startswith("SELECT")]
        assert any("feed_seconds" in statement for statement in writes), "the count was not written after an hour"
        assert len([statement for statement in writes if "daemon_state" in statement]) == 1


class TestARetirementOutlivesAStoreFault:
    async def test_a_removal_whose_row_could_not_be_dropped_is_not_sent_again(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock, caplog
    ):
        """The set took the image off and the store could not drop the row: the set's list says it is gone, which is success."""
        left = await _a_theme_then_another(daemon, tv, publish, state)
        clock.advance(15 * DAY)
        await daemon.tick()  # the retirement falls due
        armed = _arm(
            state,
            lambda: _sqlite_error("SQLITE_BUSY", "database is locked"),
            failing=lambda s: s.startswith("DELETE FROM tv_binding"),
            times=1,
        )

        with caplog.at_level(logging.INFO):
            await daemon.tick()
            assert not set(left.values()) & set(tv.holding)
            removals = list(tv.removed)
            _disarm(state, armed)
            clock.advance(1)
            await daemon.tick()

        assert len(_store_events(caplog, "store.busy")) == 1
        assert tv.removed == removals, "a removal the set had already made was sent again"
        assert state.binding_for("w1") is None
        assert state.binding_for("w2") is None
        assert [record for record in _store_events(caplog, "binding.retired") if record.retired]

    async def test_a_retired_rows_image_another_work_now_holds_is_not_removed(
        self, daemon: Wall, tv: FakeTv, publish, state: DisplayState, clock
    ):
        """A row left behind names an id the set has since given another work's upload: only the row goes.

        w2 is named by the feed and is not on the wall, so nothing but the
        shared id protects its picture.
        """
        publish(["w1", "w2"], interval_seconds=3600)
        await daemon.tick()  # w1 up; w2 uploaded behind it
        shared = state.binding_for("w2").tv_content_id
        state.record_upload("w9", shared)  # a stale row for a work no feed names, under the same id

        clock.advance(15 * DAY)
        for _ in range(3):
            await daemon.tick()

        assert shared in tv.holding, "a named work's image was removed for another work's stale row"
        assert tv.removed == []
        assert state.binding_for("w9") is None, "the stale row was kept"
        assert state.binding_for("w2").tv_content_id == shared


# -- a binding belongs to the set it was made on ------------------------------------
#
# A Samsung set's content ids are its own, small and sequential: an id one set
# gave a work names another picture on the next set, or none. So the wall reads
# which set it reaches before it uses a binding, the store keeps each set's
# bindings apart, and a store that cannot say which set its bindings are from
# sends no removal.

#: Two sets, as their descriptions name them.
THIS_SET = "uuid:frame-in-the-living-room"
ANOTHER_SET = "uuid:frame-in-the-bedroom"

#: A picture the household put on a set, under the id the first set gave w1.
HOUSEHOLDS = Path("/the-households/own-picture.jpg")


def _a_set(device_id: str | None, *, numbered_from: int = 10, **holding: Path) -> FakeTv:
    """A set with this description's id and these pictures, giving its uploads ids from `numbered_from` up."""
    tv = FakeTv()
    tv.device_id = device_id
    tv.holding.update(holding)
    tv._next_id = numbered_from
    return tv


async def _a_life_on(settings, clock, publish, tv: FakeTv, works=("w1", "w2")) -> dict[str, str]:
    """The wall shows the first work on `tv` and uploads the rest; returns each work's id on that set."""
    with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
        wall = _a_frame(settings, tv, store, clock)
        publish(list(works), interval_seconds=3600)
        await wall.tick()
        return {binding.artwork_id: binding.tv_content_id for binding in store.bindings()}


def _a_schema_3_store(path: Path, rows: list[tuple], *, identity: str | None = None) -> None:
    """A store as the previous release left it: schema 3, these rows, one work on the wall, no set recorded.

    `identity` leaves a `tv_identity` key in it, as a store rolled back to the
    previous release and forward again would hold one.
    """
    _a_schema_2_store(path, rows)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE tv_binding ADD COLUMN last_named_feed_seconds REAL")
        connection.execute("ALTER TABLE tv_binding ADD COLUMN last_named_at TEXT")
        connection.execute("UPDATE tv_binding SET last_named_feed_seconds = 0, last_named_at = '2026-01-01T00:00:00+00:00'")
        connection.execute("INSERT INTO daemon_state VALUES ('native_slideshow_disabled', '1')")
        if identity is not None:
            connection.execute("INSERT INTO daemon_state VALUES ('tv_identity', ?)", (identity,))
        connection.execute("PRAGMA user_version = 3")


class TestTheStoreKeepsEachSetsBindingsApart:
    def test_another_set_starts_with_none_and_the_first_gets_its_own_back(self, state: DisplayState, clock):
        from arrt_player.state.store import SetChange

        assert state.belongs_to(THIS_SET).change is SetChange.ADOPTED
        state.record_upload("w1", "MY-F0001", render_fingerprint="sha256:w1")
        state.record_upload_failure("w2")
        state.set_last_selected_work_id("w1")
        state.mark_native_slideshow_disabled()
        state.count_feed_time(5 * DAY)
        state.save_feed_time(named=["w1"], left={})

        clock.advance(3600)
        met = state.belongs_to(ANOTHER_SET)
        assert (met.change, met.previous, met.set_aside, met.restored) == (SetChange.CHANGED, THIS_SET, 2, 0)
        assert state.bindings() == (), "another set was handed the first set's bindings"
        assert state.accounted_content_ids() == frozenset()
        assert state.last_selected_work_id is None, "the work on the first set's wall was carried to another"
        assert not state.native_slideshow_disabled, "the first set's slideshow switch was carried to another"
        assert state.feed_seconds == 5 * DAY, "the wall's own running time was taken for a fact about the set"
        state.record_upload("w1", "MY-F0001", render_fingerprint="sha256:w1-on-the-bedroom-set")

        clock.advance(3600)
        met = state.belongs_to(THIS_SET)
        assert (met.change, met.previous, met.set_aside, met.restored) == (SetChange.CHANGED, ANOTHER_SET, 1, 2)
        back = {binding.artwork_id: binding for binding in state.bindings()}
        assert (back["w1"].tv_content_id, back["w1"].render_fingerprint) == ("MY-F0001", "sha256:w1")
        assert back["w2"].upload_status is UploadStatus.FAILED
        assert all(
            binding.last_named_at == clock.as_clock().now() for binding in back.values()
        ), "a binding back from aside kept the time a feed named it before the wall left"
        assert state.belongs_to(THIS_SET).change is SetChange.SAME

        with DisplayState(state._path) as reopened:
            assert reopened.tv_identity == THIS_SET
            assert reopened.feed_seconds == 5 * DAY
            assert reopened.belongs_to(ANOTHER_SET).restored == 1, "the second set's binding was not kept for it"

    @pytest.mark.parametrize("identity", [None, ANOTHER_SET], ids=["schema-3", "rolled-back-and-forward"])
    def test_a_schema_3_store_opens_with_every_binding_and_no_set(self, settings, clock, identity: str | None):
        """A store the previous release wrote was made on some set, and nothing says which; one that holds an
        identity was rolled back to a Player that wrote bindings without looking, so it does not know either."""
        from arrt_player.state.store import SCHEMA_VERSION, SetChange

        rows = [("1", "w1", "MY-F0001", "10:20", "2026-01-01T00:00:00+00:00", "uploaded")]
        _a_schema_3_store(settings.state_path, rows, identity=identity)

        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            assert store.tv_identity is None
            assert [(binding.artwork_id, binding.tv_content_id) for binding in store.bindings()] == [("w1", "MY-F0001")]
            assert store.last_selected_work_id == "w1"
            met = store.belongs_to(THIS_SET)
            assert met.change is SetChange.ADOPTED
            assert [binding.artwork_id for binding in store.bindings()] == ["w1"], "the first set read did not keep them"
            assert store.native_slideshow_disabled
        with sqlite3.connect(settings.state_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)

    #: The statements a move of the bindings makes, its commit last: a fault is met at each in turn.
    MOVE_STATEMENTS = 10

    @pytest.mark.parametrize("at", range(1, MOVE_STATEMENTS + 1))
    def test_a_fault_while_the_bindings_move_leaves_them_under_their_set(self, state: DisplayState, at: int):
        from arrt_player.state.store import StoreFault

        state.belongs_to(THIS_SET)
        state.record_upload("w1", "MY-F0001")
        state.set_last_selected_work_id("w1")
        met: list[str] = []

        def the_one_at(statement: str) -> bool:
            met.append(statement)
            return len(met) == at

        armed = _arm(state, lambda: _sqlite_error("SQLITE_FULL", "database or disk is full"), failing=the_one_at, times=1)

        with pytest.raises(StoreFault) as raised:
            state.belongs_to(ANOTHER_SET)

        assert armed.raised == 1, f"the move made {len(met)} statements, not {at}"
        assert at < self.MOVE_STATEMENTS or met[-1] == "COMMIT", "the last statement of the move is not its commit"
        assert raised.value.operation == "belongs_to"
        _disarm(state, armed)
        assert state.tv_identity == THIS_SET
        assert [(binding.artwork_id, binding.tv_content_id) for binding in state.bindings()] == [("w1", "MY-F0001")]
        assert state.last_selected_work_id == "w1"
        with DisplayState(state._path) as reopened:
            assert reopened.tv_identity == THIS_SET, "a move cut short recorded the other set"
            assert [binding.artwork_id for binding in reopened.bindings()] == ["w1"]
            assert reopened._connection.execute("SELECT COUNT(*) FROM tv_binding_aside").fetchone()[0] == 0


class TestABindingIsUsedOnlyOnTheSetItWasMadeOn:
    async def test_another_set_is_sent_none_of_the_first_sets_ids(self, settings, clock, publish, caplog, wall_dir):
        """The defect: the household's picture under w1's old id was selected as w1, and announced as w1."""
        from test_display_state import display

        first = _a_set(THIS_SET)
        on_the_first = await _a_life_on(settings, clock, publish, first)
        assert on_the_first["w1"] == "MY-F0011"
        another = _a_set(ANOTHER_SET, numbered_from=20, **{"MY-F0011": HOUSEHOLDS})

        with caplog.at_level(logging.INFO), DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, another, store, clock)
            publish(["w1", "w2"], interval_seconds=3600)
            await wall.tick()
            another.announce("MY-F0011", is_shown=True)  # somebody puts the household's picture up with the remote
            await wall.tick()
            shown_by_the_remote = display(wall_dir)

        assert another.selected == ["MY-F0021"], f"selected {another.selected} on another set"
        assert publish.work_of(another.holding["MY-F0021"]) == "w1"
        assert shown_by_the_remote == ("showing_art", None), "the household's picture was reported as a work of the first set's"
        changed = _store_events(caplog, "binding.set_changed")
        assert len(changed) == 1
        assert (changed[0].set_aside, changed[0].restored) == (2, 0)
        journal = [f"{record.getMessage()} {record.__dict__}" for record in caplog.records]
        assert not [line for line in journal if THIS_SET in line or ANOTHER_SET in line], "the journal printed a set's own id"
        assert changed[0].set_was != changed[0].set_now

    async def test_a_retirement_due_on_the_first_set_is_made_there_and_never_on_another(self, settings, clock, publish, caplog):
        """w1 and w2 are due when the wall meets another set: their bindings are not that set's, so nothing of
        theirs is retired there, and on the first set again they are retired after a full wait, from there."""
        first = _a_set(THIS_SET)
        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, first, store, clock)
            left = await _a_theme_then_another(wall, first, publish, store)
            clock.advance(15 * DAY)
            await wall.tick()  # the count is written and finds w1 and w2 due; their removal is owed
            assert set(left.values()) <= set(first.holding)

        another = _a_set(ANOTHER_SET)
        with caplog.at_level(logging.INFO), DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, another, store, clock)
            for _ in range(3):
                await wall.tick()
                clock.advance(DAY)
        assert another.removed == [], "a binding of the first set was the reason for a removal from another"
        assert not _store_events(caplog, "binding.retired")

        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, first, store, clock)
            await wall.tick()
            assert set(left.values()) <= set(first.holding), "bindings back on their set were retired at once"
            for _ in range(15):
                clock.advance(DAY)
                await wall.tick()
            await wall.tick()
        assert not set(left.values()) & set(first.holding), "the first set's works were never retired on it"
        assert first.removed
        assert all(set(request) <= set(left.values()) for request in first.removed)

    async def test_the_set_is_asked_again_after_it_went_away(self, settings, clock, publish, caplog):
        """Another set can answer at the address after an outage: a router's new lease, or a replaced television."""
        tv = _a_set(THIS_SET)
        with caplog.at_level(logging.INFO), DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, tv, store, clock)
            publish(["w1", "w2"], interval_seconds=3600)
            await wall.tick()
            assert tv.selected == ["MY-F0011"]
            tv.unavailable = True
            publish(["w2", "w1"], interval_seconds=3600)  # a republish, so the pass asks the set something
            await wall.tick()
            assert _store_events(caplog, "tv.unavailable")
            # The set behind the address is now another, holding the household's picture under w1's old id.
            tv.unavailable, tv.device_id, tv.holding, tv.selected = False, ANOTHER_SET, {"MY-F0011": HOUSEHOLDS}, []
            tv.displaying, tv._next_id = "MY-F0011", 20
            tv.brightness.clear()
            clock.advance(400)
            await wall.tick()

        assert tv.selected == ["MY-F0021"], f"selected {tv.selected} on the set that answered after the outage"
        assert publish.work_of(tv.on_the_wall) == "w2"
        assert tv.brightness, "the brightness written to the first set was taken as written to the second"
        assert len(_store_events(caplog, "binding.set_changed")) == 1

    async def test_a_fault_recording_the_set_ends_the_pass_with_no_binding_used(self, settings, clock, publish, caplog):
        first = _a_set(THIS_SET)
        await _a_life_on(settings, clock, publish, first)
        another = _a_set(ANOTHER_SET, numbered_from=20, **{"MY-F0011": HOUSEHOLDS})

        with caplog.at_level(logging.INFO), DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, another, store, clock)
            publish(["w1", "w2"], interval_seconds=3600)
            armed = _arm(store, lambda: _sqlite_error("SQLITE_BUSY", "database is locked"), failing=lambda s: "aside" in s)
            await wall.tick()
            assert armed.raised == 1
            assert (another.selected, another.uploaded, another.removed) == ([], [], [])
            assert store.tv_identity == THIS_SET
            await wall.tick()
            _disarm(store, armed)
            clock.advance(10)
            await wall.tick()

        assert len(_store_events(caplog, "store.busy")) == 1
        assert "MY-F0011" not in another.selected
        assert publish.work_of(another.on_the_wall) == "w1"


class TestASetThatWillNotSayWhichItIs:
    async def test_the_wall_uses_no_binding_says_it_once_and_asks_on_its_ladder(self, settings, clock, publish, caplog, wall_dir):
        from test_display_state import heartbeat

        first = _a_set(THIS_SET)
        await _a_life_on(settings, clock, publish, first)
        first.device_id = None
        first.selected.clear()

        with caplog.at_level(logging.INFO), DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, first, store, clock)
            for _ in range(20):
                await wall.tick()
                clock.advance(1)

        assert (first.selected, first.removed) == ([], []), "a binding was used on a set that did not say which it is"
        assert first.identity_reads <= 4, f"the identity was asked {first.identity_reads} times in twenty seconds"
        assert len(_store_events(caplog, "tv.unidentified")) == 1
        document = heartbeat(wall_dir)
        assert "did not say which set it is" in document["last_error"]
        assert document["television_reachable"] is None

    async def test_a_store_from_before_the_upgrade_sends_no_removal_until_the_set_says(self, settings, clock, publish, caplog):
        """The upgrade's danger: a store from the previous release does not know its set. Whatever the wall does
        on a set that will not say, through its patience and days beyond it, nothing is removed from that set."""
        from arrt_player.displays.frame import IDENTITY_PATIENCE_SECONDS

        rows = [(f"{n}", f"w{n}", f"MY-F000{n}", "1:1", "2026-01-01T00:00:00+00:00", "uploaded") for n in range(1, 4)]
        _a_schema_3_store(settings.state_path, rows)
        tv = _a_set(None, **{f"MY-F000{n}": Path(f"/somebodys/{n}.jpg") for n in range(1, 5)})

        with caplog.at_level(logging.INFO), DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, tv, store, clock)
            publish(["w9"], interval_seconds=3600)  # none of the store's works: every row is due in fifteen days
            await wall.tick()
            clock.advance(IDENTITY_PATIENCE_SECONDS)
            for _ in range(20):
                await wall.tick()
                clock.advance(DAY)
            assert [binding.artwork_id for binding in store.bindings()] == ["w1", "w2", "w3"], "a row was dropped"
            assert store.tv_identity is None

        assert tv.removed == [], "a removal was sent from a store that does not know which set it is on"
        assert not {"MY-F0001", "MY-F0002", "MY-F0003"} & set(tv.selected)
        assert publish.work_of(tv.on_the_wall) == "w9", "a set that would not say was shown no art"
        assert len(_store_events(caplog, "tv.unidentified_going_on")) == 1

    async def test_a_set_back_from_an_outage_that_will_not_say_gets_the_slots_work_from_the_store_in_memory(
        self, settings, clock, publish
    ):
        """The programme remembers through the store in use, so it does not believe the first set's picture is up."""
        from arrt_player.displays.frame import IDENTITY_PATIENCE_SECONDS

        tv = _a_set(THIS_SET)
        with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, tv, store, clock)
            publish(["w1", "w2"], interval_seconds=3600)
            await wall.tick()
            tv.unavailable = True
            publish(["w1", "w2"], interval_seconds=3600)
            await wall.tick()
            tv.unavailable, tv.device_id, tv.selected = False, None, []
            for _ in range(3):
                clock.advance(IDENTITY_PATIENCE_SECONDS / 2)
                await wall.tick()
            assert store.last_selected_work_id == "w1", "the store in memory wrote to the device's store"

        assert tv.selected, "the wall believed the picture its own store last put up was on a set it could not name"
        assert "MY-F0011" not in tv.selected
        assert publish.work_of(tv.on_the_wall) == "w1"

    async def test_a_store_from_before_the_upgrade_takes_the_first_set_read_and_uploads_nothing_again(
        self, settings, clock, publish, caplog
    ):
        tv = _a_set(THIS_SET)
        await _a_life_on(settings, clock, publish, tv)
        with sqlite3.connect(settings.state_path) as connection:
            connection.execute("DELETE FROM daemon_state WHERE key = 'tv_identity'")
            connection.execute("DROP TABLE tv_binding_aside")
            connection.execute("PRAGMA user_version = 3")
        uploaded = list(tv.uploaded)

        with caplog.at_level(logging.INFO), DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
            wall = _a_frame(settings, tv, store, clock)
            for _ in range(3):
                await wall.tick()
            assert store.tv_identity == THIS_SET

        assert tv.uploaded == uploaded, "the upgrade uploaded the library again"
        assert tv.removed == []
        assert len(_store_events(caplog, "binding.set_adopted")) == 1


class _AModeThatWillNotRead(FakeTv):
    """A set whose own art-mode flag cannot be read for the still wall's line, as when the read drops the connection."""

    async def reported_art_mode(self) -> str | None:
        return None


@pytest.mark.parametrize(
    "failure",
    ["an upload the set refused", "a removal the set could not confirm", "a still wall whose mode would not read"],
)
async def test_a_failure_that_may_have_cost_the_connection_has_the_next_one_identified(settings, clock, publish, failure):
    """Each of these is how the seam reports a set that went away mid-verb, and a pass after it reconnects quietly."""
    tv = _AModeThatWillNotRead() if failure.startswith("a still wall") else FakeTv()
    if failure.startswith("an upload"):
        tv.refuse_uploads = True
    elif failure.startswith("a removal"):
        tv.holding["MY-F0999"] = Path("/nobodys/picture.jpg")
        tv.removal_unconfirmable = True
    else:
        tv.displays_nothing_selected = True

    with DisplayState(settings.state_path, now=lambda: clock.as_clock().now()) as store:
        wall = _a_frame(settings, tv, store, clock)
        publish(["w1", "w2"], interval_seconds=3600)
        await wall.tick()
        assert tv.identity_reads == 1
        clock.advance(1)
        await wall.tick()

    assert tv.identity_reads == 2, f"after {failure}, the next pass used the bindings without asking which set it reaches"
