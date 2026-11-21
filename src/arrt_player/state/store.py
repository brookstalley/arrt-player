"""`display-state.sqlite` — what this television holds, and where the wall got to.

**Per-device runtime state never lives in the catalogue.** Which image this
particular television holds under which of its own identifiers is a fact about
one device, and putting it in the catalogue is what made the 2024 tree's
`tv_content_id` column impossible to restore onto a different set. This store is
the process that norm lives in: display is its sole writer, curation never opens
it, and losing it costs a re-upload rather than a curatorial record.

Four things live here:

* **`tv_binding`** — the work-to-content-id map, modelled in Arrt's `data-model.md`
  because it is the entity that carries the norm across the boundary between the Player and the server.
* **`daemon_state`** — which television the bindings were made on
  (`tv_identity`, below), which work the wall last showed, whether the set's own
  slideshow has been switched off, and how long the wall has run with a feed
  (`feed_seconds`, below). Display-internal, never read by anything else. A key
  is read only by name, so a key no code reads any more (a store written by an
  older Player) is left in place and costs nothing.
* **`tv_binding_aside`** — the bindings of a television the wall was moved away
  from, kept under that set's identity for its return.
* **`tv_upload`** — every picture this Player sent to a set, recorded before it
  was sent (below).

**The Player removes from a set only what it recorded putting there.** A set's
uploaded category also holds the household's own pictures, and nothing about a
picture in it says who sent it. So every upload is recorded before it is sent,
with the set it goes to (or none, while the set will not say which it is) and
the marker it carries as its `image_date`, which the set echoes in its listing;
the content id is added once the set gives one. A listed picture is this
Player's on set X only when a record on X names its id and, where the record
holds a marker, the listing shows that marker beside it; an upload whose id was
never learned is found by its marker alone, and only when exactly one listed
picture carries it. The markers of live records are never two alike
(`record_sending`). A record leaves once the listing of its set no longer
shows it, so the table holds what may still be on a set and no more. A store
from before the record existed takes every binding with a content id, current
or set aside, as recorded under the set it was made on, with no marker; one
that knew no set records them when it first adopts one.

**A binding belongs to the set it was made on, and a different set starts with
none.** A Samsung set's content ids are its own, small and sequential, so an id
recorded on one set names some other picture on the next one, or nothing:
selecting it shows somebody else's picture, and removing it deletes one. So the
store records the set's identity (`device.duid`, read by the wall before it
uses any binding) and `belongs_to` compares: the same set carries on; a
different one has the bindings set aside under the old identity, gets back any
set aside under its own, and starts with no work on the wall and its own
slideshow not known to be off, which are facts about a set. One transaction, so
a fault or a power cut leaves the bindings as they were, still under the
identity they were made on.

**A store with no identity recorded sends no removal.** One written before the
identity existed (schema 3 and older), or one the Player holds in memory while
the set will not say who it is, was made on *some* set and nothing says which;
the wall reads the set before it uses one, and a removal is sent only from a
store whose recorded identity is the set's (`displays/frame.py`). A store from
before the identity takes the first one read as its own, because for a Player
whose set has not changed that is true, and the alternative re-uploads every
wall's whole library on the first start after the upgrade.

**When a work was last named is measured in the wall's own running time, not
the calendar.** A binding is retired once no feed this wall adopted has named
its work for long enough (`displays/frame.py`), and "long enough" has to mean
time the household could have hung it and did not: not a month the Player was
switched off, and not an NTP step on a Pi with no clock of its own, which moves
wall time by years. So `feed_seconds` counts the seconds this wall has run with
a feed adopted, from the elapsed clock and only while the Player runs, and a
binding's `last_named_feed_seconds` is that count when a feed last named its
work. `last_named_at` is the wall time of the same moment, for whoever reads the
file; nothing compares it.

**`upload_status` is explicit, and the table refuses the 2024 defect
structurally.** That version's `upload_file` caught every exception, logged, and
still reported success — recording no content id while its retry loop set
`success = True`. A row here cannot say `uploaded` without an id: the check
constraint rejects it, so the failure mode is a write that raises during
development rather than a wall that silently shows nothing.

**A fault of the file or the disk is raised as `StoreFault`, and nothing else
is.** Every operation the wall makes on a running store reads SQLite's own
result code from what failed and classifies it (`_FAULTS`): a lock or a busy
database may clear by itself, and a full or read-only disk, an I/O error or a
damaged file will not. Anything else SQLite raises (a constraint, a statement
this code got wrong) is this code's mistake, raised unchanged so that the
supervisor's restart, the last resort, still answers it with a traceback.
**A write that raised did not happen**: it is rolled back before the fault is
raised, so what the store answers afterwards is what is on disk.
"""

import contextlib
import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Self

log = logging.getLogger(__name__)

