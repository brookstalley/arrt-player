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
directly: **the display plane's ability to show art never depends on the
curation plane being reachable** (Arrt § Direction, read "server" and "Player"
per its 2026-09-30 amendment), and **the television belongs to whoever is using
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
`TV_ADDRESS` is set) and at most one label output (`config.PANEL_OUTPUT`,
`epd-0`, present only when `EPD_DEVICE` is set). HDMI outputs are whatever the
kernel lists under `/sys/class/drm` (`client.hdmi_outputs`).

**Cache size is bounded by the feeds, not by a setting.** Per wall, the media
directory holds the masters named by the feed just adopted and by the one before
it; everything else is removed at adoption (`pull.Pull._evict`, called from
`_adopt_feed` with `held | previous`). Tested in
`tests/test_pull.py::test_a_feeds_media_is_evicted_once_two_documents_in_a_row_have_not_named_it`.
Composed pictures are removed when no work composes to them
(`programmes/schedule`, the tidy). What the code does not do, checked by
searching `src/` for `rmtree`, `rmdir`, `disk_usage` and `statvfs`:

- **No free-space check and no size cap.** A full disk surfaces as a failed write.
- **A wall taken away from this client keeps its directory.** Its masters,
  composed pictures and Frame store stay under `CACHE_DIR/<wall id>/`. Growth is
  bounded by the number of walls ever assigned.

**Memory has no limit.** `deploy/display.service` sets no `MemoryMax=`, and no
code bounds it. The only sized consumer is composition (above). Pillow's default
decompression-bomb guard applies; `compose.py` catches `DecompressionBombError`
as an uncomposable master.

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
checked against its hash, or answered for. Every failure keeps the cache
(`player-contract.md` § Transport). Tested in `tests/test_pull.py`
(`test_the_server_stopped_while_the_wall_runs_and_the_schedule_continues_from_the_cache`,
`test_a_player_restarted_with_the_server_down_starts_from_its_cache`),
`tests/test_client.py`
(`test_a_client_restarted_while_the_server_is_down_starts_the_walls_it_last_knew`)
and `tests/test_availability.py`.

| Request | Timeout | Retry | Where |
|---|---|---|---|
| Wall manifest, masters, wall heartbeat | 60 s total, 10 s connect, per request | Backoff from 2 s, doubling to 60 s, reset when the manifest route answers | `pull.Pull.run` (`aiohttp.ClientTimeout`), `BACKOFF_START_SECONDS`, `BACKOFF_MAX_SECONDS` |
| A higher manifest major | — | Asked again every 60 s while a lower one answers | `pull.HIGHER_MAJOR_SECONDS` |
| Client document and client heartbeat | 30 s total, 10 s connect | None; asked again on the next 30 s client poll | `pull.ClientPull._client` |
| Label document | 10 s total, 5 s connect | None; asked again on the next 1 s label poll | `pull.LabelPull._client` |

While the server cannot be reached, a label holds its last caption for 30
minutes (`label_rule.HOLD`), then follows the rule in `labels-and-surfaces.md`
§ What a label says.

**One cache failure is not kept local to the cache.** A write that fails while
adopting a wall's feed (`pull._write_atomically` inside `Pull._adopt_feed`) is not
caught there. It ends that wall's pull, which ends the wall's worker
(`__main__._pull_ended`); the supervisor starts it again (below). The previous
manifest survives, because the write is temp-and-rename. The client document's
write failure, by contrast, is caught and reported once (`ClientPull.fetch`,
`client.cache_unwritable`).

### The process comes back on its own

| Layer | Behaviour | Where | Enforced by |
|---|---|---|---|
| systemd | `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0` (no start rate limit) | `deploy/display.service` | `tests/test_deploy_unit.py::test_the_unit_cannot_silently_stop_retrying` |
| One wall's worker | Restarted after a wait from 5 s doubling to 300 s; the ladder resets after a run longer than 300 s | `client.RESTART_MIN_SECONDS`, `RESTART_MAX_SECONDS`, `Supervisor._keep_running` | `tests/test_client.py::test_a_crashed_worker_is_logged_and_started_again` |
| A wall's pull | Its death ends its wall's worker at once, so the two restart together | `__main__._beside_its_pull`, `_pull_ended` | `tests/test_main.py::test_a_pull_that_dies_ends_its_walls_worker_at_once_and_says_why` |
| The supervisor | Not caught. The process ends and systemd restarts it | `client.py` module docstring | — |
| A Frame store from a newer Player | That wall is parked and says so once; other walls run | `__main__.run_frame_wall`, `state.StateSchemaTooNew` | `tests/test_main.py::test_a_store_from_a_newer_plane_parks_its_wall_and_says_so_once` |

