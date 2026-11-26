---
artifact: data-model
version: 1
depends_on:
  - artifact: product-brief
last_validated: null
---

# Data Model — Arrt Player

**What this is.** What the Player itself stores and holds: the cache it pulls into,
the one database it writes, the settings it reads, and the states its code moves
through. Written 2026-10-27 from the code at `b82ba74`, and revised for audit waves 2 and 3
(2026-11-25, the code at `4f63234`). Every claim names the
file and symbol it was read from. Where a sentence says "only", "never" or
"exactly one", it says what enforces it.

**What is not here.** The server's entities (Wall, Client, Display, LabelOutput,
Theme, Artwork) are Arrt's: Arrt's `data-model.md` § Client, § Display,
§ LabelOutput, § Wall. The documents the Player reads and writes are the
contract's: `arrt_contract.schema(name)` and Arrt's `player-contract.md`. The label
rule (caption, card or blank, and the 30-minute hold) is Arrt's
`labels-and-surfaces.md` § What a label says, implemented in
`src/arrt_player/label_rule.py`.

**No § Direction here.** This file ratifies no norm. The norm index row
"Derived artifacts are regenerated, never transported" in
`project-preferences.md` lives in Arrt's `data-model.md` § Direction, which also
carries "Per-device runtime state never lives in the catalogue", the norm
`tv_binding` below exists for.

## Entities

The Player has no catalogue. It holds four kinds of thing: files in the cache,
one SQLite store per wall on the Frame, its settings, and in-memory state that
dies with the process.

### The cache (`CACHE_DIR`)

