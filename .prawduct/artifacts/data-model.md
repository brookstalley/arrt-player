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
through. Written 2026-10-27 from the code at `b82ba74`. Every claim names the
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
| `.client.etag` | `pull.py:ClientPull.fetch` | The ETag served with it. Removed when the server serves none. Sent only while `.client.json` exists (`ClientPull._cached_etag`). |
| `token_file` | the `samsungtvws` library, not the Player | The Frame's pairing token, at this path only when `TV_ADDRESS` is set and `TV_TOKEN_FILE` is not (`config.py:_frame`). See `security-model.md` § To the television: the pairing token. |
| `<wall id>/` | `__main__.py:run_frame_wall`, `run_screen_wall` (mkdir) | One wall. `config.py:ClientSettings.wall` refuses an id that is not one plain path component (`_PLAIN_COMPONENT`, `WallIdUnusable`). A leading dot is refused, which keeps the two `.client.*` files apart from every wall directory. |
| `<wall id>/manifest.json` | `pull.py:Pull._adopt_feed` | The last feed that `manifest.py:parse` accepted, re-serialised (`json.dumps(document, indent=2)`), not the server's bytes. Written only after every work's media is cached or answered for. Read by `manifest.py:Watcher`. |
| `<wall id>/manifest.etag` | `pull.py:Pull._adopt_feed` | The ETag served with the feed. Sent only while `manifest.json` exists (`Pull._cached_etag`). |
| `<wall id>/media/sha256-<hex>` | `pull.py:Pull._cache_media` | One presentation master, named by the SHA-256 of its bytes (`manifest.py:media_name`). Written only when `hashlib.sha256(data)` equals the feed's `media.sha256`. After each adoption, `Pull._evict` removes every file that neither the new feed nor the one it replaced names. |
| `<wall id>/composed/<key>.jpg` | `compose.py:compose`, driven by `programmes/schedule.py:Schedule` | Each work drawn for this wall's display geometry and mat, named by `compose.py:composition_key`. Pruned by `Schedule._tidy`. Never leaves the device (Arrt's `data-model.md` § Direction, "Derived artifacts are regenerated, never transported"). |
| `<wall id>/display-heartbeat-<wall id>.json` | `heartbeat.py:write`, called from the wall loop | The wall's heartbeat (`heartbeat.py:Health.document`), rewritten about every 60 s (`heartbeat.py:INTERVAL_SECONDS`) and sooner on a display-state change (`wall.py:DisplayRecord`). `pull.py:Pull._forward_heartbeat` POSTs it when it changes. |
| `<wall id>/display-state.sqlite` (+ `-wal`, `-shm`) | `state/store.py:DisplayState` | Frame walls only (`config.py:WallSettings.state_path`, opened in `__main__.py:run_frame_wall`). See `DisplayState` below. |

**Every cache write is temp-then-rename with `fsync`**: `pull.py:_write_atomically`
(`.<name>.tmp` beside the target) and `heartbeat.py:write` (`<name>.tmp`). A
reader therefore sees the old file or the new one, never part of one.

**Nothing removes a wall's directory.** No code in `src/` calls `rmtree` or
`rmdir` (searched 2026-10-27). A wall taken away from this client keeps its
directory, its media and, on the Frame, its store, until somebody deletes them.
Pruning happens only inside a wall that is still running: `Pull._evict` for
`media/`, `Schedule._tidy` for `composed/`.

### DisplayState (`display-state.sqlite`)

The Frame's own memory of what it put on the television. One file per Frame
wall. `__main__.py:run_frame_wall` opens it once per worker and holds it for the
worker's life. `state/store.py:DisplayState` is its only reader and writer; no
other module opens the path (`config.py:STATE_FILENAME` is referenced only by
`WallSettings.state_path`). Losing it costs a re-upload of every work, not a
record (`state/store.py` module docstring).

Schema version is SQLite's `PRAGMA user_version`, currently 2
(`state/store.py:SCHEMA_VERSION`). Journal mode is WAL (`DisplayState.__init__`).

#### `tv_binding` — one work's image on this television

`state/store.py:_SCHEMA`, read back as `state/store.py:Binding`.