### Start-up

`config.load` refuses to start, naming the variable, when `SERVER_URL`,
`CLIENT_TOKEN` or `CACHE_DIR` is unset; when `CACHE_DIR` or `TV_TOKEN_FILE` is
not absolute; when a numeric value does not parse; and when a retired setting
(`WALL_ID`, `WALL_TOKEN`, `MANIFEST_SOURCE`) is set (`config.RETIRED_SETTINGS`).
`__main__.main` turns that `ConfigError` into exit status 2 and a line on stderr
(`tests/test_main.py::test_a_missing_deployment_value_refuses_to_start_and_says_so_at_the_terminal`).
Under `Restart=always` that is a restart every 5 s until `.env` is fixed, which
is what keeps it visible.

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
  waits for each to close what it holds, with no timeout of its own. The panel is
  closed after that (`__main__._run`, `finally`).
- The bound is systemd's: `TimeoutStopSec=90`, `KillSignal=SIGTERM`
  (`deploy/display.service`). The unit's comment gives the worst case it must
  clear as about 45 s; `tests/test_deploy_unit.py::test_the_unit_gives_the_television_time_to_let_go`
  asserts `TimeoutStopSec` ≥ 45 and `KillSignal=SIGTERM`.
- Tested end to end: `tests/test_client.py` runs `_run`, sends the process
  SIGTERM, and asserts exit 0 within 5 s with the art channel closed.
- The one known case past 90 s is a panel driver wedged in SPI on a worker
  thread. The unit's comment records it as left to SIGKILL, after the television
  is closed.

### The television belongs to whoever is using it

The requirement is Arrt § Availability › The television belongs to whoever is
using it. What this repository's code enforces is narrower than "never touch a
set that is not in art mode":

- **Selection is gated.** The schedule asks `display.is_ours()` before every
  `show` (`programmes/schedule.Schedule`, the `may_attempt` and `is_ours` check before `display.show`),
  and the Frame answers from a fresh `get_artmode` read
  (`displays/frame.FrameDisplay.is_ours`). A no selects nothing and backs off on
  `TV_RETRY_MIN_SECONDS` 5 to `TV_RETRY_MAX_SECONDS` 300
  (`config.DEFAULT_TV_RETRY_*`). Tested in `tests/test_frame_wall.py`
  (`test_a_television_somebody_is_watching_is_left_alone`,
  `test_an_evening_of_television_ends_on_the_slots_work_in_one_selection`).
- **Other calls to the set are not gated on art mode.** Connecting
  (`FrameDisplay._connected`), switching off the set's own slideshow, reconciling
  and removing orphans, uploading, and setting brightness every 300 s when the
  value changes (`FrameDisplay._apply_brightness`) all run whatever the set is
  showing. Connecting includes a REST read on every construction and, **only when
  the token file is empty**, an open of the remote-control channel to mint a
  token, which raises a pairing prompt on the screen (`tv/samsung.SamsungTv._construct`;
  `samsung-tv-state-findings.md` § This product has been opening the
  remote-control channel all along). Whether any of the others is visible over a
  programme has not been measured here.
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
| Wall heartbeat POST | On the pull's next cycle after the file changes, and only when the manifest route answered | `pull.Pull._forward_heartbeat`, `Pull.cycle` | `tests/test_pull.py::test_the_heartbeat_file_is_posted_when_it_holds_a_new_report_and_not_otherwise` |
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
   file into `CACHE_DIR/<wall id>/`, with an `fsync`, at least every 60 s, and the
   pull posts that file (`heartbeat.write`, `pull.Pull._forward_heartbeat`). The
   card still takes that write.
4. **Interpreter.** Arrt's `operational-spec.md` § Deployment Target gives the
   display process as Python 3.13 on the system interpreter. The unit runs
   `/usr/local/bin/uv run`, `pyproject.toml` requires `>=3.12`, and
   `hdmi-output-findings.md` records uv's CPython 3.14 on the Pi.
