---
artifact: nonfunctional-requirements
version: 1
depends_on: []
last_validated: null
---

# Non-Functional Requirements — Arrt Player

The Player is one process on a Raspberry Pi beside a wall. It pulls its walls'
programming into a local cache and draws from that cache alone. One household
sees the result; nobody operates it. Three things constrain it: the wall must
keep showing art when the server is gone, the television must never be taken
from somebody watching it, and a failure must reach somewhere other than the
wall. Throughput and scale get a line each.

**How this file relates to Arrt's.** Arrt's `nonfunctional-requirements.md`
states most of the requirements that bind the Player, and it stays their home.
Each section below names the Arrt section and adds only what this repository's
code commits to: the number, where it is set, and what enforces it. Where a
requirement has no enforcing code or test, it says "stated, not enforced".
Arrt's text that is now wrong about the Player is listed at the end.

<!-- OPTIONAL norm home. To record a norm, add a `## Direction` heading with entries (a bold
     **Statement.**, then `Why:`, `Status:`, optional `Retroactivity:` / `Rulings:`) — anatomy
     and rules: /prawduct:methodology norms. Add the heading only with a real entry: the
     advisory probes read a bare `## Direction` heading as ratified norms. With no norms, leave
     this comment; "none to ratify" is recorded through /prawduct:doctor, not here. -->

No norm is ratified in this file. Two of Arrt's ratified norms bind the Player
directly: **the Player's ability to show art never depends on the
server being reachable** (Arrt § Direction, which says "display plane" and
"curation plane"; read "Player" and "server" per its 2026-09-30 amendment), and **the television belongs to whoever is using
it** (Arrt § Availability › The television belongs to whoever is using it).

## Performance

Arrt § Performance › Display plane sets the two latency targets. The Player's
code carries them as follows.

| Requirement | Value | Where it is set | Enforced by |
|---|---|---|---|
| Label redrawn after the picture changes | within 15 s (Arrt § Performance) | `label_renderer.LABEL_DRAW_BUDGET_SECONDS` = 15 bounds one draw; `config.DEFAULT_LABEL_POLL_SECONDS` = 1 is the label document poll | The draw bound is tested (`tests/test_label_renderer.py`). The end-to-end 15 s is not measured by anything in this repository: stated, not enforced |
| Art correct after a Player restart | within 60 s (Arrt § Performance, an assumption) | No single setting. The cached client document is read before the first request (`client.Supervisor.run`), so walls start without the server | Stated, not enforced. No test times it |
| Wall reacts to a new manifest | about 1 s | `config.DEFAULT_POLL_INTERVAL_SECONDS` = 1.0 (`MANIFEST_POLL_SECONDS`). It sets both the wall loop's tick (`wall.Wall.tick`) and the pull's poll of the server (`pull.Pull.__init__`, `_interval`) | Default only; not asserted |
| Wall assignment reaches the client | about 30 s | `config.DEFAULT_CLIENT_POLL_SECONDS` = 30. Not read from `.env` | Default only |
| A selection is confirmed by the set | 8 s ceiling, failing path only | `config.DEFAULT_SELECT_CONFIRM_SECONDS` (`TV_SELECT_CONFIRM_SECONDS`) | `tests/test_samsung_client.py` |
| Composing one work on the Player | 5 s and 1 GB on a Pi 4 (Arrt § Performance, measured 2026-10-04) | Runs in a thread, one work at a time per wall (`programmes/schedule.Schedule._compose_next`) | Stated, not enforced. Walls on one client compose concurrently, one each |

## Scalability and Capacity