#: Bumped when a migration lands. Read from `PRAGMA user_version`, which SQLite
#: carries in the file header for exactly this purpose and costs no table.
#:
#: 2 (2026-08-06) added `render_fingerprint`, without which a re-rendered work
#: kept showing its old image on the television for ever.
#:
#: 3 (2026-11-19) added `last_named_feed_seconds` and `last_named_at`, without
#: which an image whose work no feed names any more stayed on the set for good.
#:
#: 4 (2026-11-19) added `tv_identity` and `tv_binding_aside`, without which a
#: wall pointed at another television selected and removed that set's pictures
#: by the ids of the last one. Opening a store below 4 forgets any identity it
#: holds (`_widen`), because a Player older than 4 may have written bindings to
#: it on another set since.
#:
#: 5 (2026-11-20) added `tv_upload`, without which the Player removed every
#: uploaded picture on a set that it could not account for, the household's
#: own included. A store below 5 has its bindings taken as recorded (`_widen`).
#: The previous release refuses a store at 5, as it refuses one at 4.
SCHEMA_VERSION: Final[int] = 5


class UploadStatus(StrEnum):
    """What is known about one work's image on this television.

    `failed` exists so that "we tried and it did not land" is a different state
    from "we have not tried" — the absence of a row. Collapsing those is the
    original defect this store was designed against.
    """

    UPLOADED = "uploaded"
    FAILED = "failed"
    #: The television no longer lists the content id this row names. The row is
    #: kept rather than deleted so the next reconciliation re-uploads rather than
    #: rediscovering the work as new, and so the journal can say what happened.
    ORPHANED = "orphaned"


class StateSchemaTooNew(RuntimeError):
    """The store on disk was written by a newer Player than this one.

    Refused rather than opened, for the same reason a manifest major from the
    future is refused: a reader that guesses at a shape it does not know writes
    damage, and this is the one file whose loss is not free.
    """


class StoreFault(Exception):
    """One operation on the store failed for a reason outside this code: the disk, the file, or a lock.

    `may_clear` is the classification (`_FAULTS`): True for a lock or a busy
    database, which another moment may not meet; False for a fault the wall
    cannot outlive without somebody fixing the disk or the file. `fault` is
    SQLite's name for it (`SQLITE_IOERR`), and `operation` is the store's method
    that met it, so a journal line and the heartbeat say both.
    """

    def __init__(self, operation: str, doing: str, cause: sqlite3.Error, *, fault: str, may_clear: bool) -> None:
        super().__init__(f"the display store could not {doing} ({fault}: {cause})")
        self.operation = operation
        self.fault = fault
        self.may_clear = may_clear


#: SQLite's primary result codes for the faults of the file and the disk, and
#: whether each may clear by itself. **Read from the result code, never from the
#: class or the message**: `OperationalError` also carries this code's own
#: mistakes (`SQLITE_ERROR`, "no such table"), which no wait would cure. A code
#: absent here is not a store fault and is raised unchanged.
_FAULTS: Final[dict[int, bool]] = {
    # Another connection holds the file: it may let go at the next pass.
    sqlite3.SQLITE_BUSY: True,
    sqlite3.SQLITE_LOCKED: True,
    # The disk or the file: nothing here changes them.
    sqlite3.SQLITE_FULL: False,
    sqlite3.SQLITE_READONLY: False,
    sqlite3.SQLITE_IOERR: False,
    sqlite3.SQLITE_CORRUPT: False,
    sqlite3.SQLITE_NOTADB: False,
    sqlite3.SQLITE_CANTOPEN: False,
    sqlite3.SQLITE_PERM: False,
}

#: The extended result code's low byte is its primary code (`SQLITE_IOERR_WRITE`
#: is `SQLITE_IOERR | 3 << 8`), which is what the classification is keyed on.
_PRIMARY: Final[int] = 0xFF


@dataclass(frozen=True)
class Binding:
    """One work's image on this television."""

    artwork_id: str
    tv_content_id: str | None
    upload_status: UploadStatus
    uploaded_at: datetime
    #: What the render file looked like when it was sent. Null on a row written
    #: before this column existed, and on one that never got as far as a file.
    render_fingerprint: str | None = None
    tv_thumb_md5: str | None = None
    #: The wall's `feed_seconds` when a feed it adopted last named this work.
    #: Null only on a row an older Player wrote since the store last migrated,
    #: which the next migration names at that moment; a row with none is never
    #: retired (`displays/frame.py`).
    last_named_feed_seconds: float | None = None
    #: The wall time of the same moment, for a reader of the file. Never compared.
    last_named_at: datetime | None = None

    @property
    def is_on_the_television(self) -> bool:
        """Whether this row claims the set is holding the image right now."""
        return self.upload_status is UploadStatus.UPLOADED and self.tv_content_id is not None


@dataclass(frozen=True)
class Sending:
    """An upload recorded before it is sent: the record's `key`, and the `marker` the upload must carry."""

    key: str
    marker: str


class SetChange(StrEnum):
    """What `belongs_to` found: the set the bindings were made on, or another."""

    #: The bindings were made on this set.
    SAME = "same"
    #: The store recorded no set (written before the identity existed); it takes this one.
    ADOPTED = "adopted"
    #: Another set: its bindings are set aside, and this set's, if any were, are back.
    CHANGED = "changed"


@dataclass(frozen=True)
class SetMet:
    """The answer of `belongs_to`: what it found, the identity the bindings had before, and what moved."""

    change: SetChange
    previous: str | None = None
    #: How many bindings were set aside under `previous`, and how many came back for this set.
    set_aside: int = 0
    restored: int = 0