The only thing a wall draws from (`src/arrt_player/pull.py` module docstring;
Arrt's `nonfunctional-requirements.md` § Availability). Every path below is derived
from `CACHE_DIR` and a wall id, never configured
(`src/arrt_player/config.py:WallSettings`).

| Path | Written by | What it holds |
|---|---|---|
| `.client.json` | `pull.py:ClientPull.fetch` | The last `GET /client` answer that `client.py:parse_client_document` accepted, as the server's text. Read at start by `ClientPull.cached`, so a restart with the server down starts the walls it last knew (`client.py:Supervisor.run`). |
| `.client.etag` | `pull.py:ClientPull.fetch` | The ETag served with it. Removed before a new document is written, and written again only if the server served one, so it never sits beside a document it does not describe, even after a crash between the writes. **Sent only while `.client.json` exists and `parse_client_document` still accepts it** (`ClientPull._cached_etag`), so a torn or hand-edited document is replaced rather than vouched for with a 304, as the feed's ETag is. |
| `token_file` | the `samsungtvws` library, not the Player | The Frame's pairing token, at this path only when `TV_ADDRESS` is set and `TV_TOKEN_FILE` is not (`config.py:_frame`). See `security-model.md` § To the television: the pairing token. |
| `<wall id>/` | `__main__.py:run_frame_wall`, `run_screen_wall` (mkdir) | One wall. `config.py:ClientSettings.wall` refuses an id that is not one plain path component (`PLAIN_COMPONENT`, `WallIdUnusable`). A leading dot is refused, which keeps the two `.client.*` files apart from every wall directory; so is any other name the cache directory reserves, derived from the constants that name those files. A wall the client document stops placing here is stamped `<wall id>/.left.json` and its directory is removed a week later (below). |
| `<wall id>/manifest.json` | `pull.py:Pull._cache_feed` | The last feed that `manifest.py:parse` accepted, re-serialised (`json.dumps(document, indent=2)`), not the server's bytes. Written only after every work's media is cached or answered for, or after a master that keeps failing has been waited for `MEDIA_HOLD_SECONDS`, when it is written without that master (`Pull._hold_or_adopt`). Read by `manifest.py:Watcher`. |
| `<wall id>/manifest.etag` | `pull.py:Pull._cache_feed` | The ETag served with the feed. Removed before a new feed is written, and written again only if the server served one and every master is in hand: a feed cached after the hold ran out has none, so the next poll is answered whole and asks for the missing masters again. Sent only while `manifest.json` exists **and the watcher's own parser reads it** (`Pull._cached_etag`), so a torn or hand-edited manifest is replaced rather than vouched for with a 304. |
| `<wall id>/media/sha256-<hex>` | `pull.py:Pull._cache_media` | One presentation master, named by the SHA-256 of its bytes (`manifest.py:media_name`). Streamed to a temporary file beside it and renamed only when the SHA-256 of what was written equals the feed's `media.sha256` and the body did not run past the bytes the feed declares. After each adoption, `Pull._evict` removes every file that neither the new feed nor the one it replaced names. |
| `<wall id>/composed/<key>.jpg` | `compose.py:compose`, driven by `programmes/schedule.py:Schedule` | Each work drawn for this wall's display geometry and mat, named by `compose.py:composition_key`, flushed to disk before it takes its name. One found on disk that is not a whole JPEG (empty, cut short, zero-filled) is removed and composed again, each file read once per size and time (`Schedule._intact`, `compose.is_intact`). Pruned by `Schedule._tidy`. Never leaves the device (Arrt's `data-model.md` § Direction, "Derived artifacts are regenerated, never transported"). |
| `.labels/<label id>.json` | `pull.py:LabelPull` | The last label document the server sent for each label this client renders and this reader accepted. Its modification time is the instant the server last answered for it, moved forward at most once a minute (`LABEL_ANSWER_STAMP_SECONDS`). The ETag is not kept on disk, so a restart asks for the whole document, and **in memory it is held only for a document the cache took** (`LabelPull._keep`): asked with the ETag of a document it never cached, the server answers 304 and the document would never be cached. Read at start by `LabelPull.cached`; one that cannot be read is treated as absent. A file no mapped label would read (a retired label's, one moved to another Player's panel, an atomic write's leftover) is removed each time the client document is matched, and never before one has been read (`__main__.LabelCacheSweep`; `label.cache_forgotten`). A leading dot keeps it apart from every wall directory. |
| `<wall id>/display-heartbeat-<wall id>.json` | `heartbeat.py:write`, called from the wall loop | The wall's heartbeat (`heartbeat.py:Health.document`), rewritten about every 60 s (`heartbeat.py:INTERVAL_SECONDS`) and sooner on a display-state change (`wall.py:DisplayRecord`). `pull.py:Pull._heartbeats` POSTs it (`_forward_heartbeat`) when it changes. |
| `<wall id>/display-state.sqlite` (+ `-wal`, `-shm`) | `state/store.py:DisplayState` | Frame walls only (`config.py:WallSettings.state_path`, opened in `__main__.py:run_frame_wall`). See `DisplayState` below. |

**Every file the Player writes whole goes through `safe_write.py:write_whole`** (or
`write_whole_with` for the composed picture's encoder): the bytes are staged in a
temporary file beside the target, forced to the card with `fsync`, and renamed over
it, and the temporary file is gone on every way out, an interrupt included. A
reader therefore sees the old file or the new one, never part of one, and a failed
write leaves nothing behind. The writers are the pull's cache files
(`pull.py:_write_atomically`, staged as `.<name>.tmp`), the heartbeat
(`heartbeat.py:write`, `<name>.tmp`), a departed wall's stamp
(`client.py:WallCaches`), the settings file (`settings_ui/store.py`, `0600`
whatever the umask and whatever a crash left at the temporary name), the file
outputs' picture and record, the slideshow's saved listing, and a composed picture
(`compose.py`). **Each keeps its own temporary name**, because each directory's tidy
recognises its own. The two writers that stream a download as it arrives
(`pull.py:Pull._cache_media`, `sources/fetch.py`) stage their own, since their
bytes are not in hand when the file is opened. Held by
`tests/test_safe_writes.py`, whose scan of `src/` finds a hand-written
temp-and-rename anywhere but there.

**A wall's directory goes a week after the wall left this Player.** The client
document is read at every poll; a directory that is a wall's (one holding a file
only a wall's directory holds, under a name a wall id may take) and that no
document read places here is stamped `.left.json` (`left_at`, and `absent_seconds`
as this Player has seen it gone) and removed once `absent_seconds` reaches
`client.WALL_CACHE_KEPT_SECONDS` (7 days), its media, compositions and Frame
store with it. **The week is counted on the monotonic clock and only while
documents are being read**, at most ten minutes per sweep, so time the Player is
off or cannot read its document counts for nothing, a wall clock that jumps
expires nothing, and a restart loses at most the 15 minutes since the count was
last written (`tests/test_client.py::test_the_week_survives_a_restart`); a wall
placed here again first loses its stamp and nothing else.
Nothing is swept while no document has been read and parsed, because a Player
that cannot read it does not know which walls it has (`client.WallCaches`,
`Supervisor._read_document`). It removes only a real directory directly under
`CACHE_DIR`; a symlink, a dotted name, a plain file or an unknown directory is
never removed (`client.cache_entry_unknown` says the last once). Inside a wall
that is still running, pruning is `Pull._evict` for `media/` and
`Schedule._tidy` for `composed/`.

### DisplayState (`display-state.sqlite`)

The Frame's own memory of what it put on the television. One file per Frame
wall. `__main__.py:run_frame_wall` opens it once per worker and holds it for the
worker's life. `state/store.py:DisplayState` is its only reader and writer; no
other module opens the path (`config.py:STATE_FILENAME` is referenced only by
`WallSettings.state_path`). Losing it costs a re-upload of every work, and the
Player's record of what it put on the set: the pictures already there are then
nobody's it can name, so they stay on the set until a person removes them
(`state/store.py` module docstring).

Schema version is SQLite's `PRAGMA user_version`, currently 5
(`state/store.py:SCHEMA_VERSION`). Journal mode is WAL (`DisplayState.__init__`).

**The chain of migrations is one function, `DisplayState._widen`, run once on
opening a store below the current version, additive and idempotent** (so a start
cut short finishes on the next open): version 2 added `render_fingerprint`
(2026-08-06); 3 (2026-11-19) added `last_named_feed_seconds` and `last_named_at`,
and names every existing row as named at the upgrade, so nothing retires for 14
days; 4 (2026-11-19) added the table `tv_binding_aside` and the key `tv_identity`;
5 (2026-11-20) added the table `tv_upload` and takes every binding with a content
id as an upload of the set it was made on (below).
A store from any version opens through every step it lacks (held by
`tests/test_bindings.py::TestTheStoreKeepsEachSetsBindingsApart::test_a_schema_3_store_opens_with_every_binding_and_no_set`
and `::TestASetThatWillNotSayWhichItIs::test_a_schema_2_store_opens_through_both_migrations_and_sends_no_removal_until_the_set_says`).
**Opening a store below 4 forgets any `tv_identity` it holds**, because a Player
older than 4 may have written bindings to it on another set since (a rollback and
a roll forward again), so it is met as a store from before the identity and takes
the first identity read. A store above the code's version is refused
(`StateSchemaTooNew`); the way back across 3, 4 and 5 is the runbook's § Going back,
and across 5 it needs the owner's say-so each time (2026-11-25). **`tv_upload` gained
four columns (`sent_toward`, `address`, `spell`, `heard_from`) in place, with no
migration for them, because no release has written schema 5**: a store written by
a development build that had the table without them opens, and then that Frame
wall's pass fails on `no such column: address` every time it runs (its backoff
climbs to five minutes; the set and the other walls are untouched) until the store
is deleted. The Pi has never run such a build.

**The store holds the bindings of one television at a time, and the bindings of
every other this wall has met, set aside.** A binding is a content id, which is
the set's own; used on another set it names another picture or none
(`state/store.py` module docstring). So the store records which set the rows were
made on, and on meeting another it moves them aside (below).

#### `tv_binding` — one work's image on this television

`state/store.py:_SCHEMA`, read back as `state/store.py:Binding`.

| Column | Type | Constraints (as enforced by SQLite) | Description |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | A UUID4, minted on first insert. An upsert keeps the existing one (`_write_binding`'s `ON CONFLICT` does not set it). Nothing reads it. |
| `artwork_id` | TEXT | NOT NULL, UNIQUE | The work id from the feed. **At most one row per work in this file**, and because the file is per wall, at most one per (wall, work). |
| `tv_content_id` | TEXT | nullable; CHECK `upload_status <> 'uploaded' OR tv_content_id IS NOT NULL` | The set's own id for the uploaded picture. A cache key, meaningless off this set. Cleared to null by `record_upload_failure` and `mark_orphaned`. |
| `tv_thumb_md5` | TEXT | nullable | **Nothing writes it** (`DisplayState.record_upload` docstring). Null on every row. |
| `render_fingerprint` | TEXT | nullable | `"sha256:<hex>"` of the composed file's bytes when it was sent (`displays/frame.py:_fingerprint`; hashed once per change of the file's stamp). A mismatch, or null, counts as changed (`_render_changed`) and forces a re-upload; **the same pixels composed again are not a change**, which is what keeps a work that left the feed and came back from being uploaded twice. A row an earlier Player wrote holds `"<st_mtime_ns>:<st_size>"`, which is still accepted while the file has that stamp (`_stamp`), so the upgrade uploads nothing. Added at schema 2 by `DisplayState._widen`. |
| `last_named_feed_seconds` | REAL | nullable | The wall's `daemon_state.feed_seconds` when an adopted feed last named this work. **What retires a binding** (`displays/frame.py:_retirements`): 14 days (`RETIRED_AFTER_SECONDS`) of the wall running with a feed since. Null only on a row an older Player wrote after a rollback; the next migration names it, and a row with none is never retired. Added at schema 3. |
| `last_named_at` | TEXT | nullable | The wall time of the same moment, for a reader of the file. Nothing compares it. Added at schema 3. |
| `uploaded_at` | TEXT | NOT NULL | ISO 8601 from the injected clock (`DisplayState._now`), on every write including failures. A `failed` row's retry wait is measured from it (`displays/frame.py:FrameDisplay._too_soon_to_retry`). |
| `upload_status` | TEXT | NOT NULL; CHECK in (`uploaded`, `failed`, `orphaned`) | `state/store.py:UploadStatus`. No row means "never tried". |

`Binding.is_on_the_television` is `uploaded` with a content id. The CHECK makes
`uploaded` without an id unwritable, so that conjunction is belt and braces.

#### `daemon_state` — where the wall got to

`state/store.py:_SCHEMA`; key/value, both TEXT, key PRIMARY KEY. Read only by
name.

| Key | Accessor | Meaning |
|---|---|---|
| `last_selected_work_id` | `DisplayState.last_selected_work_id` | The work last put on the wall, so a restart resumes instead of re-selecting. The Frame's implementation of `programmes/schedule.py:Memory`. A screen wall uses `schedule.py:InMemory` instead and remembers nothing across a restart (`displays/screen.py:screen_wall`). |
| `native_slideshow_disabled` | `DisplayState.native_slideshow_disabled` | `"1"` once the set's own slideshow has been switched off. Written by `displays/frame.py:FrameDisplay._connected`, which calls `disable_native_slideshow` only while it is unset. |
| `tv_identity` | `DisplayState.tv_identity`, `belongs_to` | The `device.duid` of the set `tv_binding`'s rows were made on. **Absent from a store written before schema 4, and from the store held in memory** (`GoingOn.scratch`); a store without it sends no removal (`security-model.md` § What it does to the television). |
| `feed_seconds` | `DisplayState.feed_seconds`, `save_feed_time` | How long this wall has run with a feed adopted, counted from the elapsed clock while the Player runs and written at most hourly (`FEED_TIME_SAVED_EVERY_SECONDS`). Never the calendar, so a month powered off or a clock step retires nothing. A value that is not a number reads as nought, which delays a retirement and never brings one forward (`store.feed_time_unreadable`). |

A key no code reads is left in place (`state/store.py` module docstring).

#### `tv_binding_aside` — the bindings of a set the wall has been moved away from

`state/store.py:_SCHEMA`. Every column of `tv_binding`, as it was, plus
`tv_identity` (the set they were made on), with `PRIMARY KEY (tv_identity, artwork_id)`.
Read only when that set is met again; never selected from, removed from or
accounted for meanwhile.

**What is and is not carried across a change of set** (`DisplayState.belongs_to`,
`_move_to`, one transaction, so a fault or a power cut leaves the rows under the
set they were made on; held by
`TestTheStoreKeepsEachSetsBindingsApart::test_a_fault_while_the_bindings_move_leaves_them_under_their_set`):

| | On meeting another set |
|---|---|
| `tv_binding` rows | Set aside under the old identity. The new set gets its own back if it was met before, **each named now**, so none retires for a full wait and nothing is uploaded again; else it starts with none, as a first run |
| `last_selected_work_id`, `native_slideshow_disabled` | Set aside with them, **not carried**: which work is on the wall, and whether its own slideshow is off, are facts about a set |
| `feed_seconds` | **Survives.** It counts the wall's running time with a feed, not a set's |
| `tv_identity` | Becomes the new set's |

A store with no identity recorded takes the first one read and keeps everything
(`SetChange.ADOPTED`; for a Player whose set has not changed that is true, and
the alternative re-uploads every wall's whole library on the first start after the
upgrade); its bindings become that set's uploads (`tv_upload`, below).
**If a Frame's address was moved to a different television before this
upgrade, nothing on the Pi records which set the old rows were made on**, so such
a store takes the new set's identity for the old set's, and its old ids are
taken as pictures this Player put on the new set, matched there by id alone: one
the household's own that happens to carry one of them would be removed. The
runbook has the operator delete that wall's `display-state.sqlite` first.

#### `tv_upload` — what this Player put on a set

`state/store.py:_SCHEMA`. **One row for every picture this Player sent to a set,
written before it is sent**, so that the Player removes from a set only what a row
names there (`displays/frame.py:FrameDisplay._remove_orphans`). The set's one upload
category (`MY-C0002`) also holds the household's pictures, and nothing in its
listing says who sent a picture; an older Player's, which recorded nothing, look
the same.

| Column | Type | Description |
|---|---|---|
| `id` | TEXT PRIMARY KEY | The record's own key, a UUID4 (`record_sending`); a row carried from a binding gets a random hex key. |
| `tv_identity` | TEXT | The set the picture went to. **Null while the set would not say which it is** (a spell of the store held in memory); then the columns below say which sets it may be on. |
| `marker` | TEXT | The `image_date` the upload carried, to the second, in the set's local time (`tv/client.py:upload_marker`), moved on a second at a time past any marker a live record holds, so no two live records share one. Null only on a row carried from a binding an older store wrote, which names its picture by id alone. |
| `tv_content_id` | TEXT | The id the set gave it; null until learned (`learned`), and for an upload whose id was never learned, resolved by the first listing after it. |
| `sent_at` | TEXT NOT NULL | ISO 8601 from the store's clock. |
| `sent_toward` | TEXT | For a row of no set: the set this store last recorded when it was sent. |
| `address` | TEXT | The address it was sent to (`TV_ADDRESS`); read for a row of no set, which is found only on a set listed at it. |
| `spell` | TEXT | For a row of no set: the connection it was sent on (one stretch of the store held in memory). A new spell begins, with the same store in memory, at any failure that may have taken the connection without ending the pass (`FrameDisplay._the_connection_may_have_gone`), so a set heard from on a later connection is never this row's. |
| `heard_from` | TEXT | For a row of no set: the set that said which it is on that connection (`heard_from`). |

CHECK: a row names its picture by `marker` or by `tv_content_id`, never neither.

**A picture the set lists is this Player's when a row of its set names it**
(`DisplayState.uploads_on`): its id, with the date the listing shows equal to the
marker; or, for a row whose id was never learned, the one listed picture carrying
its marker (none, or several, and the row leaves). A listing that shows the id with
another date means the id names another picture now, and the row leaves; one that
shows no date for it cannot say, and the row stays, naming nothing. **A row of no
set is matched only on the sets it may be on**, listed at its address: the set the
store last recorded when it was sent (`sent_toward`), and the set that said which
it is on the connection it went on (`heard_from`). Found there, it becomes that
set's; listed without it, it is no longer sent toward or heard from that set; and
it leaves once no set is left that it may be on. **It is never found on any other
set, whatever date or id a picture there carries**, which is what stops a
same-second household picture on a set the upload never went to from being taken
for it. A row leaves too when its removal is confirmed (`forget_uploads`) or its
upload was never sent (`forget_sending`: held for a programme, or no connection).

**An upload whose row cannot be written is not sent** (`StoreFault` ends the
pass, `FrameDisplay._record_sending`): a picture with no row is one the Player
could never take off again. Three store writes now stand where one stood: the row,
its id, and the binding.

**What it carries from before** (`DisplayState._widen`, `belongs_to`): every binding
with a content id, the ones in use under the store's set and the ones set aside
under each other set, becomes a row with no marker under its set; a store that knows
no set records its bindings as it adopts one. Idempotent. **What it leaves out, by
the owner's ruling (2026-11-19, accepted 2026-11-25):** an older Player's copies it
never recorded (a failed upload whose read-back failed, one made while its store
could not write, the theme a set that would not say received per outage, every
picture under a store the operator deleted) and the pictures of an earlier
generation of this product stay on the set, which the journal says once per set
(`tv.others_left_alone`) and the household can delete there.

### Settings

Resolved once at start by `config.py:load` into `config.py:ClientSettings`, from
the process environment over `.env` (`load_dotenv()` does not override an
exported variable). `.env.example` documents every key. The unit loads
`/opt/arrt-player/.env` through `EnvironmentFile=` (`deploy/display.service`).

| Group | Keys | Where | Notes |
|---|---|---|---|
| Client | `SERVER_URL`, `CLIENT_TOKEN`, `CACHE_DIR` | `ClientSettings` | All three required (`_require`). `CACHE_DIR` must be absolute after `~` expansion (`_absolute`). `SERVER_URL` loses a trailing slash and is otherwise unchecked. |
| Polling | `MANIFEST_POLL_SECONDS` | `ClientSettings.poll_interval_seconds` | Default 1.0. The client poll (30 s) and label poll (1 s) are constants (`DEFAULT_CLIENT_POLL_SECONDS`, `DEFAULT_LABEL_POLL_SECONDS`), not settings. |
| Mat | `MAT_WIDTH_INCHES`, `MAT_BOTTOM_WEIGHT` | `ClientSettings` → `WallSettings` | Must be above zero (`_positive_float`). |
| Frame | `TV_ADDRESS` (also the settings page's: `.env` wins, an empty value there pins nothing; an IPv4 address or a host name, no scheme and no port, `config.tv_address_problem`) and, only when it is set, `TV_PORT`, `TV_TOKEN_FILE`, `TV_CLIENT_NAME`, `LATITUDE`, `LONGITUDE`, `LOCATION_NAME`, `LOCATION_REGION`, `TV_MIN/MAX_BRIGHTNESS`, `BRIGHTNESS_INTERVAL_SECONDS`, `TV_UPLOAD_TIMEOUT_SECONDS`, `TV_UPLOAD_RETRY_SECONDS`, `TV_SELECT_CONFIRM_SECONDS`, `TV_CONNECT_TIMEOUT_SECONDS`, `TV_RETRY_MIN/MAX_SECONDS`, `TV_PANEL_WIDTH_PX`, `TV_PANEL_HEIGHT_PX`, `TV_PANEL_DIAGONAL_INCHES` | `FrameSettings`, or None (`_frame`) | `LATITUDE`, `LONGITUDE`, `LOCATION_NAME` required once `TV_ADDRESS` is set. `TV_TOKEN_FILE` defaults to `CACHE_DIR/token_file` and must be absolute when set. `TV_CLIENT_NAME` defaults to `tvpi` (`DEFAULT_TV_CLIENT_NAME`); changing it costs a new pairing. Panel sizes must be above zero. |
| Screen brightness | `SCREEN_BRIGHTNESS` (`on`/`off`, default on), `LATITUDE`, `LONGITUDE`, `LOCATION_NAME`, `LOCATION_REGION` (shared with the Frame, one parser), `SCREEN_MIN_BRIGHTNESS`, `SCREEN_MAX_BRIGHTNESS`, `BRIGHTNESS_INTERVAL_SECONDS` (the Frame's key, read the same way) | `ScreenBrightnessSettings` on `ClientSettings` (`_screen_brightness`, `_location`) | Off (`location` None) when none of the three location keys is set; any one set requires all three. Off too with `SCREEN_BRIGHTNESS=off`. Levels default 5 and 50 (owner, 2026-11-13), on the monitor's own scale. Checked only when on, and then refused unless 0 ≤ min ≤ max. |
| Label panel | `EPD_DEVICE`, `EPD_PANEL_WIDTH_PX`, `EPD_PANEL_HEIGHT_PX`, `EPD_PANEL_DIAGONAL_INCHES`, `EPD_VIEWING_DISTANCE_INCHES`, `EPD_MARGIN_PX`, `EPD_ROTATE_DEGREES` | `PanelSettings` (`_panel`) | Read whether or not `TV_ADDRESS` is set. Empty `EPD_DEVICE` is a client with no panel. **One panel is this configuration's limit by the owner's ruling of 2026-11-19, not the design's**: a value naming more than one is refused at start naming each (`config._panel`, `EPD_DEVICE_LIMIT`), and everything past `config` holds panels by output name (`__main__.label_panels`), so a Player with several, or with labels and no display, stays describable. Diagonal and distance have no default; unset, the panel draws nothing and says why (`__main__.py:label_surface`). Rotation 0 or 180 is enforced in `panel/epaper.py`, not here. |
| Drawing to files | `FILE_OUTPUT_DIR`, `FILE_OUTPUT_SCREEN`, `FILE_OUTPUT_LABEL` | `FileOutputSettings` on `ClientSettings.files` (`config._files`) | Off unless `FILE_OUTPUT_DIR` is set; the other two are refused without it. A Player that draws to files drives no hardware: `TV_ADDRESS` or `EPD_DEVICE` beside it, a directory in or around `CACHE_DIR`, and slideshow mode are each refused by name. Reports one screen output, `file-screen-0`, and with the label switch one label output, `file-label-0` (`displays/file_output.py`). |
| Retired | `WALL_ID`, `WALL_TOKEN`, `MANIFEST_SOURCE` | `RETIRED_SETTINGS` | Any of them set stops the start with a `ConfigError` naming its replacement. |

A typed value that does not parse is refused, never defaulted (`_int`, `_float`).
A number that is `nan` or infinite is refused, and so is any `*_SECONDS` interval or
timeout at zero or below (`_seconds`): a zero is a loop that never waits, not "as
fast as possible". `LATITUDE` and `LONGITUDE` must lie in range. `UI_ALLOWED_HOSTS`
(`UiSettings.allowed_hosts`) lists the names the settings page answers to beyond
its own; its first, written order, is the address the client heartbeat reports for
the page (`client.py:settings_url`: else the address the page is bound to, else the
address the pull reached the server from, and none when the page is bound to
loopback).
A `ConfigError` exits with status 2 and one line on stderr (`__main__.py:main`).

`WallSettings` and `Settings` (a Frame wall: `WallSettings` plus `FrameSettings`)
are derived per wall by `ClientSettings.wall` and `ClientSettings.frame_wall`.

### In-memory state

Lost on restart by design; each is rebuilt from the cache or the server.

| Holder | What | Source |
|---|---|---|
| `client.py:Supervisor` | Running wall workers keyed by wall id (`_running`), label renderers keyed by label output name (`_labels`), workers still stopping (`_stopping`), the last client document and the outputs the walls were matched against (`_document`, `_reconciled_outputs`), the report last taken (outputs, label outputs and settings address, `_reported`) and the instant it was made (`_accepted_at`, which `heard` gives the settings page with the link's last answer) | `client.py:Supervisor.reconcile`, `_reconcile_labels`, `_report_outputs`, `heard` |
| `client.py:FrameIdentity` | The Frame's `device.duid` for the client heartbeat: read again once per heartbeat interval (one REST GET a minute, no art channel), kept through a failed read, and said once when it changes (`client.identity_changed`), so another set answering at the Frame's address is reported within an interval, without a restart. Separate from the wall's own read on every new connection (`FrameDisplay._know_the_set`), which decides what the wall may do | `tv/samsung.py:SamsungTv.read_identity` |
| `manifest.py:Watcher` | The last good `Feed` and the file stamp `(st_mtime_ns, st_size)` last read | `Watcher.poll` |
| `pull.py:ClientPull` | The last client heartbeat the server refused with its error body and when (`_held`: not sent again until it changes, or for 300 s), the server's last answer to a report (`ReportAnswer`, `last_answer`: accepted, unreadable, token refused, or not taken) and whether a refusal as unreadable stands until a report is taken, and this end's address on the last connection to the server (`local_address`, noted as the connection is handed out, which the settings address falls back on) | `ClientPull.report`, `_AddressNoting` |
| `pull.py:LabelPull` | The label document's ETag; not persisted, so a restarted renderer fetches the whole document. The document itself is cached (`.labels/`) | `LabelPull.fetch` |
| `label_renderer.py:LabelRenderer` | The last accepted label document (the cached one until the server answers), the drawing on the panel, and the monotonic time of the server's last answer, which the offline hold runs from | `LabelRenderer.tick`, `_seed` |
| `displays/frame.py:FrameDisplay` | `_showing_art`, `_wall_content_id`, the set's last announcement, retry ladders, whether reconciliation is owed, and **where the wall stands with the set it reaches, one value of four** (`_phase`: `NotYetKnown`, `Identified` with the set's identity, `Withheld` since an instant, or `GoingOn` from a store held in memory that starts empty, after 15 minutes of a set that will not say), changed only by `_become`; the pictures this process has said it leaves alone, per set (`_left_alone_said`) | `FrameDisplay.__init__`, `_know_the_set`, `_become` |
| `client.py:WallCaches` | Each departed wall's stamp as counted since the Player started (written to the wall's `.left.json` at most every 15 minutes) | `WallCaches.sweep` |

## Relationships

- **A client has 0..n walls running, each on one output.** The server assigns
  them (`GET /client`). `client.py:Supervisor.reconcile` runs at most one worker
  per output, enforced by the `claimed` map in that method; a second wall on the
  same output is logged as unplaceable and not started.
- **A client has at most one Frame** (`config.py:FRAME_OUTPUT`, one
  `FrameSettings`), and **a label is an output a Player has zero or more of, as
  a display is** (the owner's ruling of 2026-11-19). The configuration describes
  at most one panel today (`config.py:PANEL_OUTPUT`, `epd-0`; a value naming
  several is refused), and everything past it holds panels by output name
  (`__main__.py:label_panels`, `Supervisor._labels`), so several, or labels and
  no display, can be added without changing what holds, reports or draws on
  them. HDMI outputs are whatever `client.py:hdmi_outputs` finds under
  `/sys/class/drm`, or, with `FILE_OUTPUT_DIR`, the one file screen alone
  (`__main__.py:player_outputs`).
- **A label output has at most one renderer**, keyed on its name in
  `Supervisor._labels`, drawing only on the panel its label names
  (`__main__.py:run_label`; a label naming an output with no panel is said once,
  `label.no_panel`, and not drawn). The wall it captions need not be one this
  client shows.
- **A wall has one cache directory, one `Pull`, one `Watcher`, one heartbeat
  file**, and on the Frame **one `display-state.sqlite`**. All derived from the
  wall id by `WallSettings`; no wall reads another's directory.
- **A `tv_binding` row references a feed work by `artwork_id`**, with no foreign
  key: the work exists only in the feed, which is a file. A binding for a work
  the feed no longer names is kept, not deleted, for 14 days of the wall running
  with a feed; its content id stays in `accounted_content_ids` and is therefore
  not removed from the set as an orphan. After that the binding is **retired**:
  its image is removed from the set through the gated removal when an upload record of
  that set names it there, the set's own list confirms it, and the row goes
  (`displays/frame.py:_retire`); a picture no record names stays on the set. Never retired:
  a work the adopted feed names (its schedule's whole horizon, scene and
  staging), the work last put up, the picture the set says is on the wall, a work
  whose image the set kept when asked (not asked again until the next feed), and a
  row whose last naming is unknown.
- **A `tv_binding` row belongs to one television**, the one `daemon_state.tv_identity`
  names; the rows of any other the wall has met are in `tv_binding_aside`.
- **A `tv_upload` row belongs to one television, or to none yet**: the set it was sent to,
  or, when the set would not say, a record that waits for a set it may be on (above).
  Of the pictures a set lists, the Player removes only those a row names there.
- **The Player's ids are the server's.** Wall id, client id, label id, work id
  and media hash all come from server documents. The Player mints only
  `tv_binding.id`.

## State Machines

Each machine below is one the code holds in a type or a field, not inferred.

### Wall worker lifecycle (`client.py:Supervisor`)

- *absent* → **running**: the document names the wall on an output this client
  has, not claimed by an earlier wall in the same document, with a usable id
  (`Supervisor._start`). A new worker waits for every worker still stopping
  (`after=stopping`), so two never hold one directory or one art channel.
- **running** → **restarting**: the worker raised, or returned without being
  asked (`Supervisor._keep_running`). Wait from 5 s doubling to 300 s
  (`RESTART_MIN_SECONDS`, `RESTART_MAX_SECONDS`); a run longer than the ceiling
  resets the ladder. Then **running** again.
- **running** → **stopping** → *absent*: the wall left the document, or moved to
  another output (`Supervisor._stop`). A move is a stop and a fresh start. The stop
  is **retired** when the document no longer places the wall on the output its
  worker runs on (`client.WorkerStop.retire`), and an HDMI wall then draws its
  screen black on the way out (`displays/screen.go_dark`; a file screen records a blank); it is merely **stopped**
  when the Player is stopping, when the output has left this client, or when the
  document still places the wall there and another wall is drawn on it, and the
  screen is left as it is. A wall retired while its worker waits to restart is
  called once more, already stopped, to leave (`_keep_running`).
- The walls are matched to the last document again whenever this client's
  outputs differ from the ones they were last matched against, so a connector
  that appears or goes is acted on although the server answers 304.
- *absent* → **unplaceable**: no such output, output already claimed, or
  `WallIdUnusable`. Logged once per (wall, output) (`_unplaceable`).
- Any → **stopping**: SIGTERM or SIGINT (`__main__.py:_run`); `Supervisor.stop_all`
  waits for every worker to close what it holds.

A dead pull ends its wall's worker so the supervisor restarts both
(`__main__.py:_beside_its_pull`, `_pull_ended`).

### Label renderer lifecycle (`client.py:Supervisor._reconcile_labels`)

Same as a wall worker, and told it is retired the same way (`client.WorkerStop`,
set before the event, once true it stays true), with one extra exit: **retired**
when the label output is no longer mapped, so the renderer blanks the panel
(`_stop_label(retired=True)`). A change of mapping is stopped (not retired) and
restarted. Shutdown stops without retiring, so e-paper keeps its last caption. A
renderer retired while it waits to restart is called once more, already stopped
and retired, to blank the panel, and that call starts nothing and fetches nothing
(`label.renderer_retired`). The label's cached document goes with its mapping: see
`.labels/` above.

### Pull cycle (`pull.py:Pull.cycle`, `pull.py:_Poll`)

One poll ends in one of four outcomes:

| Outcome | When | Next |
|---|---|---|
| `ANSWERED` | 200 adopted or refused as unreadable (a feed past `MANIFEST_MAX_BYTES`, 16 MiB, or one that stages a work twice included), 304, or a 4xx refusal | Backoff reset to 2 s; wait `poll_interval_seconds`. |
| `MEDIA_FAILING` | A master returned a non-200, non-404 status or a transport error | Manifest not written until `MEDIA_HOLD_SECONDS` have passed since the first failure of the episode, then written without the missing masters and with no ETag; backoff doubles. |
| `UNREACHABLE` | Transport error, timeout or 5xx on the manifest | No heartbeat; backoff 2 s doubling to 60 s (`BACKOFF_START_SECONDS`, `BACKOFF_MAX_SECONDS`). |
| `UNWRITABLE` | The route answered and the cache refused what it sent (a full or read-only disk: `ENOSPC`, `EROFS`) | The feed already cached stays where the watcher reads it, and **no ETag is stored for the feed that could not be written**, so the next poll is answered whole and tries again; backoff as for `UNREACHABLE`, but the server is heard, so the heartbeat still goes. Said once per episode, `pull.cache_unwritable`, then `pull.cache_writable` (`Pull._cache_feed`). |

**The ETag is written last** (`Pull._cache_feed`): the old one is removed before the
manifest it describes is replaced, and the new one goes in only after the
manifest, the media directory and the eviction have all succeeded, so a crash or
a refused write anywhere before the end leaves none, which costs one full
download. The same order holds for the client document (`ClientPull.fetch`).

The heartbeat is forwarded by a loop of its own (`Pull._heartbeats`) at the poll
interval, whenever the last manifest poll was answered, whatever the manifest
poll's backoff is doing. `Pull.run`'s two loops and `Pull.cycle` run the same two
bodies, `_manifest_turn` and `_heartbeat_turn`.

A master that answers 404, fails its hash, runs past the bytes the feed declares,
or has no declared size is skipped, not retried this feed (`Pull._cache_media`,
`_skip_once`), and the feed is still adopted.

### Manifest adoption (`manifest.py:Watcher.poll`)

- **none loaded** → **loaded**: a file whose stamp changed parses
  (`manifest.parse`).
- **loaded** → **loaded (new)**: same, with a new stamp.
- Absent, unstatable, not UTF-8, unreadable, wrong major
  (`ManifestVersionUnsupported`) or malformed (`ManifestUnreadable`): stay where
  you are, keep `current`. Each is logged once per file stamp or episode.

Only major 2 is read (`manifest.py:SUPPORTED_SCHEMA_MAJORS`).

### Upload status (`state/store.py:UploadStatus`)

Per work, on the Frame.

- *no row* → `uploaded`: upload confirmed (`FrameDisplay._upload` →
  `record_upload`).
- *no row* → `failed`: `TvUploadFailed` (`record_upload_failure`).
- `failed` → `uploaded` | `failed`: retried no sooner than
  `TV_UPLOAD_RETRY_SECONDS` after `uploaded_at` (`_too_soon_to_retry`), measured
  in wall time so it survives restarts.
- `uploaded` → `orphaned`: the set stops listing the content id
  (`_reconcile_with_the_set`), or refuses to select it and does not list it
  (`_rebind_or_reraise`).
- `orphaned` → `uploaded` | `failed`: the next upload of that work.
- `uploaded` → `uploaded`: re-upload when the composed file's fingerprint
  changed (`_is_current`).
- *any* → *no row*: **retirement**, the one transition that deletes a row.
  Fourteen days of the wall running with a feed that does not name the work, the
  image removed from the set (when an upload record of that set names it; otherwise
  the row goes and the picture stays) and confirmed absent by its own list, then
  `DisplayState.retire` (keyed on the content id too, so a row rewritten since is
  never dropped with an image it did not name).
- *any* → set aside: the wall met another set (below). The rows keep their
  status; they are not read until that set is met again.

Nothing else deletes a row.

### Which set the bindings belong to (`state/store.py:SetChange`, `displays/frame.py:_know_the_set`)

Read on every new connection, before any binding is used; asked again on the
wall's own ladder when the set will not say.

- **same** → nothing is written.
- **adopted**: the store recorded no set (written before schema 4, or its identity
  forgotten on opening below 4), so it takes this one and keeps its rows
  (`binding.set_adopted`). It removes nothing until it has one recorded.
- **changed**: another set. Rows and the set's facts moved aside, the new set's
  own brought back, one transaction (`binding.set_changed`, with how many of each);
  the wall then forgets what it learned of the old set (the work on the wall, its
  brightness, which works the set kept) and owes a reconciliation, as on a first
  run.
- **unidentified**: the read failed. The pass ends, said once (`tv.unidentified`),
  and no binding is used (`Withheld`). After `IDENTITY_PATIENCE_SECONDS` (15 minutes)
  the wall shows art from a store held in memory that starts empty and removes
  nothing (`tv.unidentified_going_on`, `GoingOn`), and leaves it for the device's
  store as soon as the set says (`tv.identified`). The pictures the memory store
  put up were recorded on the device's store with no set, and come off the set
  that says only if it is one they were sent toward or heard from (`tv_upload`,
  above); on any other set they stay.

The wall is always in one of four phases, `NotYetKnown` (a fresh process, and every
connection after one went), `Identified`, `Withheld` and `GoingOn`
(`displays/frame.py:SetPhase`), and **a removal is sent only in `Identified`**
(`_may_remove`). What a pass may do in each: in `NotYetKnown` it reads the
set's identity before anything else; in `Withheld` it ends at that read and sends
nothing on the art channel; in `GoingOn` it reads, uploads and selects from the store
in memory and deletes nothing; in `Identified` it does everything, and never asks
for the picture on the wall before another has been selected.

### What the server last said to the client heartbeat (`client.py:ReportAnswer`, `pull.py:ClientPull`)

The settings page reads the Player's state from this and the last report taken
(`Supervisor.heard`, `HeardReport`). **Accepted** (`204`); **unreadable** (a `400`
carrying the server's error body: the server says so of this Player until it takes a
report, whatever it answers between); **token refused** (`401`: the server cannot say
which Player sent it); **not taken** (no answer, a `5xx`, a `408`, a `429`, a refusal
for size, an answer not in the server's form). A report refused with the error body
(any `4xx` but `401`, `408`, `429`) is held, not sent again until it changes or
`REFUSED_REPORT_RESEND_SECONDS` (300) have passed; a changed report, the instant it
was made aside, is sent at once, and so is one that carries a new settings address.

### Display state (`heartbeat.py:ScreenState`, held in `wall.py:DisplayRecord`)

The five states a controller reports; `unassigned` and `silent` are the
server's. `DisplayReport.since` is when the state was entered, not last seen.

**Frame** (`displays/frame.py:FrameDisplay`):
- Starts `unreachable` (`FrameDisplay.__init__`).
- Any → `unreachable`: the set cannot be reached (`went_away`).
- Any → `showing_art` (work or null): `get_artmode` says on
  (`_read_art_mode`), a selection is confirmed (`show`), or the set announces a
  shown picture (`_take_the_sets_news`).
- Any → `dark`: `get_artmode` says off and `PowerState` reads `standby`
  (`_screen_is_not_ours`).
- Any → `in_use`: `get_artmode` says off and `PowerState` reads anything else or
  cannot be read.

**Screen** (`displays/screen.py:ScreenDisplay._observe_the_screen`), every pass:
`no_screen` when the kernel no longer lists the connector; `dark` when nothing is
connected or nothing of this wall's has been drawn; else `showing_art`. It never
reports `unreachable` or `in_use`.

### Selection outcome (`wall.py:Shown`)

Per attempt: `YES`; `SKIP` (this work cannot be shown, try the next);
`WALL_UNCHANGED` (the display took the request and changed nothing; end the
pass and back off).

## Constraints

What the code enforces, and by what.

| Constraint | Enforced by |
|---|---|
| A wall id names one plain directory under `CACHE_DIR`, and no name the cache reserves | `config.py:PLAIN_COMPONENT` in `ClientSettings.wall` |
| `CACHE_DIR` and `TV_TOKEN_FILE` are absolute | `config.py:_absolute` |
| A cached master's bytes match its name | `pull.py:Pull._cache_media` (SHA-256 of the streamed bytes before the rename) |
| A file under a composed picture's final name is a whole JPEG | `programmes/schedule.py:Schedule._intact` (a damaged one is removed), `compose.py:is_intact` |
| `manifest.json` is always a document `manifest.parse` accepts | `pull.py:Pull._pull_manifest` parses before `_adopt_feed` writes |
| `.client.json` is always a document `parse_client_document` accepts | `pull.py:ClientPull.fetch` parses before writing |
| A feed names no work missing from `works` and stages none twice; slots ordered, non-overlapping, inside a whole-day horizon; instants carry an offset | `manifest.py:_feed`, `_instant` (Arrt's `player-contract.md` § Rules a schema cannot state, § Time) |
| An `uploaded` binding names a content id | SQLite CHECK in `state/store.py:_SCHEMA` |
| At most one binding per work per Frame wall, **for the set it is on** | `UNIQUE (artwork_id)` in a per-wall file; the other sets' rows are in `tv_binding_aside`, `PRIMARY KEY (tv_identity, artwork_id)` |
| A binding is used only on the set it was made on | `DisplayState.belongs_to`, called by `displays/frame.py:FrameDisplay._know_the_set` before anything reads a binding on a connection (`tests/test_bindings.py::TestABindingIsUsedOnlyOnTheSetItWasMadeOn`) |
| A removal is sent only from a store whose recorded identity is the connected set's | `displays/frame.py:FrameDisplay._may_remove` (the phase is `Identified`), checked in `_remove_orphans` (`tests/test_bindings.py::TestASetThatWillNotSayWhichItIs`, `::TestTheWallIsInOnePhaseOfFour`) |
| A removal names only a picture an upload record of that set names there | `DisplayState.uploads_on`, read by `_remove_orphans` (`tests/test_bindings.py::TestThePlayerRemovesOnlyWhatItRecordedUploading`, `::TestTheUploadRecord`, `::TestARecordOfNoSet`) |
| An upload is sent only once its record is written | `DisplayState.record_sending` before `TvClient.upload`; a `StoreFault` ends the pass (`tests/test_bindings.py::TestThePlayerRemovesOnlyWhatItRecordedUploading::test_a_store_that_refuses_writes_puts_no_picture_on_the_set`) |
| A record names its picture by marker or id | SQLite CHECK in `state/store.py:_SCHEMA` (`tv_upload`) |
| A file the Player writes whole is staged, forced to the card and renamed by one helper | `safe_write.py`; `tests/test_safe_writes.py::test_no_file_is_renamed_into_place_under_src_but_by_the_helper_and_the_two_streaming_writers` |
| A wall's directory is removed only a week after it left, only from a document that was read, and only if it is a wall's | `client.WallCaches` (`tests/test_client.py::test_a_wall_that_left_keeps_its_directory_for_six_days_and_loses_it_by_the_eighth`) |
| A store newer than the code is not opened | `state/store.py:DisplayState._prepare` raises `StateSchemaTooNew`; `__main__.py:run_frame_wall` parks that wall only |
| At most one worker per output, at most one renderer per label output | `client.py:Supervisor.reconcile`, `_reconcile_labels` |
| A heartbeat never names a work outside `showing_art` | `heartbeat.py:DisplayReport.__post_init__` |

**What the Player does not check at run time:** the documents against the
contract's JSON Schemas. `jsonschema` and `arrt-contract` are dev-group
dependencies only (`pyproject.toml` `[dependency-groups] dev`); nothing in `src/`
imports either (searched 2026-10-27). The hand-written readers
(`manifest.py:parse`, `client.py:parse_client_document`,
`label_rule.py:parse_label_document`) are held to the contract's fixtures by
`tests/test_player_contract.py`, and the heartbeats the Player writes are held to
the schemas there too.

## Discrepancies with Arrt's record

Recorded, not fixed; Arrt's artifacts are changed on Arrt.

- **Arrt's `data-model.md` § TvBinding gives the table a `wall_id` column and makes
  `artwork_id` unique per `wall_id`.** The built table has no `wall_id`;
  `artwork_id` is `UNIQUE` within the file, and the per-wall scope comes from one
  file per wall directory (`config.py:WallSettings.state_path`). Same effect,
  different mechanism.
- **Arrt § TvBinding calls it "display plane only".** The Player is a separate
  repository and a client, and its code says "the Player" and "the server".
- **Arrt's `data-model.md` § Player observation says a heartbeat "also carries
  cache state".** `heartbeat.py:Health.document` carries no cache-state field.
  That section is under "Planned entities … not built", so this may be an
  unbuilt plan rather than an error.
- **`config.py`'s module docstring cites `clients.md`, `labels-and-surfaces.md`
  and `feeds-and-players.md` without a repository**; per `CLAUDE.md` they are
  Arrt's.