Scale is not a goal. One client drives the walls the server assigns it. A client
has at most one Frame output (`config.FRAME_OUTPUT`, present only when
`TV_ADDRESS` is set), and a label is an output a Player has zero or more of, as a
display is (the owner's ruling of 2026-11-19): the configuration describes at most
one panel today (`config.PANEL_OUTPUT`, `epd-0`, present only when `EPD_DEVICE` is
set, and refused when it names more), and nothing past it assumes one. HDMI outputs
are whatever the kernel lists under `/sys/class/drm` (`client.hdmi_outputs`).

**Cache size is bounded by the feeds, not by a setting.** Per wall, the media
directory holds the masters named by the feed just adopted and by the one before
it; everything else is removed at adoption (`pull.Pull._evict`, called from
`_adopt_feed` with `held | previous`). Tested in
`tests/test_pull.py::test_a_feeds_media_is_evicted_once_two_documents_in_a_row_have_not_named_it`.
Composed pictures are removed when no work composes to them
(`programmes/schedule`, the tidy). A wall taken away from this client loses its
directory a week later, counted only while the Player reads its client document
(`client.WallCaches`), and a label's cached document goes when no label maps it
(`__main__.LabelCacheSweep`), so growth is bounded by the walls and labels the
document places and by a week's worth of departed ones. A Frame's store grows
with the bindings of the sets the wall has met and set aside, one row per work
per set, for as long as the store lasts. What the code does not do, checked by
searching `src/` for `disk_usage` and `statvfs`:

- **No free-space check and no size cap.** A full disk surfaces as a failed write,
  which the pull, the client document, the label cache and the Frame's store each
  answer without ending the wall (below).

**Memory is bounded by the unit, not by the code.** `deploy/display.service` sets
`MemoryHigh=60%` and `MemoryMax=75%` of the Pi's RAM, so an exhausted Pi loses
the Player and not sshd; past `MemoryMax` the kernel kills inside the unit,
systemd stops the rest of it, and `Restart=always` brings it back
(`tests/test_deploy_unit.py::test_the_unit_bounds_its_memory_below_the_pis`).
That the kernel enforces it needs its memory controller, which the runbook has
the operator check on the Pi: stated, not enforced here. In code the only sized
consumer is composition (above); a master is streamed to disk, never held whole.
Pillow's default decompression-bomb guard applies; `compose.py` catches
`DecompressionBombError` as an uncomposable master.

**The journal is bounded** at 256M, in 32M files, on both the runtime and system
ceilings (`deploy/journald.conf.d/10-bound-the-journal.conf`). Arrt's
operational-spec § Risks gives the reason and the check.

## Availability

Arrt § Availability sets the target: the Player is continuous and recovers
without human action, and "down" is a wall not following its programme or a
label that disagrees with the picture. Arrt also says why availability here is
mostly detection. What follows is what the Player's code does.

### When the server is down, the wall draws from the cache

The norm is Arrt § Direction (display independence). The mechanism is
`pull.py`'s module docstring: a wall renders only `CACHE_DIR/<wall id>/manifest.json`,
and the pull writes that file only after every master it names is fetched and
checked against its hash, or answered for, or has been waited for as long as
`MEDIA_HOLD_SECONDS` (10 minutes): a master that fails in a way that may clear
holds the feed already cached for that long, after which the new feed is cached
without it and with no ETag, and the master is asked for again on every poll.
Every failure keeps the cache
(`player-contract.md` § Transport). Tested in `tests/test_pull.py`
(`test_the_server_stopped_while_the_wall_runs_and_the_schedule_continues_from_the_cache`,
`test_a_player_restarted_with_the_server_down_starts_from_its_cache`),
`tests/test_client.py`
(`test_a_client_restarted_while_the_server_is_down_starts_the_walls_it_last_knew`)
and `tests/test_availability.py`.

| Request | Timeout | Retry | Where |
|---|---|---|---|
| Wall manifest, wall heartbeat | 60 s total, 10 s connect, per request | Backoff from 2 s, doubling to 60 s, reset when the manifest route answers; the heartbeat is not held to it | `pull.Pull.run` (`aiohttp.ClientTimeout`), `BACKOFF_START_SECONDS`, `BACKOFF_MAX_SECONDS` |
| A master | 10 s connect, 30 s without a byte, no total; stopped at the bytes its feed declares | The feed is held for at most 10 minutes (above), and the master asked for again on each poll | `pull._MEDIA_TIMEOUT`, `pull._stream_to`, `MEDIA_HOLD_SECONDS` |
| A wall's feed | Read up to 16 MiB; one past it is refused and the cached feed stays | — | `pull.MANIFEST_MAX_BYTES` |
| A client or label document | Read up to 1 MiB | — | `pull.DOCUMENT_MAX_BYTES` |
| A higher manifest major | — | Asked again every 60 s while a lower one answers | `pull.HIGHER_MAJOR_SECONDS` |
| Client document and client heartbeat | 30 s total, 10 s connect | None; asked again on the next 30 s client poll. A heartbeat the server refused in its error form is held until it changes or 300 s pass | `pull.ClientPull._client`, `ClientPull.report`, `REFUSED_REPORT_RESEND_SECONDS` |
| Label document | 10 s total, 5 s connect | None; asked again on the next 1 s label poll | `pull.LabelPull._client` |

While the server cannot be reached, a label holds its last caption for 30
minutes (`label_rule.HOLD`), measured on the monotonic clock from the server's
last answer so that a step of the Pi's clock neither blanks it at once nor holds
it for ever, then follows the rule in `labels-and-surfaces.md` § What a label
says. Each accepted label document is cached, so a Player restarted while the
server is down captions from it for what is left of that hold, never more than
30 minutes from the restart (`LabelPull.cached`, `LabelRenderer._seed`;
`tests/test_label_renderer.py`).

**A cache failure is kept local to the cache.** A write that fails while adopting
a wall's feed (`pull._write_atomically`, which writes through `safe_write`, inside `Pull._cache_feed`, `ENOSPC` or
`EROFS`) is caught there: the previous manifest stays where the watcher reads it
(the write is temp-and-rename), no ETag is stored for the feed that could not be
written, so the next poll is answered whole and tries again, and the heartbeat
still goes, because the server answered. It is said once per episode
(`pull.cache_unwritable`, then `pull.cache_writable`), as the client document's
(`ClientPull.fetch`, `client.cache_unwritable`) and a label document's
(`LabelPull`, `label.cache_unwritable`) refused writes are. Held by
`tests/test_pull.py`. Not checked on the Pi: whether a real read-only remount
returns `EROFS` for `unlink` and `mkdir` as the tests assume.

