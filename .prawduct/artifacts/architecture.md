---
artifact: architecture
version: 1
depends_on:
  - artifact: project-state
  - artifact: platform-and-dependency-findings
  - artifact: samsung-tv-state-findings
  - artifact: hdmi-output-findings
last_validated: 2026-10-27
---

# Architecture — Arrt Player

**What this covers.** The Player's own topology: its one process, what runs
inside it, the channels it holds open, what it keeps on disk, and the seams that
let the suite run with no television, screen or panel. It describes the code as
it stood on 2026-10-27; every claim names the
`file:symbol` it rests on.

**What it points at instead of restating.** The server, and every rule the two
sides share, are Arrt's:

- The wire contract is the `arrt_contract` package at the tag
  `pyproject.toml` § `[tool.uv.sources]` names (`contract-v1.0.0` today). Its
  `player-contract.md` § Transport, § Versioning and § The heartbeat (minors 2–4)
  are the specification of every request below.
- Arrt's `clients.md` § The Player is the model (one process per client, one
  worker per wall, one renderer per label output), and Arrt's `architecture.md`
  § Components & Responsibilities › display is the history of the wall loop.
- Arrt's `nonfunctional-requirements.md` § The television belongs to whoever is
  using it, and § Direction (the Player never requires the server), are the
  norms this topology exists to keep.
- Arrt's `labels-and-surfaces.md` is the label model.

<!-- OPTIONAL norm home. No norms are ratified here. The Player's norms live in
     project-preferences.md § Enforcement, and in Arrt's artifacts that table
     points at. -->

## Overview & Topology

| Runtime | Where | Lifecycle | Ours? |
|---|---|---|---|
| Arrt server | elsewhere on the LAN | always-on | No: Arrt |
| **Arrt Player** (`python -m arrt_player`) | a Raspberry Pi, under systemd as `display.service` | always-on, `Restart=always` | **Yes: one process** |
| The Frame (Samsung television) | on the LAN | the household's | No: a device |
| HDMI screens | on the Pi's connectors | the household's | No: a device |
| E-paper panel | on the Pi's SPI bus | powered with the Pi | No: a device |

```
                     HTTP (aiohttp), Bearer CLIENT_TOKEN
   Arrt server  <──────────────────────────────────────────  Player process
   GET  /client                                                │
   POST /client/heartbeat                                      │ one asyncio loop
   GET  /walls/{id}/manifest/v{major}                          │
   GET  media.url (resolved against the manifest URL)          │
   POST /walls/{id}/heartbeat                                  │
   GET  /labels/{label_id}                                     │
                                                               ├── websocket + REST ──> the Frame (art API)
              CACHE_DIR (local disk) <── reads/writes ─────────┤
                                                               ├── ioctl on /dev/dri ─> HDMI connectors (KMS)
                                                               └── SPI + GPIO ────────> e-paper panel (omni_epd)
```

