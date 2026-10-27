---
artifact: observability-strategy
version: 1
depends_on:
  - artifact: project-state
  - artifact: architecture
last_validated: 2026-10-27
---

# Observability Strategy — Arrt Player

**What this covers.** What the Player says about itself, where it goes, and how
an operator finds a failure from it. It describes the code as it stands on
`feature/discovery` at 2026-10-27; every claim names the `file:symbol` it rests
on.

**Why it matters more here than usual.** The Player runs unattended and its only
output is a picture. A stalled Player looks exactly like a working one showing a
still image (`logs.py` module docstring; `project-state.yaml`, "Failure is silent
by construction"). So it reports two ways: structured lines in the journal, and
heartbeats the server shows.

**What it points at instead of restating.** Arrt owns the reader of every signal
the Player sends, and the rules both sides share:

- Arrt's `observability-strategy.md` § The Health Surface: what the server does
  with a heartbeat, and the panel that shows its age.
- Arrt's `observability-strategy.md` § A client, and a wall on its screen: the two
  levels of report, and the server's view of them.
- Arrt's `observability-strategy.md` § Sensitive Data in Logs: the rule that no
  secret reaches a log line, which binds both repos.
- Arrt's `observability-strategy.md` § Accepted detection latency, stated as a
  number, and § What Each Failure Looks Like: the failure-to-signal table.
- The heartbeat documents are `heartbeat.v1` and `client-heartbeat.v1` in the
  `arrt_contract` package; `player-contract.md` § The heartbeat (minors 2–4)
  is their specification.

<!-- OPTIONAL norm home. No norms are ratified here. "No secret ever reaches a
     log line" is in project-preferences.md § Enforcement. -->

## What You Get

1. **When a wall stops changing**, the server's view of the wall heartbeat says
   whether the set is reachable, what the screen is doing (`display_state`) and
   the last error; the journal says why, in one line per episode
   (`tv.unavailable`, `rotation.wall_unchanged`, `rotation.wall_not_ours`).
2. **When the Player stops entirely**, every heartbeat it sends stops ageing
   forward on the server. On an HDMI screen the kernel hands the screen back to
   the text console (Arrt's `clients.md` § The Player).
3. **When the server is unreachable**, the walls keep rotating and the journal
   says so once when it starts and once when it ends (`pull.unreachable` /
   `pull.reachable`, `client.unreachable` / `client.reachable`,
   `label.server_unreachable` / `label.server_reachable`).
4. **When a wall or label worker crashes**, the journal has an ERROR with the
   traceback (`client.worker_crashed`, `client.label_crashed`,
   `daemon.crashed`, `screen.crashed`, `pull.crashed`) and the worker restarts.
5. **When the label is wrong or unreadable**, the journal says what was drawn and
   what had to give (`label.drawn`, `label.shrunk`, `label.name_wrapped`,
   `label.truncated`, `label.unusable`), and the client heartbeat says whether
   the panel is connected.

## Architecture

```
  Player process ──stderr──> systemd journal (SyslogIdentifier=display)
       │                       └─ journalctl -u display -o cat | jq
       ├─ wall loop ──file──> CACHE_DIR/<wall>/display-heartbeat-<wall>.json
       │                       └─ pull ──POST /walls/{id}/heartbeat──> server
       └─ supervisor ──POST /client/heartbeat──> server
                                                   └─ Arrt's health view
```

The journal is local to the Pi. Nothing is exported, collected or aggregated
(`logs.py` docstring: "there is no collector and no metrics store"). The
journal is bounded at 256 MB by `deploy/journald.conf.d/10-bound-the-journal.conf`.

## Signal Types

### Logging

**Shape.** One JSON object per line, written to stderr by a `StreamHandler` on
the root logger (`logs.configure`, `logs.JsonFormatter`). Every line has `time`
(UTC ISO 8601), `level`, `logger` (the module's `__name__`), `message`; then
`work_id` when one is bound; then every field the call site passed in `extra`;
then `exception` (the formatted traceback, one field) when there is one. A value
that will not serialise is rendered with `str` (`json.dumps(…, default=str)`).
`tests/test_logs.py` holds the one-object-per-line shape, the `work_id`
binding, nesting, and that `configure` twice does not duplicate lines.

**Every call site carries an `event`.** Checked by walking every
`log.debug/info/warning/error/exception` call in `src/arrt_player` with `ast`:
109 call sites, each with an `extra` dict literal holding an `event` key, and no
logger other than `logging.getLogger(__name__)` bound to `log` (16 modules). No
test enforces this; it holds today by that walk.

**Levels, as the code uses them** (derived from the inventory below, not
prescribed anywhere):

| Level | Meaning in this product |
|---|---|
| ERROR | Will not fix itself: a deployment value, a refusal by the server, a document the Player will not read, an assignment it cannot place, a crash (with traceback) |
| WARNING | A degraded condition that may pass, or a device the operator must look at: server or set unreachable, a draw refused, a label set below the legibility floor |
| INFO | A state change or the end of an episode: started, adopted, selected, drawn, recovered |
| DEBUG | One call site, `tv.close_failed`. Never emitted: `__main__.main` calls `logs.configure()` at INFO |

**Episodes, not repetition.** A condition that persists is logged when it starts
and when it ends, through `episodes.ReportOnce` (`begin()` / `end()`). This is
why most WARNING/ERROR events below have a paired INFO. The exceptions, logged
on every occurrence because each is about a different work: `label.shrunk`,
`label.name_wrapped`, `label.truncated`, `rotation.selected`, `label.drawn`.

**The event inventory.** Derived, not remembered: an `ast` walk of every log call
in `src/arrt_player` (event, level, enclosing function), plus the five places an
event name is built rather than written: `client.Supervisor._keep_running`
(`f"{event}_crashed"`, `f"{event}_ended"`, with `event` = `client.worker` or
`client.label`), `wall.Wall.run` (`f"{name}.started/stopped/crashed"`, with
`name` = `FrameDisplay.journal_name` `daemon` or `ScreenDisplay.journal_name`
`screen`), and `programmes/schedule.py:Schedule._collect` (a variable set to one
of three names). **114 distinct names.** A grep for `"event": "` alone finds 101
and misses those 13.

| Events | Level | Emitted by | Says |
|---|---|---|---|
| **Process** | | | |
| `daemon.misconfigured` | ERROR | `__main__.main` | A `.env` value is missing or wrong; exit 2, also on stderr |
| `client.started` / `client.stopped` | INFO | `client.Supervisor.run` | Up, with server URL, cache dir, Frame and panel settings (`ClientSettings.startup_lines`); down |
| **Supervisor** | | | |
| `client.wall_started` / `client.wall_stopped` | INFO | `Supervisor._start`, `_stop` | A wall assigned to an output, or taken away or moved |
| `client.wall_unplaceable` | ERROR | `Supervisor._unplaceable` | Assigned to an output this client lacks or that another wall holds, or a wall id that is not a plain name; once per (wall, output) |
| `client.label_started` / `client.label_stopped` | INFO | `Supervisor._start_label`, `_stop_label` | A label output mapped, remapped or unmapped |
| `client.label_unplaceable` | ERROR | `Supervisor._label_unplaceable` | Mapped to a label output this client lacks, or a second label on one output |
| `client.worker_crashed` / `client.label_crashed` | ERROR (traceback) | `Supervisor._keep_running` | A worker raised; restarting after the backoff |
| `client.worker_ended` / `client.label_ended` | ERROR | `Supervisor._keep_running` | A worker returned without being asked to |
| `client.identity_unreadable` / `client.identity_read` | WARNING / INFO | `client.FrameIdentity.refresh` | The Frame's device id could not be read, then could |
| **Client routes** (`pull.ClientPull`) | | | |
| `client.unreachable` / `client.reachable` | WARNING / INFO | `ClientPull._report_unreachable`, `fetch` | `GET /client` got no answer, a timeout or a 5xx |
| `client.refused` / `client.accepted` | ERROR / INFO | `ClientPull.fetch` | The server refused the client (with `status`) |
| `client.document_refused` / `client.document_readable` | ERROR / INFO | `ClientPull.fetch` | A client document this reader will not act on |
| `client.cache_unwritable` / `client.cache_writable` | ERROR / INFO | `ClientPull.fetch` | `.client.json` could not be written; the document is still followed |
| `client.cache_unreadable` | WARNING | `ClientPull.cached` | The cached client document could not be read at start |
| `client.heartbeat_refused` / `client.heartbeat_ok` | WARNING / INFO | `ClientPull.report` | The server refused the client heartbeat |
| **A wall's pull** (`pull.Pull`) | | | |
| `pull.started` | INFO | `Pull.run` | The wall's pull began, with `wall_id`, server, cache dir |
| `pull.unreachable` / `pull.reachable` | WARNING / INFO | `Pull._report_unreachable`, `_report_reachable` | The feed route got no answer, a timeout or a 5xx |
| `pull.refused` / `pull.accepted` | ERROR / INFO | `Pull._report_refused`, `_report_accepted` | 401 (token), 403 (not this client's wall), 404 at every major, or another status; once per status |
| `pull.manifest_refused` / `pull.manifest_readable` | ERROR / INFO | `Pull._pull_manifest` | A feed the parser refuses; the cached one stays |
| `pull.media_failing` / `pull.media_ok` | WARNING / INFO | `Pull._adopt_feed` | A master could not be fetched; the feed is held back |
| `pull.work_skipped` | WARNING | `Pull._skip_once` | A master 404s or fails its hash; that work is skipped (once per hash) |
| `pull.adopted` | INFO | `Pull._adopt_feed` | A new feed cached, with `works` and `media_held` |
| `pull.heartbeat_refused` / `pull.heartbeat_ok` | WARNING / INFO | `Pull._forward_heartbeat` | The server refused the wall heartbeat (with `status`) |
| `pull.crashed` | ERROR (traceback) | `__main__._pull_ended` | The pull raised; its wall's worker is ended to restart both |
| **A wall's loop** | | | |
| `daemon.started` / `daemon.stopped` / `daemon.crashed` | INFO / INFO / ERROR | `wall.Wall.run` (Frame) | A Frame wall loop started (with its settings), stopped, or raised |
| `screen.started` / `screen.stopped` / `screen.crashed` | INFO / INFO / ERROR | `wall.Wall.run` (HDMI) | The same for an HDMI wall |
| `daemon.state_too_new` | ERROR | `__main__.run_frame_wall` | The Frame store is from a newer Player; this wall is parked |
| `display.state` | INFO | `wall.DisplayRecord.moved_to` | The screen's state changed (`showing_art`, `in_use`, `dark`, `no_screen`, `unreachable`) |
| `heartbeat.failed` / `heartbeat.recovered` | WARNING / INFO | `wall.Wall._beat` | The heartbeat file could not be written |
| `manifest.adopted` | INFO | `manifest.Watcher.poll` | The loop adopted a cached feed (theme, works, scene, schema) |
| `manifest.absent`, `manifest.statable` / `manifest.unstatable`, `manifest.unreadable` | INFO, INFO / WARNING, WARNING | `Watcher.poll` | No cached feed yet; the file could not be stat'd or read |
| `manifest.not_text`, `manifest.refused`, `manifest.version_refused` | ERROR | `Watcher.poll` | A cached feed that is not UTF-8, does not parse, or has a major this Player does not read |
| `schedule.media_missing` | WARNING | `Schedule._ready` | A work's master is not in the cache yet |
| `schedule.uncomposable`, `schedule.compose_write_failed`, `schedule.compose_failed` | WARNING | `Schedule._collect` | A work could not be composed for this display |
| `schedule.composed_removed`, `schedule.tidy_failed` / `schedule.tidy_recovered` | INFO, WARNING / INFO | `Schedule._remove_unwanted`, `_say_tidy_failed` | Old compositions removed, or could not be |
| `compose.done` | INFO | `compose.compose` | A picture composed (`composed_path`) |
| **The Frame** (`displays/frame.py:FrameDisplay`, `tv/samsung.py:SamsungTv`) | | | |
| `tv.unavailable` / `tv.recovered` | WARNING / INFO | `FrameDisplay.went_away`, `answering` | The set cannot be reached; the wall holds |
| `rotation.wall_not_ours` / `rotation.wall_returned` | INFO | `FrameDisplay.is_ours` | The set is not in art mode, so nothing is sent; then it is again |
| `display.power_unreadable` / `display.power_readable` | INFO | `FrameDisplay._screen_is_not_ours` | `PowerState` could not be read; `in_use` is reported |
| `rotation.selected` | INFO | `FrameDisplay.show` | A work is on the wall, confirmed by the set (`tv_content_id`, `theme_id`) |
| `rotation.wall_unchanged` / `rotation.wall_recovered` | WARNING / INFO | `FrameDisplay._report_wall_unchanged`, `show` | The set accepted a selection and displayed nothing (`art_mode`) |
| `binding.uploaded`, `binding.upload_failed` | INFO, WARNING | `FrameDisplay._upload` | A work uploaded to the set, or not |
| `binding.orphaned` | WARNING | `FrameDisplay._rebind_or_reraise`, `_reconcile_with_the_set` | The set no longer lists a content id the store has |
| `tv.orphans_removed`, `tv.orphan_removal_unconfirmed` | INFO, WARNING | `FrameDisplay._remove_orphans` | Unaccounted images removed from the set, or the outcome unknown |
| `tv.removal_incomplete` | WARNING | `SamsungTv.remove` | Some images survived a removal |
| `tv.upload_misreported` | WARNING | `SamsungTv.upload` | The library's upload reply disagreed with what the set holds |
| `tv.selection_unreadable` | WARNING | `SamsungTv._on_image_selected` | An `image_selected` announcement could not be read |
| `tv.selection_observer_failed` | ERROR (traceback) | `SamsungTv._tell_observers` | An observer of selections raised; the others still ran |
| `tv.native_slideshow_disabled` | INFO | `FrameDisplay._connected` | The set's own slideshow was switched off (once per store) |
| `tv.close_failed` | DEBUG | `SamsungTv._quietly_close` | Closing the connection raised. Not emitted at the configured level |
| `brightness.set` | INFO | `FrameDisplay._apply_brightness` | Brightness set from the sun's angle |
| **HDMI** (`kms.py:KmsOutput`, `displays/screen.py:ScreenDisplay`) | | | |
| `screen.absent` / `screen.returned` | WARNING / INFO | `KmsOutput._gone`, `_draw` | No screen on the connector, then one (drawn at once) |
| `screen.draw_failed` / `screen.draw_recovered` | WARNING / INFO | `ScreenDisplay.show` | The output refused a picture |
| `screen.refresh_failed` / `screen.refresh_recovered` | WARNING / INFO | `ScreenDisplay._refresh` | The per-poll redraw failed |
| `rotation.selected` | INFO | `ScreenDisplay.show` | A work drawn (`picture_path`) |
| **The label** (`__main__`, `label_renderer.py`, `pull.LabelPull`, `panel/epaper.py`) | | | |
| `panel.type_scale` | INFO | `__main__.label_surface` | The derived type size, floor and border, once at start |
| `panel.unavailable` | WARNING | `__main__.label_panel` | A configured panel will not open, has no text stack, or has no viewing distance; walls unaffected |
| `panel.size_disagrees` | WARNING | `EpaperSurface._warn_if_the_panel_disagrees_about_its_size` | The driver reports a size other than `.env`'s |
| `panel.close_failed` | WARNING | `EpaperSurface.close` | The panel's power-down raised |
| `label.renderer_started` | INFO | `LabelRenderer.run` | A renderer captions a wall (`label_id`, `output`, `wall_id`) |
| `label.server_unreachable` / `label.server_reachable` | WARNING / INFO | `LabelPull` | The label route got no answer, a timeout or a 5xx |
| `label.refused` / `label.accepted` | ERROR / INFO | `LabelPull.fetch` | The server refused the label (with `status`) |
| `label.document_refused` / `label.document_readable` | ERROR / INFO | `LabelPull.fetch` | A label document this reader will not use |
| `label.drawn` | INFO | `LabelPanel.draw` | The only positive label line: the panel captions a work |
| `label.blanked`, `label.card`, `label.absent` | INFO | `LabelPanel.draw` | Blank on purpose (with `display_state`), the wall's card, or a work with no label text |
| `label.failed` / `label.recovered` | WARNING / INFO | `LabelPanel._would_not_take_it`, `draw` | A drawing did not reach the panel (refused, raised, still drawing, or over the 15 s budget) |
| `label.unusable` | WARNING | `LabelPanel.draw` | The geometry leaves no usable area; once per episode |
| `label.shrunk` | WARNING | `LabelPanel.draw` | Identifying facts set below the legibility floor (`floor_px`, `smallest_px`) |
| `label.name_wrapped` | WARNING | `LabelPanel.draw` | The line breaker split a name across rows |
| `label.truncated` | INFO | `LabelPanel.draw` | Lines dropped for room (`dropped`) |

**Destinations and retention.** stderr, captured by systemd
(`deploy/display.service`: `StandardOutput=journal`, `StandardError=journal`,
`SyslogIdentifier=display`). Retention is journald's, capped at 256 MB system and
runtime (`deploy/journald.conf.d/10-bound-the-journal.conf`). On a dev machine
the same JSON goes to the terminal.

### Heartbeats

These are the Player's state signal, sent to the server. Arrt's health view is
their reader; Arrt § The Health Surface owns what it does with them.

**The wall heartbeat**, one per wall, built by `wall.Wall._beat` as
`heartbeat.Health` and written by `heartbeat.write` to
`CACHE_DIR/<wall_id>/display-heartbeat-<wall_id>.json`. It is written once per
`heartbeat.INTERVAL_SECONDS` (60 s), and at once when the display state or the
capabilities change. It is written on every path through `Wall.tick`, including
a pass with no feed and a pass where the display was unreachable
(`tests/test_heartbeat_wiring.py`). `pull.Pull._forward_heartbeat` POSTs it when
the file and its `reported_at` change, and only after a feed poll that reached
the server.

| Key (`Health.document`) | What problem it shows |
|---|---|
| `schema` | `1.4` (`heartbeat.SCHEMA_MAJOR`, `SCHEMA_MINOR`) |
| `reported_at` | Its age is the liveness signal: a heartbeat that stops advancing is a stopped loop or a stopped pull |
| `manifest_schema`, `theme_id` | A wall sitting on an old feed (the pull's refusals are not otherwise in the heartbeat) |
| `current_work_id`, `scene_id` | What this wall last confirmed showing |
| `announced_content_id` | Frame only: what the set last announced, the set's own word |
| `television_reachable` | Frame only: `false` is network or pairing (`FrameDisplay.heartbeat_fields`) |
| `television_showing_art` | Frame only: `false` with the set awake means somebody is watching, and the Player is leaving it alone |
| `display_state` | `{state, work_id, since}`. Frame: `showing_art`, `in_use`, `dark`, `unreachable`. HDMI: `showing_art`, `dark`, `no_screen` (`wall.DisplayRecord`) |
| `capabilities` | Backend, label modes, `manifest_majors`, and `screen` when the size is known |
| `last_error` | The latest failure the display recorded, not cleared by a good pass. **Frame: set only by `FrameDisplay.went_away`** (set unreachable). **HDMI: set by `ScreenDisplay.show` and `_refresh`**. Pull failures and worker crashes never reach it |
| `has_label_surface`, `label_surface_working` | Always `false` / `null` now: the panel is the client's (`FrameDisplay.heartbeat_fields` and `Health`'s defaults) |

**The client heartbeat**, one per client, built by `client.client_heartbeat` and
sent by `Supervisor._report_outputs` on each client poll when the outputs changed
or 60 s have passed. It carries `reported_at`, `outputs` (each `name`, `kind`,
`connected`, `screen`, and `identity` for a Frame once read) and, when the client
has a panel, `label_outputs` (each `name`, `kind`, `connected`, `size`).

- An HDMI connector with nothing plugged in is `connected: false`
  (`client.hdmi_outputs`).
- The Frame is always `connected: true` when configured; whether it answers is
  in the wall heartbeat (`client.client_outputs`).
- A panel is `connected: false` when it would not open or its last draw failed
  (`label_renderer.LabelPanel.report`). **Why** it would not open is not sent:
  `LabelPanel.error` is held and never reported. The journal's
  `panel.unavailable` is the only record.

### Metrics

None. No counter, gauge or time series exists in `src/` (searched for
`prometheus`, `statsd`, `opentelemetry`: nothing). The heartbeat's age is the one
quantity the server derives.

### Tracing

Not applicable. Each request is one hop with no fan-out; Arrt's
`observability-strategy.md` § Signals, the tracing row, says the same for the
server side.

## Instrumentation Layers

Layer 1 (automatic): systemd captures stderr and restarts. Layer 3 (contextual):
`work_id` is bound by context variable (below). Layer 4 (manual): every event
above is a hand-placed call. There is no layer 2.

## Correlation Context

- **`work_id`, bound, not passed.** `logs.work_context` sets a `ContextVar`;
  `logs.WorkCorrelationFilter` stamps it on every record. Bound at
  `displays/frame.py:FrameDisplay.show` (around a selection), `programmes/schedule.py`
  (three sites: a step, a composition, a collected composition) and
  `label_renderer.LabelRenderer._show`. Because it is a `ContextVar`, it follows
  into `asyncio.to_thread` work, which copies the context. It is the one id that
  spans the Player and the server (Arrt § Correlation).
- **`wall_id`** is an `extra` field, not bound. 24 of the 109 call sites carry
  it by the same `ast` walk: 21 as a literal key, three through a spread dict
  (`Supervisor._keep_running`'s `extra`, twice, and `Wall.run`'s
  `start_lines()`). Which ones do not, and why that matters, is under
  Known gaps.
- **`label_id`, `output`, `tv_content_id`, `status`, `server_url`** are carried
  where the call site has them.
- **Task names are set and then dropped.** Workers run in tasks named
  `wall:<id>`, `pull:<id>`, `label:<output>`, `compose:<wall>:<work>`
  (`client.Supervisor._start`, `_start_label`; `__main__._beside_its_pull`;
  `Schedule._start`), but `logs._BUILT_IN` lists `taskName` among the attributes
  the formatter leaves out.
- No request id. Nothing crosses to the server but the wall id in the path.

## Sensitive Data Filtering

Arrt § Sensitive Data in Logs is the rule. The two secrets on the Player are
`CLIENT_TOKEN` and the Frame's pairing token (`project-state.yaml`,
`handles_sensitive_data`). What keeps each out:

| Secret | Mechanism | Held by |
|---|---|---|
| `CLIENT_TOKEN` in a settings repr | `field(repr=False)` on `WallSettings.client_token` and `ClientSettings.client_token` | `tests/test_config.py::test_the_client_token_is_in_no_repr_and_no_startup_line` |
| `CLIENT_TOKEN` in a startup line | `ClientSettings.startup_lines`, `WallSettings.wall_lines`, `FrameSettings.frame_lines` list their fields by hand and omit it | the same test |
| `CLIENT_TOKEN` in a request log | Structural: no call site logs a header. Failures are logged by exception type and message (`Pull._pull_manifest`), which `aiohttp` builds from the URL, not the headers | Critic only |
| `CLIENT_TOKEN` sent elsewhere | Not attached to a media URL on another origin (`Pull._cache_media`, `pull._same_origin`) | `tests/test_pull.py` (a second `TestServer` as the other origin) |
| The TV token, logged by `samsungtvws` at INFO (`New token %s`) | `logs.configure` floors the `samsungtvws` logger at WARNING | `tests/test_logs.py::test_a_token_logged_at_info_by_that_logger_is_not_emitted` and its DEBUG sibling |
| The TV token's value in the startup line | Only its path is logged (`FrameSettings.frame_lines`, `tv_token_file`) | — |
| A token file in git | — | `tests/test_repository.py::test_no_credential_file_is_tracked` |

Tracebacks go in the `exception` field; `Formatter.formatException` prints no
local variables. Nothing else the Player logs is personal: works, walls, file
paths and the server's URL.

## Alerting

None on the Player. It sends nothing to a person. The server's health view,
reading heartbeat age, is the operator's alert surface; Arrt § Accepted detection
latency is its budget. The journal is read on demand.

## Health Signals

- **Healthy**: each wall heartbeat's `reported_at` advances about once a minute,
  `display_state` is `showing_art` with the expected `current_work_id`, the
  client heartbeat is recent, and the journal's last ERROR is older than the last
  restart.
- **Degraded, by design**: server unreachable (walls rotate from cache, wall
  heartbeats stop reaching the server and age there); set asleep or in use
  (`television_reachable: false`, or `display_state` `dark` / `in_use`); panel
  broken (`label_outputs[].connected: false`).
- **Worse than an error**: a `reported_at` that does not advance with a `null`
  `last_error` means the loop is not running (`deploy/README.md`,
  "Read the heartbeat rather than the wall").
- No health endpoint. The Player serves nothing.

## Infrastructure and Deployment

systemd and journald only, configured by `deploy/display.service` and
`deploy/journald.conf.d/10-bound-the-journal.conf`. Always on; nothing to
configure off. No cost.

## Agent-Accessible Observability

Not applicable beyond reading. The journal is JSON, so an agent with a shell on
the Pi reads it the way an operator does:

```sh
journalctl -u display -o cat --since -1h | jq 'select(.level == "ERROR")'
journalctl -u display -o cat | jq 'select(.work_id == "…")'
journalctl -u display -o cat | jq 'select(.event | startswith("pull."))'
cat "$CACHE_DIR"/<wall_id>/display-heartbeat-<wall_id>.json
```

The `tools/` scripts drive hardware and are not observability tools.

## Verification

- **The shape** is held by `tests/test_logs.py` (JSON per line, `work_id`,
  the `samsungtvws` floor).
- **The heartbeat documents** are validated against the contract's schemas:
  `heartbeat.v1` in `tests/test_player_contract.py` and
  `tests/test_display_state.py`; `client-heartbeat.v1` there and on every POST
  to `tests/server_double.py:ServerDouble.receive_client_heartbeat`. The wall
  heartbeat POST to the double (`ServerDouble.receive_heartbeat`) is not
  validated. `tests/test_heartbeat_key.py` pins `reported_at` in both schemas.
- **When the heartbeat is written** is held by `tests/test_heartbeat_wiring.py`.
- **The event names are not pinned as a set.** 55 of the 114 names appear
  nowhere in `tests/` as a quoted literal (checked by grepping each), among them
  `client.started`, `client.wall_started`, every `pull.*` but `pull.crashed`,
  and `label.drawn`'s siblings `label.renderer_started` and
  `label.server_unreachable`. A test may still reach some through a message
  match or a built string; a rename of any of them is likely to pass the suite.
- **On the Pi**, `deploy/README.md` § How to tell your own install worked is the
  operator's check, with the caveat below.

## Known gaps

Observed in the code while writing this; none is fixed here.

1. **Most `pull.*` lines do not say which wall.** With several walls in one
   process, `pull.unreachable`, `pull.refused`, `pull.manifest_refused`,
   `pull.media_failing`, `pull.adopted`, `pull.work_skipped` and
   `pull.heartbeat_*` carry no `wall_id`, and their logger is the same
   `arrt_player.pull` for every wall. `pull.started` and `pull.crashed` do. A
   403 (`pull.refused`) is by definition about one wall. The same holds for
   `screen.draw_failed` / `screen.draw_recovered` on two HDMI walls. The task
   name that would say (`pull:<wall_id>`) is dropped by `logs._BUILT_IN`.
   Frame-only events (`tv.*`, `binding.*`, `brightness.set`, the Frame's
   `rotation.*`) are unambiguous because a client has at most one Frame;
   `manifest.*` lines carry `manifest_path`, which contains the wall's directory.
2. **`tv.close_failed` is never emitted.** It logs at DEBUG and the root level is
   INFO; its comment says the failure is "said out loud rather than swallowed".
3. **The Frame's `last_error` carries only unreachability.** Upload failures,
   unconfirmed removals and a wall that will not change are journal-only.
4. **Pull and supervisor failures never reach a heartbeat.** A wall whose feed is
   refused or whose media keep failing shows on the server only as a stale
   `manifest_schema` / `theme_id`; a crash-looping worker shows only as a
   heartbeat ageing.
5. **Why a panel is not connected is not sent** (`LabelPanel.error`).
6. **`deploy/README.md` § How to tell your own install worked is stale for this
   repo**: it reads `/srv/art`, `curation.service` and the monorepo checkout
   path, and its own note says to rewrite it "when wave 3 lands".

## Discrepancies with Arrt's record

For a pull request on Arrt, not an edit here.

- Arrt § What Each Failure Looks Like, the "panel captions the wrong work" row,
  says `label.drawn` carries `tv_content_id`. It carries `label_id` and
  `display_state`, with `work_id` bound (`LabelPanel.draw`); Arrt § Correlation's
  2026-10-08 note already says no label event carries `tv_content_id`.
- The same table's `label.unusable` row says the condition is written to the
  heartbeat's `last_error` and that `label_surface_working` stays `true`. The
  panel is no longer a wall's: no heartbeat carries either for a label, and the
  row two above it already says `label.unusable` is journal-only.
- Arrt § A client, and a wall on its screen lists seven events. The client and
  HDMI sets are now larger: `client.wall_unplaceable`, `client.worker_crashed` /
  `_ended`, `client.document_refused`, `client.cache_unwritable`,
  `client.heartbeat_refused`, `client.identity_*`, `screen.started` /
  `stopped` / `crashed`, and the `client.label_*` events listed in Arrt's own
  § Correlation note.
- Arrt § What Each Failure Looks Like still has the "*Planned, 2026-09-30:* the
  server is unreachable from a Player" row, built in wave 2. The built events are
  `pull.unreachable` / `pull.reachable`, `client.unreachable` /
  `client.reachable` and `label.server_unreachable` / `label.server_reachable`.
  The row's "heartbeat's cache report" does not exist: no heartbeat key reports
  what the cache holds.
- Arrt calls the Player "display" or "the display plane". This repo keeps that
  name where it is an interface: the unit `display.service`, the journal
  identifier `display`, and the `daemon.*` events of a Frame wall.