### The process comes back on its own

| Layer | Behaviour | Where | Enforced by |
|---|---|---|---|
| systemd | `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0` (no start rate limit) | `deploy/display.service` | `tests/test_deploy_unit.py::test_the_unit_cannot_silently_stop_retrying` |
| The unit's watchdog | `Type=notify`, `WatchdogSec=300`: a Player that sends no `WATCHDOG=1`, which it sends only for progress, is stopped with SIGTERM and started again; every mode owes `READY=1` or the start fails at `TimeoutStartSec=600` | `deploy/display.service`, `notify.py`, `__main__.PettingSupervisor` | `tests/test_notify.py`, `tests/test_deploy_unit.py::test_the_unit_waits_for_the_players_word_and_ends_one_that_stops_making_progress` |
| One wall's worker | Restarted after a wait from 5 s doubling to 300 s; the ladder resets after a run longer than 300 s | `client.RESTART_MIN_SECONDS`, `RESTART_MAX_SECONDS`, `Supervisor._keep_running` | `tests/test_client.py::test_a_crashed_worker_is_logged_and_started_again` |
| A wall's pull | Its death ends its wall's worker at once, so the two restart together | `__main__._beside_its_pull`, `_pull_ended` | `tests/test_main.py::test_a_pull_that_dies_ends_its_walls_worker_at_once_and_says_why` |
| The supervisor | Not caught. The process ends and systemd restarts it | `client.py` module docstring | — |
| A fault of the Frame's store (a lock, a full disk, a damaged file) | The pass ends, not the wall; said once per episode and in the heartbeat's `last_error`; tried again on the set's own ladder | `state.store.StoreFault`, `FrameDisplay._the_store_failed` | `tests/test_bindings.py::TestTheWallOutlivesAStoreFault` |
| A Frame store from a newer Player | That wall is parked and says so once; other walls run | `__main__.run_frame_wall`, `state.StateSchemaTooNew` | `tests/test_main.py::test_a_store_from_a_newer_player_parks_its_wall_and_says_so_once` |