_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS tv_binding (
    id             TEXT PRIMARY KEY,
    artwork_id     TEXT NOT NULL UNIQUE,
    tv_content_id  TEXT,
    tv_thumb_md5   TEXT,
    -- What the file looked like when it was uploaded. The television is handed a
    -- path, and `ready/{artwork_id}.jpg` is stable across re-renders — so
    -- changing a mat colour rewrites the bytes under an unchanged name, and
    -- without this the binding still reads `uploaded` and the wall shows the old
    -- composition until somebody notices by eye.
    render_fingerprint TEXT,
    uploaded_at    TEXT NOT NULL,
    -- When a feed this wall adopted last named the work, in the wall's running
    -- time with a feed (`daemon_state.feed_seconds`), and the wall time of the
    -- same moment for a reader. What retires a binding is the first.
    last_named_feed_seconds REAL,
    last_named_at  TEXT,
    upload_status  TEXT NOT NULL
        CHECK (upload_status IN ('uploaded', 'failed', 'orphaned')),
    -- The 2024 defect, made unwritable: a row may not claim the image is on the
    -- television without naming the identifier the television gave it back.
    CHECK (upload_status <> 'uploaded' OR tv_content_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS daemon_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- The bindings of a set the wall has been moved away from, under that set's
-- identity, every column of `tv_binding` as it was. Read only when that set is
-- met again; never selected from, removed from or accounted for meanwhile.
CREATE TABLE IF NOT EXISTS tv_binding_aside (
    tv_identity    TEXT NOT NULL,
    id             TEXT NOT NULL,
    artwork_id     TEXT NOT NULL,
    tv_content_id  TEXT,
    tv_thumb_md5   TEXT,
    render_fingerprint TEXT,
    uploaded_at    TEXT NOT NULL,
    last_named_feed_seconds REAL,
    last_named_at  TEXT,
    upload_status  TEXT NOT NULL,
    PRIMARY KEY (tv_identity, artwork_id)
);

-- Every picture this Player sent to a set, written before it was sent. The
-- set it went to (null: sent while the set would not say which it is), the
-- `image_date` it carried (null only on a record carried from a binding an
-- older Player wrote), and the id the set gave it (null until learned). What
-- the Player may remove from a set is what these name there, and nothing else.
CREATE TABLE IF NOT EXISTS tv_upload (
    id             TEXT PRIMARY KEY,
    tv_identity    TEXT,
    marker         TEXT,
    tv_content_id  TEXT,
    sent_at        TEXT NOT NULL,
    -- A record names its picture by its marker, its id, or both: never neither.
    CHECK (marker IS NOT NULL OR tv_content_id IS NOT NULL)
);
"""

#: The columns `tv_binding` and `tv_binding_aside` share, in one order, for the
#: two copies between them. Spelled once, so a column added to the one is not
#: silently dropped by the other.
_SHARED_COLUMNS: Final[str] = (
    "id, artwork_id, tv_content_id, tv_thumb_md5, render_fingerprint, uploaded_at, "
    "last_named_feed_seconds, last_named_at, upload_status"
)

#: Every column `_binding` reads, spelled once. Two SELECTs want the same list,
#: and a list that drifts between them is a `KeyError` at the row factory rather
#: than anything a reader would spot.
_BINDING_COLUMNS: Final[str] = (
    "SELECT artwork_id, tv_content_id, tv_thumb_md5, render_fingerprint, uploaded_at, upload_status, "
    "last_named_feed_seconds, last_named_at FROM tv_binding"
)

#: Keys in `daemon_state`. Named rather than spelled at each call site, because a
#: typo in a key string is a silent read of nothing — which for the work key
#: means a restart that cannot tell which picture is already up.
_LAST_SELECTED_WORK_ID: Final[str] = "last_selected_work_id"
_SLIDESHOW_DISABLED: Final[str] = "native_slideshow_disabled"
#: How long this wall has run with a feed adopted, in seconds, as last saved.
_FEED_SECONDS: Final[str] = "feed_seconds"
#: The identity (`device.duid`) of the set `tv_binding`'s rows were made on.
#: Absent from a store written before it existed, and from one held in memory.
_TV_IDENTITY: Final[str] = "tv_identity"
#: The schema at which the store began recording its set's identity.
_IDENTITY_RECORDED_FROM: Final[int] = 4
#: The keys that are facts about one set, not carried to another (`belongs_to`).
_SET_FACTS: Final[tuple[str, ...]] = (_LAST_SELECTED_WORK_ID, _SLIDESHOW_DISABLED)


class DisplayState:
    """The device's store, opened once and held for the life of the process."""

    def __init__(self, path: Path, *, now: Callable[[], datetime] | None = None) -> None:
        # **The same clock the daemon compares against, or the wait it computes is
        # nonsense.** `uploaded_at` on a failed row is what the retry wait is
        # measured from, so a store stamping the real clock while the loop reads an
        # injected one gives a difference of years — which reads as "long enough"
        # or "not yet" depending only on which side of today the test sits.
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._path = path
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        # Foreign keys are not used here — the one reference that crosses to the server is by id
        # and by design has no constraint behind it — but the checks above are,
        # and they are on by default. What is switched on is WAL, so a reader
        # (nothing today; the heartbeat writer tomorrow) never blocks the loop.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._prepare()
        #: The work on the wall as last committed, **held here as well as on
        #: disk**, because the wall's heartbeat reads it on every way out of a
        #: pass, the one that ended on a store fault included: a read of the file
        #: there would raise the fault the heartbeat exists to report. This store
        #: is the file's sole writer, so the copy is the file's value; it moves
        #: only after a commit.
        self._last_selected: str | None = self._get(_LAST_SELECTED_WORK_ID)
        #: The wall's running time with a feed: counted here as the wall runs
        #: (`count_feed_time`) and written only by `save_feed_time`, so a pass
        #: costs the card no write for it.
        self._feed_seconds: float = self._saved_feed_seconds()
        #: The set the bindings were made on, held as the work on the wall is
        #: and for the same reason: it moves only after a commit.
        self._identity: str | None = self._get(_TV_IDENTITY)

    def _prepare(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StateSchemaTooNew(
                f"{self._path} was written by a Player at schema {version}; this one understands "
                f"{SCHEMA_VERSION}. Roll the deployment forward rather than letting an older reader write to it."
            )
        self._connection.executescript(_SCHEMA)
        self._widen(version)
        if version < SCHEMA_VERSION:
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._connection.commit()

    def _widen(self, version: int) -> None:
        """Bring a store written by an older version up to this one.

        **Additive only, and each step idempotent**, because the alternative on a
        device whose whole storage story is one SD card is a migration that can
        half-run. `CREATE TABLE IF NOT EXISTS` above already covers a store that
        predates a whole table; this covers one that predates a column.

        A store at 0 that already has the column is the ordinary case for a file
        this version created, so the column list is read rather than assumed —
        `ALTER TABLE ADD COLUMN` is not idempotent and raises on the second run.
        """
        if version >= SCHEMA_VERSION:
            return
        columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(tv_binding)")}
        if "render_fingerprint" not in columns:
            # Null on every existing row, which reads as "unknown" and forces one
            # re-upload per bound work on the first pass after the upgrade. That is
            # the right cost: the alternative is assuming the file has not changed
            # since a version that never looked, which is the defect this column
            # exists to close.
            self._connection.execute("ALTER TABLE tv_binding ADD COLUMN render_fingerprint TEXT")
        if "last_named_feed_seconds" not in columns:
            self._connection.execute("ALTER TABLE tv_binding ADD COLUMN last_named_feed_seconds REAL")
        if "last_named_at" not in columns:
            self._connection.execute("ALTER TABLE tv_binding ADD COLUMN last_named_at TEXT")
        # **A store below 4 has no identity, even one that holds the key**: a
        # store rolled back to an older Player and forward again may have had
        # bindings written to it on another set meanwhile, by a Player that
        # never looked. So it is met as a store from before the identity, and
        # takes the first one read. Keyed on the version, so it runs once, and
        # never on a store at 4, whose identity is its own.
        if version < _IDENTITY_RECORDED_FROM:
            self._connection.execute("DELETE FROM daemon_state WHERE key = ?", (_TV_IDENTITY,))
        # **Every binding with a content id is taken as this Player's upload**
        # on the set it was made on: the bindings in use under the store's
        # identity, and each set aside under its own. A store that knows no set
        # records its bindings when it adopts one (`belongs_to`). Idempotent, so
        # a migration cut short finishes on the next open.
        identity = self._get(_TV_IDENTITY)
        if identity is not None:
            self._carry_bindings_as_uploads(identity)
        self._connection.execute(
            "INSERT INTO tv_upload (id, tv_identity, marker, tv_content_id, sent_at) "
            "SELECT lower(hex(randomblob(16))), aside.tv_identity, NULL, aside.tv_content_id, aside.uploaded_at "
            "FROM tv_binding_aside AS aside WHERE aside.tv_content_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM tv_upload WHERE tv_upload.tv_identity = aside.tv_identity "
            "AND tv_upload.tv_content_id = aside.tv_content_id)"
        )
        # **Every row this version has not written is named now**, never read as
        # "never named": that reading would retire the household's whole library
        # from the set on the first day after the upgrade. So each binding gets
        # the full wait from the upgrade. Keyed on the null, so a migration cut
        # short after its columns were added finishes on the next open, and so a
        # row an older Player wrote after a roll back is named when the store is
        # rolled forward again.
        self._connection.execute(
            "UPDATE tv_binding SET last_named_feed_seconds = ?, last_named_at = ? WHERE last_named_feed_seconds IS NULL",
            (self._saved_feed_seconds(), self._now().isoformat()),
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- bindings ---------------------------------------------------------

    def binding_for(self, artwork_id: str) -> Binding | None:
        """What this device knows about one work's image, if anything."""
        with self._operation("binding_for", f"read the binding of {artwork_id}"):
            row = self._connection.execute(
                f"{_BINDING_COLUMNS} WHERE artwork_id = ?",
                (artwork_id,),
            ).fetchone()
        return _binding(row) if row is not None else None

    def bindings(self) -> tuple[Binding, ...]:
        """Every work this device has a record for, in no meaningful order."""
        with self._operation("bindings", "read the bindings"):
            rows = self._connection.execute(_BINDING_COLUMNS).fetchall()
        return tuple(_binding(row) for row in rows)

    @property
    def feed_seconds(self) -> float:
        """How long this wall has run with a feed adopted, as counted so far. Answered from memory."""
        return self._feed_seconds

    def count_feed_time(self, seconds: float) -> None:
        """Count `seconds` more of the wall running with a feed. Memory only, until `save_feed_time`."""
        self._feed_seconds += seconds

    def save_feed_time(self, *, named: Iterable[str], left: Mapping[str, float]) -> None:
        """Write the count, and when each work was last named, in one transaction.

        `named` are the works the adopted feed names now, so each was last named
        at the count. `left` are works that stopped being named, each with the
        count when it did; **a time is never moved earlier**, so a stale one can
        never make a work look longer unnamed than it is.
        """
        named = tuple(named)
        now = self._now().isoformat()
        with self._operation("save_feed_time", "record how long the wall has had a feed"):
            self._connection.execute(
                "INSERT INTO daemon_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_FEED_SECONDS, repr(self._feed_seconds)),
            )
            if named:
                self._connection.execute(
                    f"UPDATE tv_binding SET last_named_feed_seconds = ?, last_named_at = ? "  # noqa: S608 -- placeholders only
                    f"WHERE artwork_id IN ({', '.join('?' * len(named))})",
                    (self._feed_seconds, now, *named),
                )
            for artwork_id, at in left.items():
                self._connection.execute(
                    "UPDATE tv_binding SET last_named_feed_seconds = ?, last_named_at = ? "
                    "WHERE artwork_id = ? AND (last_named_feed_seconds IS NULL OR last_named_feed_seconds < ?)",
                    (at, now, artwork_id, at),
                )
            self._connection.commit()

    def retire(self, artwork_id: str, tv_content_id: str | None) -> bool:
        """Drop a work's binding, but only while it still names `tv_content_id`, and say whether it went.

        The image it named is already off the set, or never was there. Keyed on
        the id as well as the work, so a row that was rewritten since it was
        judged (a new upload under a new id) is never dropped with the image it
        did not name.
        """
        with self._operation("retire", f"drop the binding of {artwork_id}"):
            cursor = self._connection.execute(
                "DELETE FROM tv_binding WHERE artwork_id = ? AND tv_content_id IS ?",
                (artwork_id, tv_content_id),
            )
            self._connection.commit()
        return cursor.rowcount > 0

    # -- the set the bindings were made on ----------------------------------

    @property
    def tv_identity(self) -> str | None:
        """The set the bindings were made on; None for a store that has not recorded one. Answered from memory."""
        return self._identity

    def belongs_to(self, identity: str) -> SetMet:
        """Make the bindings the set `identity`'s, in one transaction, and say what that took.

        The same set: nothing is written. **A store that recorded no set takes
        this one**, keeping its bindings and its facts. **Another set**: every
        binding and the set's facts (`_SET_FACTS`) are set aside under the
        identity they were made on, the bindings set aside under `identity`
        come back, each named now so none is retired for a full wait, and
        `identity` is recorded. A fault raises `StoreFault` with nothing
        changed, so the bindings stay under the set they were made on.
        """
        previous = self._identity
        if previous == identity:
            return SetMet(SetChange.SAME, previous)
        with self._operation("belongs_to", "record which television its bindings belong to"):
            if previous is None:
                # A copy of this set's own set aside earlier is older than the
                # rows being taken, and must not come back over them later.
                self._connection.execute("DELETE FROM tv_binding_aside WHERE tv_identity = ?", (identity,))
                # The rows taken are this set's uploads, as a migration takes
                # the rows of a store that knew its set.
                self._carry_bindings_as_uploads(identity)
                self._put(_TV_IDENTITY, identity)
                self._connection.commit()
                met = SetMet(SetChange.ADOPTED)
            else:
                met = self._move_to(previous, identity)
        self._identity = identity
        if met.change is SetChange.CHANGED:
            self._last_selected = None
        return met

    def _move_to(self, previous: str, identity: str) -> SetMet:
        """Set `previous`'s bindings aside and bring back `identity`'s. Inside `belongs_to`'s operation."""
        execute = self._connection.execute
        set_aside = execute("SELECT COUNT(*) FROM tv_binding").fetchone()[0]
        execute("DELETE FROM tv_binding_aside WHERE tv_identity = ?", (previous,))
        execute(
            f"INSERT INTO tv_binding_aside (tv_identity, {_SHARED_COLUMNS}) "  # noqa: S608 -- column names only
            f"SELECT ?, {_SHARED_COLUMNS} FROM tv_binding",
            (previous,),
        )
        execute("DELETE FROM tv_binding")
        execute(
            f"INSERT INTO tv_binding ({_SHARED_COLUMNS}) "  # noqa: S608 -- column names only
            f"SELECT {_SHARED_COLUMNS} FROM tv_binding_aside WHERE tv_identity = ?",
            (identity,),
        )
        restored = execute("DELETE FROM tv_binding_aside WHERE tv_identity = ?", (identity,)).rowcount
        # **Named now**, as the migration names every row it meets: the feeds
        # this wall adopted while it was on the other set named their works on
        # no row here, so the time a row last saw one says nothing.
        execute(
            "UPDATE tv_binding SET last_named_feed_seconds = ?, last_named_at = ?",
            (self._feed_seconds, self._now().isoformat()),
        )
        execute(
            f"DELETE FROM daemon_state WHERE key IN ({', '.join('?' * len(_SET_FACTS))})",  # noqa: S608 -- placeholders only
            _SET_FACTS,
        )
        self._put(_TV_IDENTITY, identity)
        self._connection.commit()
        return SetMet(SetChange.CHANGED, previous, set_aside=set_aside, restored=restored)

    def _carry_bindings_as_uploads(self, identity: str) -> None:
        """Record each binding in use with a content id as an upload to `identity`, with no marker. In a caller's transaction."""
        self._connection.execute(
            "INSERT INTO tv_upload (id, tv_identity, marker, tv_content_id, sent_at) "
            "SELECT lower(hex(randomblob(16))), ?, NULL, binding.tv_content_id, binding.uploaded_at "
            "FROM tv_binding AS binding WHERE binding.tv_content_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM tv_upload WHERE tv_upload.tv_identity = ? AND tv_upload.tv_content_id = binding.tv_content_id)",
            (identity, identity),
        )

    def accounted_content_ids(self) -> frozenset[str]:
        """Every content id a binding in use names: the pictures the wall still wants on the set.

        **Orphaned rows are not in this set, and that is not an oversight.**
        `mark_orphaned` clears the content id along with the status — the row says
        "this work has no image on this television" — so there is nothing left to
        account for. If the set later lists that id again while its upload
        record stands (`uploads_on`), it is removed and the work is uploaded
        afresh, which converges on the right wall at the cost of one transfer.
        """
        with self._operation("accounted_content_ids", "read the content ids it accounts for"):
            rows = self._connection.execute("SELECT tv_content_id FROM tv_binding WHERE tv_content_id IS NOT NULL").fetchall()
        return frozenset(row["tv_content_id"] for row in rows)

    def record_upload(self, artwork_id: str, tv_content_id: str, *, render_fingerprint: str | None = None) -> Binding:
        """Record that the television is holding this work's image, under this id.

        **`tv_thumb_md5` is modelled and nothing writes it**, which is stated here
        rather than left to be discovered from a column of nulls. Arrt's `data-model.md`
        gives it as "used to re-match after the TV loses or renames content", and
        re-matching as built does not need it: a binding whose id the set stops
        listing is marked orphaned and uploaded again, which costs one transfer
        and is correct even when the set renamed nothing. Fetching a thumbnail per
        work to compare hashes would cost more than the re-upload it saves.
        The column stays because the entity is modelled; the keyword argument that
        no caller ever passed is gone, because a parameter nothing supplies reads
        as a capability rather than as a gap.
        """
        with self._operation("record_upload", f"record the upload of {artwork_id}"):
            return self._write_binding(artwork_id, tv_content_id, UploadStatus.UPLOADED, render_fingerprint)

    def record_upload_failure(self, artwork_id: str) -> Binding:
        """Record that an upload was attempted and did not land.

        Keeps the row rather than deleting it, so the next pass can tell a work
        that failed from one nobody has tried — and so a work that fails every
        pass is visible in the store rather than only in the journal.
        """
        with self._operation("record_upload_failure", f"record the failed upload of {artwork_id}"):
            return self._write_binding(artwork_id, None, UploadStatus.FAILED, None)

    def mark_orphaned(self, artwork_id: str) -> Binding:
        """Record that the television no longer lists this work's content id."""
        with self._operation("mark_orphaned", f"record {artwork_id} as orphaned"):
            return self._write_binding(artwork_id, None, UploadStatus.ORPHANED, None)

    def _write_binding(
        self,
        artwork_id: str,
        tv_content_id: str | None,
        status: UploadStatus,
        render_fingerprint: str | None,
    ) -> Binding:
        # **Every write names the work now**: the wall writes a binding only for
        # a picture it was handed, which the adopted feed names.
        uploaded_at = self._now()
        self._connection.execute(
            """
            INSERT INTO tv_binding (
                id, artwork_id, tv_content_id, render_fingerprint, uploaded_at, upload_status,
                last_named_feed_seconds, last_named_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artwork_id) DO UPDATE SET
                tv_content_id           = excluded.tv_content_id,
                render_fingerprint      = excluded.render_fingerprint,
                uploaded_at             = excluded.uploaded_at,
                upload_status           = excluded.upload_status,
                last_named_feed_seconds = excluded.last_named_feed_seconds,
                last_named_at           = excluded.last_named_at
            """,
            (
                str(uuid.uuid4()),
                artwork_id,
                tv_content_id,
                render_fingerprint,
                uploaded_at.isoformat(),
                str(status),
                self._feed_seconds,
                uploaded_at.isoformat(),
            ),
        )
        self._connection.commit()
        return Binding(
            artwork_id=artwork_id,
            tv_content_id=tv_content_id,
            upload_status=status,
            uploaded_at=uploaded_at,
            render_fingerprint=render_fingerprint,
            last_named_feed_seconds=self._feed_seconds,
            last_named_at=uploaded_at,
        )

    # -- what this Player sent to a set ------------------------------------

    def record_sending(
        self, identity: str | None, *, marker_for: Callable[[datetime], str], forgetting_the_wall: bool = False
    ) -> Sending:
        """Record an upload about to be sent to `identity` (None: a set that has not said), and return its marker.

        **Written before the upload is sent, and an upload whose record cannot
        be written is not sent**: a picture this Player put on a set with no
        record of it is one it can never take off again. The marker is
        `marker_for` the time now, moved on a second at a time past any marker a
        live record holds, so no two records are ever found by the same one.

        `forgetting_the_wall` also forgets, in the same transaction, which work
        this store last put on the wall: for an upload whose selection this
        store's bindings will not record, so that a Player started again
        afterwards puts one of its own up rather than believing it is there.
        """
        with self._operation("record_sending", "record an upload before it is sent"):
            held = {row["marker"] for row in self._connection.execute("SELECT marker FROM tv_upload WHERE marker IS NOT NULL")}
            at = self._now()
            marker = marker_for(at)
            while marker in held:
                at += timedelta(seconds=1)
                marker = marker_for(at)
            key = str(uuid.uuid4())
            self._connection.execute(
                "INSERT INTO tv_upload (id, tv_identity, marker, tv_content_id, sent_at) VALUES (?, ?, ?, NULL, ?)",
                (key, identity, marker, self._now().isoformat()),
            )
            if forgetting_the_wall:
                self._connection.execute("DELETE FROM daemon_state WHERE key = ?", (_LAST_SELECTED_WORK_ID,))
            self._connection.commit()
        if forgetting_the_wall:
            self._last_selected = None
        return Sending(key=key, marker=marker)

    def learned(self, key: str, tv_content_id: str) -> None:
        """Record the id the set gave the upload `key`."""
        with self._operation("learned", f"record the id {tv_content_id} of an upload"):
            self._connection.execute("UPDATE tv_upload SET tv_content_id = ? WHERE id = ?", (tv_content_id, key))
            self._connection.commit()

    def forget_sending(self, key: str) -> None:
        """Drop the record of an upload that was never sent (held, or no connection to send it on)."""
        with self._operation("forget_sending", "drop the record of an upload never sent"):
            self._connection.execute("DELETE FROM tv_upload WHERE id = ? AND tv_content_id IS NULL", (key,))
            self._connection.commit()

    def has_unplaced_uploads(self) -> bool:
        """Whether any upload is recorded as sent to a set that had not said which it was."""
        with self._operation("has_unplaced_uploads", "read whether any upload's television is unknown"):
            unplaced = self._connection.execute("SELECT 1 FROM tv_upload WHERE tv_identity IS NULL LIMIT 1").fetchone()
        return unplaced is not None

    def uploads_on(self, identity: str, listed: Mapping[str, str | None]) -> frozenset[str]:
        """Of the pictures the set `identity` lists (each id with its `image_date`), those this Player put there.

        One transaction, which also keeps the records to what may still be on
        a set. **A record of `identity`'s**: kept, and its picture named, while
        the listing shows its id with its marker (a record with no marker, by
        its id, and the date the listing shows is then kept as its marker); its
        id unknown, it takes the one listed picture carrying its marker. It
        leaves when the listing shows its id with another marker, or shows no
        id for it at all: the set no longer holds what it recorded. A listing
        that shows the id with no date cannot say, and the record is kept,
        naming nothing. **A record of no set's**: placed on `identity`, and its
        picture named, only when the listing shows its id and marker together,
        or, its id unknown, exactly one picture with its marker; otherwise it
        stays, for the set it went to may be another.
        """
        ours: set[str] = set()
        with self._operation("uploads_on", "match its uploads against the television's list"):
            rows = self._connection.execute(
                "SELECT id, tv_identity, marker, tv_content_id FROM tv_upload WHERE tv_identity = ? OR tv_identity IS NULL",
                (identity,),
            ).fetchall()
            for row in rows:
                if row["tv_content_id"] is not None:
                    self._match_by_id(row, identity, listed, ours)
            carrying: dict[str, list[str]] = {}
            for content_id, date in listed.items():
                if date is not None and content_id not in ours:
                    carrying.setdefault(date, []).append(content_id)
            for row in rows:
                if row["tv_content_id"] is None:
                    self._match_by_marker(row, identity, carrying, ours)
            self._connection.commit()
        return frozenset(ours)

    def _match_by_id(self, row: sqlite3.Row, identity: str, listed: Mapping[str, str | None], ours: set[str]) -> None:
        """`uploads_on` for a record whose id is known. Inside its transaction."""
        execute = self._connection.execute
        content_id, marker, placed = row["tv_content_id"], row["marker"], row["tv_identity"] == identity
        date = listed.get(content_id)
        if content_id not in listed or (marker is not None and date is not None and date != marker):
            # Gone from the set, or its id names another picture now.
            if placed:
                execute("DELETE FROM tv_upload WHERE id = ?", (row["id"],))
        elif marker is None:
            # Carried from a binding, which names the picture by its id alone.
            if placed:
                ours.add(content_id)
                if date is not None:
                    execute("UPDATE tv_upload SET marker = ? WHERE id = ?", (date, row["id"]))
        elif date == marker:
            ours.add(content_id)
            if not placed:
                execute("UPDATE tv_upload SET tv_identity = ? WHERE id = ?", (identity, row["id"]))

    def _match_by_marker(self, row: sqlite3.Row, identity: str, carrying: dict[str, list[str]], ours: set[str]) -> None:
        """`uploads_on` for a record whose id was never learned: the one listed picture with its marker. In its transaction."""
        found = carrying.get(row["marker"], [])
        if len(found) == 1:
            (content_id,) = found
            ours.add(content_id)
            del carrying[row["marker"]]
            self._connection.execute(
                "UPDATE tv_upload SET tv_identity = ?, tv_content_id = ? WHERE id = ?", (identity, content_id, row["id"])
            )
        elif row["tv_identity"] == identity:
            # Resolved by the first listing after it: it did not land, or it
            # cannot be told from a picture stamped in the same second.
            self._connection.execute("DELETE FROM tv_upload WHERE id = ?", (row["id"],))

    def forget_uploads(self, identity: str, tv_content_ids: Iterable[str]) -> None:
        """Drop the records of pictures the set `identity` has confirmed are gone from it."""
        tv_content_ids = tuple(tv_content_ids)
        if not tv_content_ids:
            return
        with self._operation("forget_uploads", "drop the records of pictures removed from the television"):
            self._connection.execute(
                f"DELETE FROM tv_upload WHERE tv_identity = ? AND tv_content_id IN ({', '.join('?' * len(tv_content_ids))})",  # noqa: S608 -- placeholders only
                (identity, *tv_content_ids),
            )
            self._connection.commit()

    # -- where the wall got to --------------------------------------------

    @property
    def last_selected_work_id(self) -> str | None:
        """The work last put on the wall, so a restart resumes rather than restarts.

        Answered from memory (see `__init__`), so it never raises a store fault.
        """
        return self._last_selected

    def set_last_selected_work_id(self, work_id: str) -> None:
        with self._operation("set_last_selected_work_id", f"record {work_id} as the work on the wall"):
            self._set(_LAST_SELECTED_WORK_ID, work_id)
        self._last_selected = work_id

    def forget_the_wall(self) -> None:
        """Record that no work of these bindings is known to be on the wall, so the next pass puts one up."""
        with self._operation("forget_the_wall", "forget which work is on the wall"):
            self._connection.execute("DELETE FROM daemon_state WHERE key = ?", (_LAST_SELECTED_WORK_ID,))
            self._connection.commit()
        self._last_selected = None

    @property
    def native_slideshow_disabled(self) -> bool:
        """Whether this device has already been told to stop its own slideshow.

        Persisted rather than held in memory, because `Restart=always` makes
        restarts routine and the call is only correct to make once — the
        television's own slideshow fighting host-driven `select_image` is the
        failure this switch prevents, and it does not come back on by itself.
        """
        with self._operation("native_slideshow_disabled", "read whether the set's own slideshow is off"):
            return self._get(_SLIDESHOW_DISABLED) == "1"

    def mark_native_slideshow_disabled(self) -> None:
        with self._operation("mark_native_slideshow_disabled", "record the set's own slideshow as off"):
            self._set(_SLIDESHOW_DISABLED, "1")

    @contextlib.contextmanager
    def _operation(self, operation: str, doing: str) -> Iterator[None]:
        """Run one operation of the wall's on the store: roll back what raised, and raise a fault of the file as one.

        **Rolled back whatever raised**, before anything is classified. A
        statement that failed inside the transaction it opened, or a commit
        refused, can leave that transaction open, and SQLite says it may or may
        not have rolled it back itself: this connection would then read the
        write back as kept, and the next commit that succeeded would publish it.
        A rollback that itself fails is not allowed to replace the fault being
        reported.
        """
        try:
            yield
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                self._connection.rollback()
            code = getattr(exc, "sqlite_errorcode", None)
            may_clear = _FAULTS.get(code & _PRIMARY) if isinstance(code, int) else None
            if may_clear is None:
                raise
            fault = getattr(exc, "sqlite_errorname", None) or f"SQLite result code {code}"
            raise StoreFault(operation, doing, exc, fault=fault, may_clear=may_clear) from exc

    def _saved_feed_seconds(self) -> float:
        """The count as last saved; nought for a store that never saved one.

        **A value that does not read as a number reads as nought**, which can
        only make a work look named more recently than it was: it delays a
        retirement, and never brings one forward.
        """
        saved = self._get(_FEED_SECONDS)
        try:
            return float(saved) if saved is not None else 0.0
        except ValueError:
            log.warning(
                "the display store's feed time %r is not a number; counting from nought",
                saved,
                extra={"event": "store.feed_time_unreadable"},
            )
            return 0.0

    def _get(self, key: str) -> str | None:
        row = self._connection.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def _set(self, key: str, value: str) -> None:
        self._put(key, value)
        self._connection.commit()

    def _put(self, key: str, value: str) -> None:
        """Write one key inside the transaction a caller commits."""
        self._connection.execute(
            "INSERT INTO daemon_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _binding(row: sqlite3.Row) -> Binding:
    return Binding(
        artwork_id=row["artwork_id"],
        tv_content_id=row["tv_content_id"],
        upload_status=UploadStatus(row["upload_status"]),
        uploaded_at=datetime.fromisoformat(row["uploaded_at"]),
        render_fingerprint=row["render_fingerprint"],
        tv_thumb_md5=row["tv_thumb_md5"],
        last_named_feed_seconds=row["last_named_feed_seconds"],
        last_named_at=datetime.fromisoformat(row["last_named_at"]) if row["last_named_at"] is not None else None,
    )
