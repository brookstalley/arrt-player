"""What this television holds, what this device believes, and the gap between them.

Two defects shape everything here. The 2024 loader recorded a successful upload
with no content id, so a failure read as a success; and the television's single
user-upload category means anything this device cannot account for is an image
nobody will ever remove if this process does not.
"""

import logging
import sqlite3
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