### Start-up

`config.load` and `config.load_ui` refuse to start, naming the variable, when
`CACHE_DIR` is unset; when `CACHE_DIR` or `TV_TOKEN_FILE` is not absolute; when
a numeric value does not parse, is `nan` or infinite, or `UI_PORT` is not a port;
when any `*_SECONDS` interval or timeout is zero or below (`config._seconds`: a
zero would be a loop that never waits), or `LATITUDE` or `LONGITUDE` is out of
range; when a wall id equals a name the cache directory reserves
(`WallIdUnusable`); when `SERVER_URL`
is not an http(s) address with a host or carries a user name, password, query or
fragment, `CLIENT_TOKEN` holds whitespace, or `PLAYER_NAME` is over 64
characters or holds control characters (the same functions judge the settings
page's form, so neither accepts what the other refuses); when the settings
page's file cannot be trusted (`settings_ui.store.SettingsUnreadable`); and when
a retired setting (`WALL_ID`, `WALL_TOKEN`, `MANIFEST_SOURCE`) is set
(`config.RETIRED_SETTINGS`); when `TV_ADDRESS` is not an IPv4 address or a host
name without a scheme or a port; when `EPD_DEVICE` names more than one panel;
and when `FILE_OUTPUT_DIR` is set beside `TV_ADDRESS` or `EPD_DEVICE`, in or around
`CACHE_DIR`, in slideshow mode, or a `FILE_OUTPUT_*` key is set without it. `__main__.main` turns that `ConfigError` into exit
status 2 and a line on stderr
(`tests/test_main.py::test_a_missing_deployment_value_refuses_to_start_and_says_so_at_the_terminal`).
Under `Restart=always` that is a restart every 5 s until `.env` is fixed, which
is what keeps it visible.

**No server is not a refusal: it is setup mode** (owner, 2026-11-10). With
`SERVER_URL` or `CLIENT_TOKEN` set neither in the environment nor on the
settings page, `config.load` raises `SetupRequired`, deliberately not a
`ConfigError`, and the Player serves only its settings page and prints
`go to http://<address>:<port>/… to configure`
(`tests/test_config.py::TestWhatMustBeSet`,
`tests/test_settings_ui.py::test_without_a_server_the_player_serves_only_its_page_and_says_where`).
A setup-mode Player whose page cannot bind exits 1, for systemd to try again.

**In slideshow mode the server keys are not read** (`PLAYER_MODE=slideshow`,
owner 2026-11-10, extending the rule above). A slideshow needs `SLIDESHOW_URL`
and `REDDIT_SESSION` instead; lacking either is setup mode in the same way,
naming what is missing and saying that nothing is shown
(`tests/test_config.py::TestSlideshowMode`,
`tests/test_settings_ui.py::TestWhatTheSlideshowSays::test_setup_mode_for_a_slideshow_says_nothing_is_shown`).
Set and unusable is still a refusal: a mode other than `arrt` or `slideshow`, a
listing that is not a Reddit listing, a cookie that cannot ride in a header, an
output that is not an HDMI connector, or dwell limits outside 5 s to 3600 s or
with the minimum above the maximum (`config.player_mode_problem`,
`reddit_settings.listing_url_problem`, `session_problem`,
`config.slideshow_output_problem`, `dwell_problem`; the page's form is judged by
the same functions). A settings file of layout version 1 or 2 is read; one newer than
version 3 is refused by name.
Beside running walls, a page that cannot bind costs the page and nothing else
(`::test_a_page_that_cannot_listen_says_so_and_returns`).

**A label that cannot be drawn never stops the walls.** No `EPD_DEVICE` means no
label output. A configured panel that will not open, a missing text stack, or
unstated viewing conditions each become a label output reported as not
connected, with a `panel.unavailable` warning (`__main__.label_panel`,
`label_surface`; tested in `tests/test_main.py`).

### Shutdown is SIGTERM, never SIGKILL

Why: the set holds an abandoned art-channel client's slot for minutes, and a
hard kill can leave the next connection refused
(`samsung-tv-state-findings.md` § The art channel can refuse to open, and it is
not about art mode).

- SIGTERM and SIGINT set one stop event (`__main__._run`,
  `loop.add_signal_handler`).
- `client.Supervisor.stop_all` stops every wall worker and label renderer and
  waits for each to close what it holds, with no timeout of its own; it retires
  nothing, so every screen and label is left as it was. The panel is closed
  after that, once any draw still in the driver has finished or the label budget
  has run out (`__main__._run`, `finally`; `LabelPanel.close`).
- The bound is systemd's: `TimeoutStopSec=90`, `KillSignal=SIGTERM`
  (`deploy/display.service`); the watchdog's stop is the same SIGTERM
  (`WatchdogSignal=SIGTERM`, `TimeoutAbortSec=90`). The unit's comment gives the worst case it must
  clear as about 45 s; `tests/test_deploy_unit.py::test_the_unit_gives_the_television_time_to_let_go`
  asserts `TimeoutStopSec` ≥ 45 and `KillSignal=SIGTERM`.
- Tested end to end: `tests/test_client.py` runs `_run`, sends the process
  SIGTERM, and asserts exit 0 within 5 s with the art channel closed.
- The one known case past 90 s is a panel driver wedged in SPI on a worker
  thread. The unit's comment records it as left to SIGKILL, after the television
  is closed.

### Leaving the television to whoever is using it

The requirement is Arrt's `nonfunctional-requirements.md` § Availability › The
television belongs to whoever is using it. What this repository's code enforces is narrower than "never touch a
set that is not in art mode":

- **Every write to the set is gated, in the seam.** `tv.client.WRITES` names each
  verb that changes what the set shows or stores, and the gate it passes. Each is
  a final `TvClient` method that reads the set immediately before the client's
  `_send_<verb>` (`showing_art`, then `power_state` for a verb whose gate it can
  open) and raises `TvNotOurs` when the set is not in a state the verb was
  measured safe in; a subclass cannot define the verb itself, so a verb added
  later inherits the read. Selection needs art mode on. Upload, removal,
  brightness and switching off the set's own slideshow need art mode on, or art
  mode off with `PowerState` `standby` (the dark state), and are held when the
  set is lit and showing a programme and whenever `PowerState` cannot be read.
  `samsung-tv-state-findings.md` § What works in each state is the measurement.
  Tested in `tests/test_samsung_client.py`
  (`test_a_write_is_sent_only_in_the_states_it_was_measured_safe_in`,
  `test_the_read_is_taken_at_the_moment_of_the_write_not_before`,
  `test_nothing_outside_the_seam_reaches_a_send`) and `tests/test_frame_wall.py`
  (`test_every_write_is_held_from_a_set_showing_a_programme`,
  `test_a_set_somebody_is_watching_is_sent_no_write_at_all`,
  `test_a_set_whose_power_cannot_be_read_is_sent_no_write_either`).
- **Selection is also gated early.** The schedule asks `display.is_ours()` before every
  `show` (`programmes/schedule.Schedule`, the `may_attempt` and `is_ours` check before `display.show`),
  and the Frame answers from a fresh `get_artmode` read
  (`displays/frame.FrameDisplay.is_ours`). A no selects nothing and backs off on
  `TV_RETRY_MIN_SECONDS` 5 to `TV_RETRY_MAX_SECONDS` 300
  (`config.DEFAULT_TV_RETRY_*`). Tested in `tests/test_frame_wall.py`
  (`test_a_television_somebody_is_watching_is_left_alone`,
  `test_an_evening_of_television_ends_on_the_slots_work_in_one_selection`).
- **A held write is retried, not failed.** `FrameDisplay` answers `TvNotOurs` as
  it answers a selection's no: said once (`tv.writes_held`), not recorded as a
  failed upload, and tried again on one shared wait (`FrameDisplay._writes`) that a set
  returning to art mode ends (`tv.writes_resumed`). So uploads wait while somebody
  watches television, and the wall catches up in art mode.
- **A removal is the one write that needs a store that knows the set.** The gate
  asks whether the set may be written to; a removal also needs the store's
  recorded identity to be the connected set's, or it sends nothing
  (`FrameDisplay._may_remove`; `security-model.md` § What it does to the television).
  A binding retired after 14 days unnamed is removed through the same gated verb.
  **What a removal names is only what the Player recorded uploading** on that set
  (`DisplayState.uploads_on`): the household's own pictures are left where they are.
- **Connecting is not gated.** It includes a REST read on every construction, the
  read of the set's identity on every new connection (one GET, no key; the client reads it once a minute too, for its heartbeat), and,
  **only when the token file is empty**, an open of the remote-control channel to
  mint a token, which raises a pairing prompt on the screen
  (`tv/samsung.SamsungTv._construct`; `samsung-tv-state-findings.md` § This
  product has been opening the remote-control channel all along). Whether a held
  write would have been visible over a programme has not been measured: the gate
  holds it there rather than finding out, and which verbs may be released is
  the owner's measurement at the set.
- **No shipped code sends a power key.** A search of `src/` for `KEY_POWER`,
  `send_key` and `SendRemoteKey` finds nothing. No test asserts that absence:
  stated, not enforced. Power control at the window edges (Arrt's amendment of
  2026-08-17) is not built; `programmes/schedule.py` leaves the wall as it is
  through a gap "until power control exists".
- **`tools/power_probe.py` is the only code that presses power.** It sends
  nothing without `--i-am-at-the-set` (`tests/test_power_probe.py`). It is run by
  hand and never by the daemon.

### Detection

A failure must reach the server's heartbeat view or the journal, not only the
wall (`project-state.yaml` goals). Arrt's `observability-strategy.md` is the
authority on what the server does with it.

| Signal | Cadence | Where | Enforced by |
|---|---|---|---|
| Wall heartbeat file | Every 60 s, and at once when the display state or capabilities change | `heartbeat.INTERVAL_SECONDS` = 60, `wall.Wall._beat` | `tests/test_heartbeat_interval.py` holds 60 to the contract's `heartbeat_interval_seconds`. The contract's `stale_after_intervals` is 3, so the server calls a wall stale after 180 s |
| Wall heartbeat POST | Every poll interval from the pull's own loop, when the file holds a report not yet sent and the last manifest poll was answered; a manifest backoff does not delay it | `pull.Pull._heartbeats`, `Pull._forward_heartbeat` | `tests/test_pull.py::test_the_heartbeat_file_is_posted_when_it_holds_a_new_report_and_not_otherwise`, `::test_a_failing_master_does_not_hold_the_heartbeat_back_to_the_backoff` |
| Client heartbeat POST | When the outputs change, else once per 60 s, checked on the 30 s client poll | `client.Supervisor._report_outputs` | `tests/test_client.py::test_a_screen_plugged_in_is_reported_at_once_and_an_unchanged_one_is_not_repeated` |
| Journal | One JSON object per line; each trouble said once when it starts and once when it ends | `logs.configure`, `episodes.ReportOnce` | `tests/test_logs.py`, `tests/test_episodes.py` |

A heartbeat is not queued while the server is unreachable. The newest file is
posted when the server answers again.

## Label legibility

The requirement and its calibration are Arrt's `accessibility-spec.md` § The
e-paper label, notably § The type floor is derived from viewing distance, not
chosen for a panel, and § The panel must be driven at 16 grey levels. The code
here commits to:

- **Type size is derived, never chosen.** `panel/legibility.type_scale_for`
  converts the panel's diagonal and the viewing distance into pixels, against
  `COMFORTABLE_CAP_ARCMIN` = 12.4′ for the primary tier and
  `MINIMUM_CAP_ARCMIN` = 8.8′ as the floor, with `CAP_RATIO` = 0.70. The border
  derives from the type (`margin_for`, `MARGIN_TO_PRIMARY_RATIO` = 0.25) unless
  `EPD_MARGIN_PX` is set.
- **The two physical inputs have no default.** `EPD_PANEL_DIAGONAL_INCHES` and
  `EPD_VIEWING_DISTANCE_INCHES` unset or ≤ 0 raise `ViewingConditionsUnknown`,
  which costs the label and not the wall (above). The derived sizes are logged
  once at start (`panel.type_scale`).
- **16 grey levels, read back.** `panel/epaper.GREYSCALE_MODE` = `gray16` is set
  and then read back from the driver's `mode`.
- **Tests:** `tests/test_type_floor.py` asserts in arcminutes;
  `tests/test_label_*.py` cover layout, metadata and styling; `tests/raster`
  typesets with Pango into memory and runs in CI as its own `typesetting` job
  (`.github/workflows/suites.yml`), which fails if no test ran.
- **What no test can settle** is legibility on the panel at the wall. That is
  `tools/label_preview.py --panel`, run by hand
  (`deploy/README.md` § Looking at the label without the daemon).

## Credentials

The Player holds two secrets: the client token (`CLIENT_TOKEN`, in `.env`) and
the television's pairing token (`TV_TOKEN_FILE`, default `$CACHE_DIR/token_file`,
`config._frame`). What the code commits to:

- The client token is sent only to the server's own origin; a master named on
  another host is fetched without it (`pull._same_origin`, `Pull._cache_media`).
- The television library logs a new token at INFO. Its logger is held at WARNING
  or above (`logs.configure`, `_TELEVISION_CLIENT_FLOOR`).
- No credential file is tracked (`tests/test_repository.py`).

## Cost Constraints

None to state. The Player calls no metered service. Its costs are the Pi, its
power and its SD card.

## Discrepancies with Arrt's record

Arrt's text about the Player that is now wrong for this repository. None is
fixed here; each is a change on Arrt.

1. **Paths.** Arrt's `nonfunctional-requirements.md` § Direction and § The
   television belongs to whoever is using it cite `arrt-player/tests/test_pull.py`,
   `arrt-player/tools/power_probe.py` and `arrt-player/src/arrt_player/tv/`. The
   Player is now its own repository; the paths are `tests/`, `tools/` and
   `src/arrt_player/tv/` here.
2. **"`src/arrt_player/tv/` opens the art channel only and sends no key"**
   (Arrt § The television belongs to whoever is using it, Retroactivity). It
   sends no key. It does not open the art channel only: constructing the art
   client reads the set over REST, and on an empty token file opens the
   remote-control channel (`tv/samsung.SamsungTv._construct`).
3. **The heartbeat still writes to the card.** Arrt's `operational-spec.md`
   § Risks says that after wave 3 the heartbeat "becomes an HTTP POST and stops
   writing to the card at all". The Player still writes each wall's heartbeat
   file into `CACHE_DIR/<wall id>/`, with an `fsync` (`safe_write`), at least every 60 s, and the
   pull posts that file (`heartbeat.write`, `pull.Pull._heartbeats`). The
   card still takes that write.
4. **Interpreter.** Arrt's `operational-spec.md` § Deployment Target gives the
   display process as Python 3.13 on the system interpreter. The unit runs
   `/usr/local/bin/uv run`, `pyproject.toml` requires `>=3.12`, and
   `hdmi-output-findings.md` records uv's CPython 3.14 on the Pi.