| Column | Type | Constraints (as enforced by SQLite) | Description |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | A UUID4, minted on first insert. An upsert keeps the existing one (`_write_binding`'s `ON CONFLICT` does not set it). Nothing reads it. |
| `artwork_id` | TEXT | NOT NULL, UNIQUE | The work id from the feed. **At most one row per work in this file**, and because the file is per wall, at most one per (wall, work). |
| `tv_content_id` | TEXT | nullable; CHECK `upload_status <> 'uploaded' OR tv_content_id IS NOT NULL` | The set's own id for the uploaded picture. A cache key, meaningless off this set. Cleared to null by `record_upload_failure` and `mark_orphaned`. |
| `tv_thumb_md5` | TEXT | nullable | **Nothing writes it** (`DisplayState.record_upload` docstring). Null on every row. |
| `render_fingerprint` | TEXT | nullable | `"<st_mtime_ns>:<st_size>"` of the composed file when it was sent (`displays/frame.py:_fingerprint`). A mismatch, or null, counts as changed (`_render_changed`) and forces a re-upload. Added at schema 2 by `DisplayState._widen`. |
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

A key no code reads is left in place (`state/store.py` module docstring).

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
| Frame | `TV_ADDRESS` and, only when it is set, `TV_PORT`, `TV_TOKEN_FILE`, `TV_CLIENT_NAME`, `LATITUDE`, `LONGITUDE`, `LOCATION_NAME`, `LOCATION_REGION`, `TV_MIN/MAX_BRIGHTNESS`, `BRIGHTNESS_INTERVAL_SECONDS`, `TV_UPLOAD_TIMEOUT_SECONDS`, `TV_UPLOAD_RETRY_SECONDS`, `TV_SELECT_CONFIRM_SECONDS`, `TV_CONNECT_TIMEOUT_SECONDS`, `TV_RETRY_MIN/MAX_SECONDS`, `TV_PANEL_WIDTH_PX`, `TV_PANEL_HEIGHT_PX`, `TV_PANEL_DIAGONAL_INCHES` | `FrameSettings`, or None (`_frame`) | `LATITUDE`, `LONGITUDE`, `LOCATION_NAME` required once `TV_ADDRESS` is set. `TV_TOKEN_FILE` defaults to `CACHE_DIR/token_file` and must be absolute when set. `TV_CLIENT_NAME` defaults to `tvpi` (`DEFAULT_TV_CLIENT_NAME`); changing it costs a new pairing. Panel sizes must be above zero. |
| Label panel | `EPD_DEVICE`, `EPD_PANEL_WIDTH_PX`, `EPD_PANEL_HEIGHT_PX`, `EPD_PANEL_DIAGONAL_INCHES`, `EPD_VIEWING_DISTANCE_INCHES`, `EPD_MARGIN_PX`, `EPD_ROTATE_DEGREES` | `PanelSettings` (`_panel`) | Read whether or not `TV_ADDRESS` is set. Empty `EPD_DEVICE` is a client with no panel. Diagonal and distance have no default; unset, the panel draws nothing and says why (`__main__.py:label_surface`). Rotation 0 or 180 is enforced in `panel/epaper.py`, not here. |
| Retired | `WALL_ID`, `WALL_TOKEN`, `MANIFEST_SOURCE` | `RETIRED_SETTINGS` | Any of them set stops the start with a `ConfigError` naming its replacement. |

A typed value that does not parse is refused, never defaulted (`_int`, `_float`).
A `ConfigError` exits with status 2 and one line on stderr (`__main__.py:main`).

`WallSettings` and `Settings` (a Frame wall: `WallSettings` plus `FrameSettings`)
are derived per wall by `ClientSettings.wall` and `ClientSettings.frame_wall`.

### In-memory state

Lost on restart by design; each is rebuilt from the cache or the server.

| Holder | What | Source |
|---|---|---|
| `client.py:Supervisor` | Running wall workers keyed by wall id (`_running`), label renderers keyed by label output name (`_labels`), workers still stopping (`_stopping`) | `client.py:Supervisor.reconcile`, `_reconcile_labels` |
| `client.py:FrameIdentity` | The Frame's `device.duid`, once read; never read again after | `tv/samsung.py:SamsungTv.read_identity` |
| `manifest.py:Watcher` | The last good `Feed` and the file stamp `(st_mtime_ns, st_size)` last read | `Watcher.poll` |
| `pull.py:LabelPull` | The label document's ETag; not persisted, so a restarted renderer fetches the whole document | `LabelPull.fetch` |
| `label_renderer.py:LabelRenderer` | The last accepted label document and the drawing on the panel | `LabelRenderer.tick` |
| `displays/frame.py:FrameDisplay` | `_showing_art`, `_wall_content_id`, the set's last announcement, retry ladders, whether reconciliation is owed | `FrameDisplay.__init__` |

## Relationships

- **A client has 0..n walls running, each on one output.** The server assigns
  them (`GET /client`). `client.py:Supervisor.reconcile` runs at most one worker
  per output, enforced by the `claimed` map in that method; a second wall on the
  same output is logged as unplaceable and not started.
- **A client has at most one Frame** (`config.py:FRAME_OUTPUT`, one
  `FrameSettings`) and **at most one label panel** (`config.py:PANEL_OUTPUT`,
  `epd-0`). HDMI outputs are whatever `client.py:hdmi_outputs` finds under
  `/sys/class/drm`.
- **A label output has at most one renderer**, keyed on its name in
  `Supervisor._labels`. The wall it captions need not be one this client shows.
- **A wall has one cache directory, one `Pull`, one `Watcher`, one heartbeat
  file**, and on the Frame **one `display-state.sqlite`**. All derived from the
  wall id by `WallSettings`; no wall reads another's directory.
- **A `tv_binding` row references a feed work by `artwork_id`**, with no foreign
  key: the work exists only in the feed, which is a file. A binding for a work
  the feed no longer names is kept, not deleted; its content id stays in
  `accounted_content_ids` and is therefore not removed from the set as an
  orphan.
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
  another output (`Supervisor._stop`). A move is a stop and a fresh start.
- *absent* → **unplaceable**: no such output, output already claimed, or
  `WallIdUnusable`. Logged once per (wall, output) (`_unplaceable`).
- Any → **stopping**: SIGTERM or SIGINT (`__main__.py:_run`); `Supervisor.stop_all`
  waits for every worker to close what it holds.

A dead pull ends its wall's worker so the supervisor restarts both
(`__main__.py:_beside_its_pull`, `_pull_ended`).

### Label renderer lifecycle (`client.py:Supervisor._reconcile_labels`)

Same as a wall worker, with one extra exit: **retired** when the label output is
no longer mapped, which sets `retired` so the renderer blanks the panel
(`_stop_label(retired=True)`). A change of mapping is stopped (not retired) and
restarted. Shutdown stops without retiring, so e-paper keeps its last caption.

### Pull cycle (`pull.py:Pull.cycle`, `pull.py:_Poll`)

One poll ends in one of three outcomes:

| Outcome | When | Next |
|---|---|---|
| `ANSWERED` | 200 adopted or refused as unreadable, 304, or a 4xx refusal | Heartbeat forwarded; backoff reset to 2 s; wait `poll_interval_seconds`. |
| `MEDIA_FAILING` | A master returned a non-200, non-404 status or a transport error | Heartbeat forwarded; manifest not written; backoff doubles. |
| `UNREACHABLE` | Transport error, timeout or 5xx on the manifest | No heartbeat; backoff 2 s doubling to 60 s (`BACKOFF_START_SECONDS`, `BACKOFF_MAX_SECONDS`). |

A master that answers 404 or fails its hash is skipped, not retried this feed
(`Pull._cache_media`, `_skip_once`), and the feed is still adopted.

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

No transition deletes a row.

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
| A wall id names one plain directory under `CACHE_DIR` | `config.py:_PLAIN_COMPONENT` in `ClientSettings.wall` |
| `CACHE_DIR` and `TV_TOKEN_FILE` are absolute | `config.py:_absolute` |
| A cached master's bytes match its name | `pull.py:Pull._cache_media` (SHA-256 before write) |
| `manifest.json` is always a document `manifest.parse` accepts | `pull.py:Pull._pull_manifest` parses before `_adopt_feed` writes |
| `.client.json` is always a document `parse_client_document` accepts | `pull.py:ClientPull.fetch` parses before writing |
| A feed names no work missing from `works`; slots ordered, non-overlapping, inside a whole-day horizon; instants carry an offset | `manifest.py:_feed`, `_instant` (Arrt's `player-contract.md` § Rules a schema cannot state, § Time) |
| An `uploaded` binding names a content id | SQLite CHECK in `state/store.py:_SCHEMA` |
| At most one binding per work per Frame wall | `UNIQUE (artwork_id)` in a per-wall file |
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
- **Arrt § TvBinding calls it "display plane only"**, and `state/store.py`'s and
  `manifest.py`'s docstrings still say "display plane" and "curation plane". The
  Player is a separate repository and a client; there are no planes in it.
- **Arrt's `data-model.md` § Player observation says a heartbeat "also carries
  cache state".** `heartbeat.py:Health.document` carries no cache-state field.
  That section is under "Planned entities … not built", so this may be an
  unbuilt plan rather than an error.
- **`config.py`'s module docstring cites `clients.md`, `labels-and-surfaces.md`
  and `feeds-and-players.md` without a repository**; per `CLAUDE.md` they are
  Arrt's.