**Why the split.** The Player is a separate runtime from the server because it
sits beside the walls and the server does not (Arrt's `re-architecture.md`). It
is one process rather than one per wall so that several walls share one
television connection budget, one panel and one cache, and so that one wall's
fault does not need a process restart (`__main__` module docstring; `client.py`
module docstring).

## The process and its concurrency

**One process, one event loop.** `__main__.main` calls `asyncio.run(_run())`;
nothing in `src/` starts a second process or a second event loop. Searched:
`grep -rn "subprocess\|multiprocessing\|new_event_loop" src/` returns nothing.

**The task tree.** All tasks are created by `asyncio.create_task` at five sites
(`grep -rn create_task src/`):

| Task | Created by | Runs | Lives |
|---|---|---|---|
| the supervisor | `__main__._run` (awaited directly) | `client.Supervisor.run`: polls `GET /client` every `CLIENT_POLL_SECONDS` (default 30 s, `config.DEFAULT_CLIENT_POLL_SECONDS`), reconciles workers, POSTs the client heartbeat | the process |
| `wall:<wall_id>` | `Supervisor._start` | `Supervisor._keep_running` around `__main__.run_wall` | while the wall is assigned |
| `pull:<wall_id>`, `stop:<wall_id>` | `__main__._beside_its_pull` | `pull.Pull.run` beside the wall loop, and a relay of the supervisor's stop | with their wall's worker |
| `label:<output>` | `Supervisor._start_label` | `_keep_running` around `__main__.run_label` → `label_renderer.LabelRenderer.run` | while the label output is mapped |
| `compose:<wall_id>:<work_id>` | `programmes/schedule.py:Schedule._start` | composing one work for this wall's geometry, on a thread | one composition |

Inside a wall worker, `wall.Wall.run` is the loop: `Wall.tick` reads the cached
manifest (`manifest.Watcher.poll`), asks the programme
(`programmes/schedule.py:Schedule.step`) whether to change the wall, has the
display act, and writes the heartbeat file (`Wall._beat`). A display is either
`displays/frame.py:FrameDisplay` or `displays/screen.py:ScreenDisplay`; which one
is chosen by `__main__.run_wall` on the output's kind.

**Threads, where blocking work goes.** Every `asyncio.to_thread` in `src/`
(`grep -rn to_thread src/`):

- `label_renderer.py:LabelPanel.draw`: layout and the panel write, bounded by
  `LABEL_DRAW_BUDGET_SECONDS` (15 s). A draw that overruns is left running, not
  cancelled, and the panel's one-at-a-time gate (`LabelPanel._draw`) stays shut
  until it finishes.
- `displays/screen.py:ScreenDisplay.show` and `ScreenDisplay._refresh`: the KMS
  draw. `kms.py` guards its shared device with `threading.Lock`
  (`LibdrmDevice._lock`, and the module's `_devices_lock`), because two
  connectors on one card share one DRM master.
- `tv/samsung.py:SamsungTv.connect`: the library's constructor, which blocks on
  `get_token()`.
- `programmes/schedule.py`: composing a picture with Pillow.

The heartbeat write (`heartbeat.write`) stays on the loop on purpose: a few
hundred bytes and one `fsync` a minute (its docstring).

**Callbacks from the television's library.** `samsungtvws` runs its own reader
task on the art websocket. `SamsungTv._on_image_selected` and
`SamsungTv._on_art_mode_changed` run from it; `SamsungTv._tell_observers` isolates
each observer so one that raises costs neither the others nor the reader
(`tests/test_selection_observers.py`).

**What keeps two workers off one device.** `Supervisor.reconcile` refuses a
second wall on an output already claimed (`client.wall_unplaceable`), and
`Supervisor._start` awaits every worker still stopping before the new one runs
(the `after=stopping` argument to `_keep_running`), so a wall moved between
outputs never has two workers on one directory or one art channel. There is at
most one Frame per client: `config.FRAME_OUTPUT` is a single name and
`ClientSettings.frame` a single optional value. There is at most one panel:
`config.PANEL_OUTPUT` (`epd-0`), opened once per process by
`__main__.label_panel`.

**Shutdown.** SIGTERM and SIGINT set one `asyncio.Event` (`__main__._run`).
`Supervisor.stop_all` sets each worker's stop and gathers them; each wall loop
closes its display in `Wall.run`'s `finally`; the panel is closed last
(`LabelPanel.close`, the e-paper power-down). The unit gives this 90 s
(`deploy/display.service`, `TimeoutStopSec=90`). Every wait in a loop is
`asyncio.wait_for(stop.wait(), timeout=…)`, so a sleeping worker wakes on the
signal (`Wall._wait`, `Pull.run`, `Supervisor.run`, `LabelRenderer.run`).

## Components & Responsibilities

The module map, with each module's one job. Derived by walking `src/arrt_player`
with `ast` and resolving every `arrt_player.*` import to a module; the arrow list
is that walk's output, not a recollection.

| Module | Job | Imports (in-package) |
|---|---|---|
| `__main__` | Composition root: builds settings, the panel, the Frame client, the supervisor; the only place `tv.samsung`, `pull`, `kms` and the panel driver are named together | almost everything |
| `client` | `Supervisor`, the client document, output discovery (`hdmi_outputs`, `client_outputs`), the client heartbeat document, `FrameIdentity` | `config`, `episodes`, `heartbeat` |
| `pull` | Every HTTP request: `Pull` (one wall), `ClientPull` (the client routes), `LabelPull` (one label) | `client`, `config`, `episodes`, `heartbeat`, `label_rule`, `manifest` |
| `wall` | The one wall loop for every display kind; `Clock`; `DisplayRecord`; the `Display` and `Programme` protocols | `config`, `episodes`, `heartbeat`, `manifest` |
| `displays/frame` | The Frame as a `Display`: uploads, bindings, reconciliation, brightness, the art-mode gate | `brightness`, `config`, `episodes`, `heartbeat`, `logs`, `manifest`, `programmes.schedule`, `state`, `tv`, `wall` |
| `displays/screen` | An HDMI connector as a `Display`, behind `ScreenOutput` | `compose`, `config`, `episodes`, `heartbeat`, `manifest`, `programmes.schedule`, `wall` |
| `programmes/schedule` | What the wall should show from a major 2 feed; composes each work for the display | `compose`, `episodes`, `logs`, `manifest`, `wall` |
| `manifest` | Parse and watch the cached feed (`Watcher`, `parse`, `REQUESTED_MAJORS = (2,)`) | — |
| `compose` | Mat composition for a geometry (Pillow) | — |
| `kms` | `KmsOutput`/`LibdrmDevice`: libdrm through `ctypes` | `client`, `episodes` |
| `tv/client` | The `TvClient` seam and its exceptions | — |
| `tv/samsung` | `SamsungTv`, the only `samsungtvws` user | `tv.client` |
| `state/store` | `DisplayState`, the Frame's SQLite store | — |
| `label_rule` | The label document and the label rule | — |
| `label_renderer` | `LabelPanel` (the device) and `LabelRenderer` (one mapping) | `client`, `episodes`, `label_rule`, `logs`, `panel` |
| `panel/*` | Label content, layout, legibility, raster, the `LabelSurface` seam (`surface`), Pango (`pango`), the e-paper driver (`epaper`) | `panel.*` only |
| `heartbeat` | The wall heartbeat document and its atomic write | — |
| `logs` | JSON log shape and `work_id` binding | — |
| `episodes` | `ReportOnce`, `Backoff` | — |
| `config` | `.env` into `ClientSettings` / `WallSettings` / `Settings` | `compose`, `heartbeat` |
| `brightness` | Sun-angle brightness (astral) | — |

**Dependency direction, as the graph shows it.** `__main__` sits on top. Below it
the Frame and screen drivers depend on `wall`, `programmes.schedule` and the
leaf modules; `wall` depends only on leaves (`config`, `episodes`, `heartbeat`,
`manifest`). `panel/*` imports nothing outside `panel`. `tv/samsung` is imported
only by `__main__`; `displays/frame` sees only the `tv` seam. Two edges run
sideways rather than down: `kms` imports `client` (for `DRM_ROOT`,
`FRAMEBUFFER_KIND`, `hdmi_outputs`), and `pull` imports `client` (for the client
document parser).

**Third-party imports by module** (same walk, stdlib excluded): `aiohttp` in
`pull` only; `samsungtvws`, `websockets` in `tv/samsung` only; `omni_epd` in
`panel/epaper.open_panel` only, inside the function; `gi`, `cairo` in
`panel/pango` only; `PIL` in `compose`, `kms`, `panel/epaper`; `astral` in
`brightness`; `dotenv` in `config`. `arrt_contract` is imported by no module in
`src/`: it is a `dev` dependency-group member (`pyproject.toml`) and reaches only
the suite.

**What the tests enforce about layering, and what they do not.**
`tests/test_plane_isolation.py`, reading imports with `tests/import_graph.py`
(AST, relative imports resolved, `import_module`/`__import__` with a literal
argument counted, followed transitively through repo-local files):

- No module under `src/arrt_player` imports `arrt` or `arrt.*`
  (`test_no_display_module_reaches_the_curation_plane`).
- No module but `pull.py` reaches an HTTP client (`aiohttp`, `httpx`,
  `requests`, `urllib3`, `http.client`, `urllib.request`, `httplib2`,
  `aiohttp_client_cache`), directly or through a helper
  (`test_no_display_module_but_the_pull_opens_an_http_client`).
  `samsungtvws` and `websockets` are deliberately not on the list.
- `pull.py` is imported by `__main__.py` and nothing else
  (`test_only_the_entry_point_imports_the_pull`).
- Every string in `pull.py` that starts a path segment is a path in
  `arrt_contract.routes()` (`test_the_pull_requests_only_the_routes_the_contract_names`).
- Each half is shown failing against a planted violation (`TestTheGuardCanFail`).
- Its stated limit: attribute access such as `arrt_player.pull.aiohttp` from a
  module that never imports `pull` is invisible to it.

**Not enforced by any test** (searched `tests/` for `imported_names`,
`ast.parse` and `import_module`; only `test_plane_isolation.py`,
`import_graph.py` and `test_norm_index.py` use them, and the last reads Markdown):
that `tv/samsung` is imported only by `__main__`; that `panel/*` imports nothing
outside `panel`; that `wall` stays free of display specifics. These hold today
by the graph above and are Critic-checked norms ("Hardware + network access sits
behind an interface", "Async at the I/O boundary, synchronous core",
`project-preferences.md` § Enforcement).

**What each component must never do.**

- `pull` never writes a manifest it has not checked with the watcher's own
  parser, and never caches it before every master it names is fetched and
  hash-checked or answered for (`Pull._pull_manifest`, `Pull._adopt_feed`).
- Nothing but `displays/frame` and `tv/*` talks to the television; nothing in
  shipped code presses power. The second is Arrt's norm, tested by
  `tests/test_frame_wall.py::test_a_television_somebody_is_watching_is_left_alone`
  and the rows `project-preferences.md` lists for it.
- No label failure stops a wall: `LabelPanel.draw` never raises (its broad
  `except` with a pragma), and the label runs in its own task.

## Communication & Boundaries

| # | Channel | Transport | Direction | Sync | Contract |
|---|---|---|---|---|---|
| 1 | Client document | `GET /client`, ETag | server → Player | async poll, 30 s | `player-contract.md` § Transport; `pull.ClientPull.fetch` |
| 2 | Client heartbeat | `POST /client/heartbeat`, JSON | Player → server | on change, else every 60 s | `client-heartbeat.v1`; `client.client_heartbeat`, `Supervisor._report_outputs` |
| 3 | Wall feed | `GET /walls/{wall_id}/manifest/v{major}`, ETag, highest major first | server → Player | async poll, `MANIFEST_POLL_SECONDS` (default 1 s) | `player-contract.md` § Major 2, § The cutover; `Pull._request_manifest` |
| 4 | Media | `GET` of each work's `media.url`, resolved against the manifest URL; SHA-256 checked | server → Player | inside a feed poll | `Pull._cache_media` |
| 5 | Wall heartbeat | file in the wall's cache, then `POST /walls/{wall_id}/heartbeat` | Player → server | file every 60 s or on change; POST when the file changes | `heartbeat.v1`; `heartbeat.write`, `Pull._forward_heartbeat` |
| 6 | Label document | `GET /labels/{label_id}`, ETag | server → Player | poll, `LABEL_POLL_SECONDS` (default 1 s) | `labels-and-surfaces.md`; `pull.LabelPull.fetch` |
| 7 | Frame art channel | websocket via `samsungtvws` | Player ↔ television | async, with the set's own announcements | foreign; `tv/client.TvClient`, `samsung-tv-state-findings.md` |
| 8 | Frame REST | HTTP via the library's REST helper | Player → television | one-shot reads | `SamsungTv.power_state`, `SamsungTv.read_identity` |
| 9 | HDMI | `ioctl` on `/dev/dri/card*` through libdrm (`ctypes`) | Player → screen | blocking, on a thread | `kms.py`; `hdmi-output-findings.md` |
| 10 | E-paper | SPI and GPIO through `omni_epd` | Player → panel | blocking, on a thread | `panel/epaper.py`; `platform-and-dependency-findings.md` § The e-paper panel |
| 11 | Connector discovery | reads `/sys/class/drm` | kernel → Player | each client poll | `client.hdmi_outputs` |

**Trust boundaries.** Channels 1–6 cross the LAN and carry `CLIENT_TOKEN` as a
Bearer header (`Pull._auth`, `ClientPull._auth`, `LabelPull.fetch`). A media URL
on another origin is fetched without the token (`Pull._cache_media`,
`pull._same_origin`), so a manifest cannot send the token elsewhere. Every
document from the server is parsed and refused whole if it is malformed
(`client.parse_client_document`, `manifest.parse`, `label_rule.parse_label_document`).
Channels 7–8 cross the LAN to a device that authenticates the Player by a token
it issued, held in `TV_TOKEN_FILE` (default `CACHE_DIR/token_file`,
`config._frame`). Arrt's `security-model.md` owns the server side.

**Channel 5 is a file first.** The wall loop writes
`CACHE_DIR/<wall_id>/display-heartbeat-<wall_id>.json` atomically
(`heartbeat.write`: temp, `fsync`, rename) and the pull forwards it when its
mtime, size and `reported_at` change (`Pull._forward_heartbeat`). The loop never
waits on the network to report. The file is in the wall's own directory, never
one the server writes (`WallSettings.heartbeat_root`'s docstring says why).

**Channel 6 polls with no backoff.** `LabelRenderer.run` waits the poll interval
between ticks whatever the answer, and `LabelPull` bounds each request at 10 s
total (`LabelPull._client`). Channels 1 and 3 differ: see Failure Modes.

## Persistence boundary

**`CACHE_DIR` is the whole of what the Player keeps.** It is required and must be
absolute (`config.load`, `config._absolute`). Its layout, derived from the
constants and properties in `config.py`, `pull.py` and `manifest.py`:

```
CACHE_DIR/
  .client.json                 last good client document     (config.CLIENT_DOCUMENT_FILENAME; written by ClientPull)
  .client.etag                                               (pull.CLIENT_ETAG_FILENAME)
  token_file                   the Frame's pairing token, unless TV_TOKEN_FILE says otherwise (config._frame)
  <wall_id>/                   one per wall                  (ClientSettings.wall)
    manifest.json              last good feed                (config.CACHED_MANIFEST_FILENAME; written by Pull)
    manifest.etag                                            (pull.ETAG_FILENAME)
    media/sha256-<hash>        verified masters              (manifest.MEDIA_DIRNAME, manifest.media_name)
    composed/                  pictures composed for this display (config.COMPOSED_DIRNAME; Schedule)
    display-heartbeat-<wall_id>.json                         (heartbeat.HEARTBEAT_FILENAME_TEMPLATE; Wall._beat)
    display-state.sqlite       Frame walls only              (config.STATE_FILENAME; state.DisplayState)
```

**Single writer per file.** Each file above has one writer, named in the
right-hand column. Two walls cannot share a directory: a wall id must match
`config._PLAIN_COMPONENT` (no `..`, no `/`, no leading dot) or
`ClientSettings.wall` raises `WallIdUnusable`, and the supervisor reports it and
does not start the wall. The leading-dot rule is what keeps `.client.json` apart
from every wall directory.

**Atomic writes.** `pull._write_atomically` for the client document, manifest,
ETags and media; `heartbeat.write` for the heartbeat. Media are evicted only
after a new feed is cached, and a master either feed names is kept
(`Pull._evict`, called with `held | previous`).

**The Frame's store.** `state/store.py:DisplayState`: tables `tv_binding` and
`daemon_state`, version in `PRAGMA user_version` (`SCHEMA_VERSION = 2`). A store
from a newer Player raises `StateSchemaTooNew`, and `__main__.run_frame_wall`
parks that one wall (`daemon.state_too_new`) rather than opening it. `tv_binding`
cannot hold `uploaded` without a content id (the table's check constraint, per the
module docstring). The store is the only per-device state that costs anything to
lose: losing it costs re-uploading the theme to the set.

**Not persisted.** Brightness (recomputed from the sun, `brightness.py`), the
supervisor's view of running workers, every `ReportOnce` episode, and the label
renderer's last document (`LabelRenderer._document`). A restart re-announces any
condition still true.

## Failure Modes & Resilience

The intended answer to every failure below is that the walls keep showing what is
cached. Arrt's `nonfunctional-requirements.md` § Direction is the norm;
`tests/test_pull.py::test_the_server_stopped_while_the_wall_runs_and_the_schedule_continues_from_the_cache`
is its test.

| Failure | What happens | Bounds |
|---|---|---|
| Server unreachable, 5xx, timeout | Walls rotate from cache; feed poll backs off; heartbeat POSTs stop; client document kept | `Pull`: 2 s doubling to 60 s (`BACKOFF_START_SECONDS`, `BACKOFF_MAX_SECONDS`), request 60 s total / 10 s connect. `ClientPull`: fixed 30 s poll, 30 s / 10 s. `LabelPull`: 1 s poll, 10 s / 5 s |
| Server down at boot | `Supervisor.run` reconciles from `.client.json` before the first request | — |
| Token refused (401), wall not this client's (403), nothing published (404 at every major) | Logged once per status per episode; cache kept; poll continues at the normal interval | `Pull._report_refused` |
| Feed unparseable or an unknown major | Refused before caching; the last good feed stays | `Pull._pull_manifest`, `manifest.Watcher.poll` |
| A master 404s or fails its hash | That work is skipped; the rest of the feed is cached | `Pull._cache_media` |
| A master fails transiently | The whole feed is held back; the poll backs off; the heartbeat still goes | `Pull.cycle`, `_Poll.MEDIA_FAILING` |
| A wall's pull raises | `__main__._pull_ended` logs and ends that wall's worker; the supervisor restarts both | 5 s doubling to 300 s (`client.RESTART_MIN_SECONDS`, `RESTART_MAX_SECONDS`); the ladder resets after a run longer than 300 s |
| A wall loop or label renderer raises | `Supervisor._keep_running` logs and restarts it; other walls untouched | same ladder |
| The supervisor raises | The process ends; systemd restarts it | `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0` |
| Television asleep or gone | `TvUnavailable`; the wall holds; a heartbeat is still written | `TV_RETRY_MIN_SECONDS`..`TV_RETRY_MAX_SECONDS` (5 s..300 s); connect 30 s; upload 60 s; selection confirm 8 s (`config` defaults) |
| Television in use (not art mode) | Nothing is sent to it | `FrameDisplay.is_ours`; Arrt's norm |
| Selections accepted, none displayed | Said once, rotation stops trying for the pass | `Shown.WALL_UNCHANGED`, `FrameDisplay._report_wall_unchanged` |
| HDMI screen absent or the card refuses | The wall rotates unseen; drawn again when a screen returns | `KmsOutput`, `ScreenDisplay._refresh` every poll |
| Panel will not open, or no text stack, or viewing distance unset | No label; the walls run; the client heartbeat reports the label output not connected | `__main__.label_panel`, `LabelPanel.report` |
| A panel draw hangs | The renderer stops waiting at 15 s; the gate stays shut until the thread returns | `LABEL_DRAW_BUDGET_SECONDS` |
| Heartbeat file cannot be written | Said once; the wall is unaffected | `Wall._beat` |
| Frame store from a newer Player | That wall is parked until a rollout | `__main__.run_frame_wall` |
| Disk full | Writes raise `OSError` inside the pull or the loop; each is the corresponding row above. Nothing checks free space ahead | — |

**No unbounded queues.** Nothing in `src/` uses `asyncio.Queue` (searched). Work
that cannot proceed is retried on the next poll, not buffered.

## Deployment & Version Skew

- **One deploy unit.** The Player ships as a checkout on the Pi and runs from it
  under `deploy/display.service`. The move procedure is
  `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`.
- **Skew with the server is a contract version.** The Player takes
  `arrt-contract` at a tag; Arrt's `player-contract.md` § Versioning says what
  each side must tolerate. The Player asks for the majors in
  `manifest.REQUESTED_MAJORS` (`(2,)`), highest first, and re-asks for higher
  ones every 60 s (`pull.HIGHER_MAJOR_SECONDS`). It sends heartbeat schema
  `1.4` (`heartbeat.SCHEMA_MAJOR`, `SCHEMA_MINOR`).
- **The route strings are spelled in `pull.py`**, not imported from the
  contract, because the contract is not a runtime dependency. The isolation
  test holds them to `arrt_contract.routes()`.
- **The Frame store is forward-refusing** (`StateSchemaTooNew`), and migrates
  forward on open (`DisplayState._widen`). Rolling back a Player across a store
  migration parks its Frame wall.
- **Settings retired by a release are refused by name** (`config.RETIRED_SETTINGS`),
  so an old `.env` fails at start rather than being half-read.

## Scaling Model

Not a concern at this size: one household, a handful of outputs per Pi. The
first limit is the Pi's own: every master is read whole into memory before it is
hashed (`Pull._cache_media` calls `response.read()`), and composition and the
label draw are seconds of CPU on a thread. Nothing here is designed to scale out.

## Cross-Cutting Runtime Concerns

- **Time.** `wall.Clock` carries two readings: `now()` (aware local time) for the
  schedule and the sun, `monotonic()` for every interval and backoff, so an NTP
  step on a Pi with no RTC cannot stall or fire waits (`Clock`'s docstring;
  `episodes.Backoff`). Instants in documents are RFC 3339 with an offset
  (`player-contract.md` § Time). The server's clock and the Player's are not
  reconciled; the label rule's hold runs from the Player's own receipt time
  (`LabelRenderer._last_answer`).
- **Configuration and secrets.** One `.env`, read once at start by `config.load`
  (the unit's `EnvironmentFile=` too). A change takes a restart. A missing or
  malformed value stops the process with `ConfigError` before anything runs
  (`__main__.main`, exit 2). `CLIENT_TOKEN` is held with `repr=False` on
  `WallSettings` and `ClientSettings`; the TV token is a path, not a value. The
  observability strategy covers what reaches the journal.
- **Correlation.** See `observability-strategy.md` § Correlation Context.
- **Environment parity: the seams that keep the suite off the hardware.** Nothing
  in the suite reaches a television, screen, panel or server (CLAUDE.md § The
  television):

  | Boundary | Seam | Double | How the real one is kept out |
  |---|---|---|---|
  | Television | `tv/client.TvClient` (ABC) | `tests/fakes.py:FakeTv` subclasses it, so a new verb fails the double at import | `displays/frame.frame_wall` takes a `TvClient`; only `__main__.frame_tv` builds `SamsungTv` |
  | Label surface | `panel/surface.LabelSurface` (ABC) | `tests/fakes.py:FakeSurface` for the renderer; `tests/test_epaper.py:FakeEpd` and `FlatRasterizer` for `EpaperSurface` itself | `panel/epaper.EpaperSurface` takes its driver (`epd=`) and a rasterizer as arguments; `omni_epd` is imported only inside `open_panel` |
  | HDMI | `displays/screen.ScreenOutput` (Protocol); `kms.Device` (Protocol) | `tests/fakes.py:RecordingOutput`; `tests/fakes.py:drm_tree` builds a fake `/sys/class/drm` | `client.hdmi_outputs(drm_root=…)` and `kms.KmsOutput(drm_root=…, dev_root=…, device_for=…)` are injectable |
  | Server | `client.ClientLink` (Protocol) for the supervisor; real `aiohttp` against a local server for the pull | `tests/server_double.py:ServerDouble`, an `aiohttp.web` app on the contract's own routes, validating client heartbeats against `client-heartbeat.v1`, served by `aiohttp.test_utils.TestServer` | tests set `server_url` to the test server's URL (`tests/test_pull.py`) |
  | Clock | `wall.Clock`; `monotonic=` on `Supervisor`, `Pull`, `Backoff` | test clocks | injected everywhere a wait is measured |
  | Text stack | `panel/raster.Rasterizer` | `tests/test_epaper.py:FlatRasterizer` | the real one, `panel/pango.PangoRasterizer`, is exercised by `tests/raster`, which needs `--group raster` and skips without it |

  The hardware is exercised only by `tools/tv_api_check.py`, `tools/tv_delete.py`,
  `tools/panel_check.py` and `tools/power_probe.py`, run by hand.
- **GPIO ownership.** `omni_epd` pulls `jetson-gpio`, which installs a shim at the
  path `rpi-gpio` owns. `pyproject.toml` overrides it out, and
  `tests/test_gpio_has_one_owner.py` holds the lockfile to that: every requirer
  of `jetson-gpio` carries the marker `sys_platform == 'never'`, and `rpi-gpio`
  is still locked. It checks `uv.lock`, not an installed Pi.

## Decision Log

Decisions visible in the code, with where each is argued. New entries append.

1. **One process per client, one worker per wall** — not one process per wall,
   so one wall's fault restarts that worker and not the others
   (`__main__` docstring; Arrt's `clients.md`).
2. **A dead pull ends its own wall's worker** rather than the process
   (`__main__._beside_its_pull`, `_pull_ended`).
3. **One module holds HTTP** (`pull.py`), reachable only from the composition
   root, so "just fetch it live" cannot appear elsewhere
   (`tests/test_plane_isolation.py` docstring).
4. **The heartbeat is a file the pull forwards**, so the wall loop never waits on
   the network to report (`pull.py` docstring; `WallSettings.heartbeat_root`).
5. **Blocking device work goes to threads, the core stays on one loop**; the
   heartbeat write stays on the loop by size (`heartbeat.write` docstring;
   `ScreenOutput` docstring; `LabelPanel.draw`).
6. **Hardware behind ABCs or Protocols, drivers passed in**, so the suite runs on
   a GitHub runner (`tv/client.py`, `panel/surface.py`, `panel/epaper.py`
   docstrings).
7. **The panel belongs to the client, not to a wall**: opened once per process
   and mapped by the server (`__main__.label_panel`; Arrt's
   `labels-and-surfaces.md`).

## Discrepancies with Arrt's record

Places where Arrt's artifacts, as read on 2026-10-27, say something about the
Player that is now wrong for this repo. Each is
for a pull request on Arrt, not an edit here.

- Arrt's `architecture.md` § Communication & Boundaries: the channel table still
  lists channels 1, 2 and 2b as files in `ART_ROOT` and channel 6 as "SPI"
  alone, with a 2026-09-30 note that they "become LAN HTTP". They did: see the
  table above. The HDMI channel is not in Arrt's table.
- Arrt's `architecture.md` § Failure Modes & Resilience opens "There is no network
  between the planes". The rows "display process down … label goes stale" and
  "E-paper write fails" predate the label output: a label may be drawn by a
  different client from the wall it captions.
- Arrt's `architecture.md` cites `tests/preferences/test_plane_isolation.py` in
  three places (§ Direction). Here it is `tests/test_plane_isolation.py`.
- Arrt's `architecture.md` § display "Owned state" places `display-state.sqlite`
  as display's store without a path; here it is per wall,
  `CACHE_DIR/<wall_id>/display-state.sqlite`, and only on Frame walls.
- Arrt names the Player "display" or "the display plane" throughout. The same
  name survives in this repo in `logs.py` (the docstring, the context variable
  `display_work_id`), `heartbeat.py`, the `daemon.*` journal prefix
  (`FrameDisplay.journal_name`), the stderr message in `__main__.main`
  ("display plane cannot start"), the unit name `display.service` and its
  `SyslogIdentifier=display`. The unit and journal names are operational
  interfaces; changing them is a migration, not a rename.
