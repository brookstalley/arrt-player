---
artifact: architecture
version: 1
depends_on:
  - artifact: project-state
  - artifact: platform-and-dependency-findings
  - artifact: samsung-tv-state-findings
  - artifact: hdmi-output-findings
last_validated: 2026-11-25
---

# Architecture — Arrt Player

**What this covers.** The Player's own topology: its one process, what runs
inside it, the channels it holds open, what it keeps on disk, and the seams that
let the suite run with no television, screen or panel. It describes the code as
it stood on 2026-11-25, after audit waves 2 and 3; every claim names the
`file:symbol` it rests on.

**What it points at instead of restating.** The server, and every rule the two
sides share, are Arrt's:

- The wire contract is the `arrt_contract` package at the tag
  `pyproject.toml` § `[tool.uv.sources]` names (`contract-v1.3.0` today). Its
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
| A directory (`FILE_OUTPUT_DIR`) | the Pi's disk, or any machine's | a Player started with it draws to files and **drives no hardware**: one screen output, `file-screen-0`, and with `FILE_OUTPUT_LABEL=on` one label output, `file-label-0`, in place of the HDMI connectors and the panel | Ours: `displays/file_output.py`, `panel/file_surface.py` |
| mpv (slideshow mode) | a child process of the Player, in its own process group | started on the first draw, stopped on every way out (`mpv.MpvOutput`) | Run by the Player; not our code |
| Reddit, redgifs, and any host a post links (slideshow mode) | the internet | theirs | No: services |

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

**Two modes, never both** (`PLAYER_MODE`: `arrt`, the default, or
`slideshow`; `config.PLAYER_MODES`). Everything above is **Arrt mode**.
**Slideshow mode** needs no Arrt server: `__main__._run` hands a
`SlideshowSettings` to `run_slideshow`, which plays one Reddit listing on one
HDMI connector through mpv and captions each post on the panel. It has no
supervisor, no client document, no pull and no heartbeat; the settings page and
the panel are the parts the two modes share.

```
   www.reddit.com, api.redgifs.com  <── listing and API calls (cookie to Reddit only) ──┐
   any public host a post links     <── media downloads, public addresses only ─────────┤ sources/fetch.py
                                                                                         │
              CACHE_DIR/.slideshow/ <── reads/writes ─── Player process ─── JSON IPC ──> mpv ── DRM ──> HDMI
                                                                        └── SPI + GPIO ──> e-paper panel
```

**A Player with no hardware** (`FILE_OUTPUT_DIR`, Arrt mode only) reports a screen
and optionally a label that are directories, places walls and labels on them as on
any other output, and draws what it would have drawn into files that a reader can
assert on (`__main__.player_outputs`, `label_panels`, `file_screen_output`;
`displays/file_output.py`). It exists so that a real Player process can be run
against a real server where there is no screen, television or panel, which is
what the server's test of a real Player does; `tests/test_running_without_hardware.py`
is this repository's half, run as a process against `tests/server_double.py`.
It is **never beside hardware**: `TV_ADDRESS` or `EPD_DEVICE` beside it, a
directory in or around `CACHE_DIR`, and slideshow mode are each refused at start
by name, and it says so once at WARNING (`files.drawing`), because on a wall it
would be a Player showing nothing.

**The Player tells systemd when it is up and when it makes progress**
(`notify.py`; `Type=notify`, `WatchdogSec=300`): `READY=1` once each mode is
running, `WATCHDOG=1` only for progress, which is the supervisor finishing a
cycle (`__main__.PettingSupervisor`), the slideshow reaching its next step
(`__main__.petting`), or, in setup mode, the page being served by a loop that
turns. A stuck single wall worker is not watched, by design, because a restart
would blank every other wall. Without `NOTIFY_SOCKET` nothing is sent.

**Why the split.** The Player is a separate runtime from the server because it
sits beside the walls and the server does not (Arrt's `re-architecture.md`). It
is one process rather than one per wall so that several walls share one
television connection budget, one panel and one cache, and so that one wall's
fault does not need a process restart (`__main__` module docstring; `client.py`
module docstring).

## The process and its concurrency

**One process, one event loop, and in slideshow mode one child.**
`__main__.main` calls `asyncio.run(_run())`; nothing in `src/` starts a second
event loop. The one other process is mpv, which `mpv.MpvOutput._start` runs with
`subprocess.Popen` in its own process group, with an environment of only
`mpv.MPV_ENVIRONMENT` (so none of `.env` reaches it), and which the Player owns for its
whole life: stopped by group on every way out, its pid kept in
`CACHE_DIR/.mpv/` so one left by a Player killed outright is stopped at the next
start (`mpv.stop_stale`, which checks the pid's command line names this
directory's socket before stopping it, since pids are reused). `mpv.py` also runs
`ps` once for that check. Searched 2026-11-11:
`grep -rn "subprocess\|multiprocessing\|new_event_loop" src/` finds only
`mpv.py`.

**The task tree.** Every `asyncio.create_task` in `src/`
(`grep -rn create_task src/`):

| Task | Created by | Runs | Lives |
|---|---|---|---|
| `settings-page` | `__main__._run`, beside the supervisor; in setup mode awaited directly and alone | `__main__.run_settings_page` → `settings_ui.server.serve`: the settings page on `UI_BIND`:`UI_PORT`. A bind failure ends this task and nothing else | the process |
| the supervisor | `__main__._run` (awaited directly) | `client.Supervisor.run`: polls `GET /client` every `CLIENT_POLL_SECONDS` (default 30 s, `config.DEFAULT_CLIENT_POLL_SECONDS`), reconciles workers, POSTs the client heartbeat, and sweeps the cache of walls and labels the document no longer places here (`WallCaches`, `forget_labels`), only against a document that was read | the process |
| `wall:<wall_id>` | `Supervisor._start` | `Supervisor._keep_running` around `__main__.run_wall` | while the wall is assigned |
| `pull:<wall_id>`, `stop:<wall_id>` | `__main__._beside_its_pull` | `pull.Pull.run` beside the wall loop, and a relay of the supervisor's stop | with their wall's worker |
| `heartbeat:<wall_id>` | `pull.Pull.run` | `Pull._heartbeats`: forwards the wall's heartbeat (`_heartbeat_turn`, the body `Pull.cycle` runs too) every poll interval while the last manifest poll was answered, on its own loop, so a manifest poll backing off never delays a report | with its pull |
| `label:<output>` | `Supervisor._start_label` | `_keep_running` around `__main__.run_label` → `label_renderer.LabelRenderer.run` | while the label output is mapped |
| `compose:<wall_id>:<work_id>` | `programmes/schedule.py:Schedule._start` | composing one work for this wall's geometry, on a thread | one composition |
| `settings-page` (slideshow mode) | `__main__.run_slideshow`, beside the programme | as in Arrt mode | the process |
| `slideshow-listing` | `programmes/slideshow.py:Slideshow` | one read of the listing, every `REFRESH_S` (600 s), or `FAILED_REFRESH_S` (120 s) or Reddit's own retry-after after a failure; taken between items | one read |
| `slideshow-fill` | `Slideshow.run` | `MediaCache.fill`: downloads from the play position onward, after each listing taken or when nothing is ready | the programme |
| `slideshow-label` | `Slideshow` | one label draw for the post on screen | one draw |

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
  until it finishes. `LabelPanel.close` waits for a draw still in the driver
  (`LabelPanel._in_driver`), up to the same budget, before it powers the panel
  down; past it the panel is left open and `label.close_abandoned` says so.
- `displays/screen.py:ScreenDisplay.show`, `ScreenDisplay._refresh` and
  `go_dark` (`ScreenOutput.blank`): the KMS draw. `kms.py` guards its shared device with `threading.Lock`
  (`LibdrmDevice._lock`, and the module's `_devices_lock`), because two
  connectors on one card share one DRM master.
- `tv/samsung.py:SamsungTv.connect`: the library's constructor, which blocks on
  `get_token()`.
- `programmes/schedule.py`: composing a picture with Pillow.
- `programmes/slideshow.py`: every call into the screen (`MpvOutput.load`,
  `check`, `close`), each of which waits on mpv's socket with a timeout.
  `MpvOutput._lock` keeps two threads from interleaving commands on that socket.
- `settings_ui/server.py`: scrypt for the page's password (tenths of a second
  and 16 MiB on a Pi).

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
outputs never has two workers on one directory or one art channel. The supervisor
keeps the last client document and matches the walls to it again whenever the
outputs differ from the ones it last matched against (`Supervisor.cycle`,
`_current_outputs`), so a connector the kernel lists after the Player started
places its wall although the server, with nothing new to say, answers 304. There is at
most one Frame per client: `config.FRAME_OUTPUT` is a single name and
`ClientSettings.frame` a single optional value. **A label is an output a Player
has zero or more of, as a display is** (the owner's ruling of 2026-11-19): the
configuration describes at most one panel today (`config.PANEL_OUTPUT`, `epd-0`;
`EPD_DEVICE` naming more than one is refused at start, naming each), and
everything past it holds the panels by output name: `__main__.label_panels`
opens them once per process, `__main__.run_label` draws a label only on the
panel it names (and says `label.no_panel`, once, when it names none), the client
heartbeat reports and `__main__._run` closes every one. A Player with several panels, or
with labels and no display, is a change to the configuration and nothing else.

**Shutdown.** SIGTERM and SIGINT set one `asyncio.Event` (`__main__._run`).
`Supervisor.stop_all` sets each worker's stop and gathers them; each wall loop
closes its display in `Wall.run`'s `finally`; the panel is closed last
(`LabelPanel.close`, the e-paper power-down, after any draw still in the
driver). A Player that is stopping leaves its screens and its labels as they
are: only a wall the document takes off an output is retired, and a retired
screen wall draws black there (`Supervisor._stop`, `client.WorkerStop`,
`__main__.run_screen_wall`, `displays/screen.go_dark`); a label renderer is told
the same way and leaves its panel blank. The unit gives this 90 s
(`deploy/display.service`, `TimeoutStopSec=90`), and the watchdog stops a Player
that stopped making progress with the same SIGTERM (`WatchdogSignal=SIGTERM`), so
its art channel is closed rather than abandoned; one whose event loop is itself
stuck is SIGKILLed after `TimeoutAbortSec=90`. Every wait in a loop is
`asyncio.wait_for(stop.wait(), timeout=…)`, so a sleeping worker wakes on the
signal (`Wall._wait`, `Pull.run`, `Supervisor.run`, `LabelRenderer.run`).

## Components & Responsibilities

The module map, with each module's one job. Derived by walking `src/arrt_player`
with `ast` and resolving every `arrt_player.*` import to a module; the arrow list
is that walk's output, not a recollection.

| Module | Job | Imports (in-package) |
|---|---|---|
| `__main__` | Composition root: builds settings, the panels (`label_panels`), the outputs (`player_outputs`), the Frame client, the supervisor (`PettingSupervisor`) and the label sweep (`LabelCacheSweep`); the only place `tv.samsung`, `pull`, `kms` and the panel driver are named together | almost everything |
| `notify` | systemd's notify protocol, `READY=1` and `WATCHDOG=1`, over `$NOTIFY_SOCKET` with no dependency; a no-op without it | `episodes` |
| `client` | `Supervisor` (and `heard`, what the server last said of this Player), `WorkerStop` (why a worker was stopped), `WallCaches` (a departed wall's directory), the client document, output discovery (`hdmi_outputs`, `client_outputs`), the client heartbeat document and the settings page's address in it (`settings_url`), `FrameIdentity`, `ReportAnswer` and `HeardReport` | `config`, `episodes`, `heartbeat`, `logs`, `manifest`, `safe_write` |
| `pull` | Every HTTP request: `Pull` (one wall), `ClientPull` (the client routes, the report it holds back after a refusal, its last answer, and the address it reached the server from), `LabelPull` (one label, and its cached document) | `client`, `config`, `episodes`, `heartbeat`, `label_rule`, `manifest`, `safe_write` |
| `wall` | The one wall loop for every display kind; `Clock`; `DisplayRecord`; the `Display` and `Programme` protocols | `config`, `episodes`, `heartbeat`, `manifest` |
| `displays/frame` | The Frame as a `Display`: which set this is (`_know_the_set`, one phase of four, `SetPhase`), uploads and their records, bindings, their retirement, the removal of what the records name, brightness, the art-mode gate | `brightness`, `config`, `episodes`, `heartbeat`, `logs`, `manifest`, `programmes.schedule`, `state`, `tv`, `wall` |
| `displays/file_output` | A screen and a label drawn to a directory (`FILE_OUTPUT_DIR`): another implementation of `ScreenOutput`, and the label panel on `panel/file_surface` | `client`, `config`, `kms`, `label_renderer`, `label_rule`, `logs`, `panel`, `safe_write` |
| `displays/screen` | An HDMI connector as a `Display`, behind `ScreenOutput`; `go_dark`, the black a wall leaves its connector with | `compose`, `config`, `displays.screen_brightness`, `episodes`, `heartbeat`, `logs`, `manifest`, `programmes.schedule`, `wall` |
| `displays/screen_brightness` | An HDMI screen's brightness on the sun, over the `ddc.DdcBus` seam | `brightness`, `config`, `ddc`, `episodes`, `wall` |
| `programmes/schedule` | What the wall should show from a major 2 feed; composes each work for the display | `compose`, `episodes`, `logs`, `manifest`, `wall` |
| `manifest` | Parse and watch the cached feed (`Watcher`, `parse`, `REQUESTED_MAJORS = (2,)`) | — |
| `compose` | Mat composition for a geometry (Pillow), and `is_intact`, the check on a composed file found on disk | `safe_write` |
| `kms` | `KmsOutput`/`LibdrmDevice`: libdrm through `ctypes` | `client`, `episodes` |
| `ddc` | A monitor's DDC/CI features over its HDMI connector's I2C bus (`Ddc`, `I2cTransport`, `bus_for`); no `ddcutil` | `client` |
| `tv/client` | The `TvClient` seam, its exceptions, and `upload_marker`, the `image_date` an upload carries | — |
| `tv/samsung` | `SamsungTv`, the only `samsungtvws` user | `tv.client` |
| `state/store` | `DisplayState`, the Frame's SQLite store: the bindings of the set the wall is on, those of the sets it has left, the set's identity (`belongs_to`), and the record of every upload sent (`record_sending`, `uploads_on`) | — |
| `label_rule` | The label document and the label rule | — |
| `label_renderer` | `LabelPanel` (the device) and `LabelRenderer` (one mapping) | `client`, `episodes`, `label_rule`, `logs`, `panel` |
| `panel/*` | Label content, layout, legibility, raster, the `LabelSurface` seam (`surface`), Pango (`pango`), the e-paper driver (`epaper`), the surface that needs no typesetter (`file_surface`) | `panel.*` only |
| `heartbeat` | The wall heartbeat document and its atomic write | `safe_write` |
| `safe_write` | The one way a file is written whole: stage beside it, `fsync`, rename, leave no temporary file (`write_whole`, `write_whole_with`) | — |
| `logs` | JSON log shape, and the `work_id`, `wall_id` and `label_id` a worker binds around what it runs (`work_context`, `wall_context`, `label_context`) | — |
| `episodes` | `ReportOnce`, `Backoff` | — |
| `config` | `.env`, with the settings page's file beneath it, into `ClientSettings` / `WallSettings` / `Settings` / `UiSettings`; `FileOutputSettings`; `resolve`, `load` with the page's file swapped in, which the page judges a save by; `SetupRequired` | `compose`, `dwell`, `heartbeat`, `settings_ui.store`, `sources.reddit_settings` |
| `settings_ui/store` | The settings page's file, `$CACHE_DIR/.settings.json` (layout 3 while it holds `TV_ADDRESS`, else 2) | `safe_write` |
| `settings_ui/auth` | The page's password hash, session cookie, setup code and login throttle (per source address, under a ceiling for all of them) | — |
| `settings_ui/server` | The settings page (`aiohttp.web`), which answers only to its own names (`_known_hosts`), saves what `config.resolve` accepts, forgets the television's pairing when the Frame's address changes, says what a save did, and says the Player's state in the server's words (`PLAYER_STATE_WORDS`, from `Supervisor.heard`); the Player's one listening socket | `client`, `config`, `programmes.slideshow_status`, `settings_ui.auth`, `settings_ui.store`, `sources`, `sources.reddit_settings` |
| `brightness` | Sun-angle brightness (astral), for the Frame and the HDMI screens | — |
| `programmes/slideshow` | Slideshow mode's programme: the listing on its own task, the cache filled on another, playback from what is cached, the caption; the saved listing | `dwell`, `episodes`, `label_rule`, `mpv`, `programmes.slideshow_status`, `safe_write`, `sources`, `sources.cache` |
| `programmes/slideshow_status` | What the settings page shows of a running slideshow | `sources` |
| `dwell` | How long an item stays up: a still for the minimum, a clip repeated to reach it and cut at the maximum | — |
| `mpv` | `MpvOutput`, the slideshow's screen through an mpv this process runs, started with only `MPV_ENVIRONMENT`; `renderer_problems`, `stop_stale` | `client`, `episodes`, `kms` |
| `sources` | The source seam (`Item`, `Listing`, `Outcome`, `Kind`), so another source can replace Reddit | — |
| `sources/fetch` | The slideshow's one HTTP client: API calls to `API_HOSTS`, media from public addresses only | `sources` |
| `sources/reddit`, `sources/reddit_settings` | The Reddit listing read into items; the listing URL and cookie checks the page uses, with no network import | `sources`, `sources.fetch`, `sources.reddit_settings` |
| `sources/cache` | `MediaCache`: the 2 GB media cache under `CACHE_DIR/.slideshow/` | `sources`, `sources.fetch` |
| `sources/resolvers/*` | Per-host resolvers (redgifs HD, imgur) tried before Reddit's own copy | `sources`, `sources.fetch` |

**Dependency direction, as the graph shows it.** `__main__` sits on top. Below it
the Frame and screen drivers depend on `wall`, `programmes.schedule` and the
leaf modules; `wall` depends only on leaves (`config`, `episodes`, `heartbeat`,
`manifest`). `panel/*` imports nothing outside `panel`. `tv/samsung` is imported
only by `__main__`; `displays/frame` sees only the `tv` seam. Two edges run
sideways rather than down: `kms` imports `client` (for `DRM_ROOT`,
`FRAMEBUFFER_KIND`, `hdmi_outputs`), and `pull` imports `client` (for the client
document parser).

**Third-party imports by module** (same walk, stdlib excluded): `aiohttp` in
`pull` and `sources/fetch` only (with `yarl` in the latter), and `aiohttp.web` (the server half) in `settings_ui/server` only; `samsungtvws`, `websockets` in `tv/samsung` only; `omni_epd` in
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
- No module but `pull.py` and `sources/fetch.py` reaches an HTTP client
  (`aiohttp`, `httpx`, `requests`, `urllib3`, `http.client`, `urllib.request`,
  `httplib2`, `aiohttp_client_cache`), directly or through a helper
  (`test_no_display_module_but_the_pull_and_the_slideshow_fetch_opens_an_http_client`).
  `samsungtvws` and `websockets` are deliberately not on the list.
- `sources/fetch.py` is imported only from `sources/`, the slideshow programme
  and `__main__` (`test_only_the_slideshow_imports_its_fetch`); its API calls go
  only to `SLIDESHOW_API_HOSTS` (`test_the_slideshow_fetch_calls_only_its_api_hosts`);
  it imports nothing that could tell it the server's address or token
  (`test_the_slideshow_fetch_cannot_learn_the_server`). That its media downloads
  reach public addresses only is held by `tests/test_fetch.py`.
- mpv is handed files under the media cache, never a URL (`MpvOutput._refusal`),
  so it cannot become a third network client.
- `pull.py` is imported by `__main__.py` and nothing else
  (`test_only_the_entry_point_imports_the_pull`).
- The one exception beside the pull: `settings_ui/server.py` may import
  `aiohttp.web`, spelled `from aiohttp.web import …`, and nothing else from aiohttp,
  names no client class (`test_the_settings_page_names_no_client`), and is
  imported only by `__main__.py`
  (`test_only_the_entry_point_imports_the_settings_page`). It listens and
  requests nothing.
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
  parser, and caches it before every master it names is in hand only for the
  master that cannot be had: one answered for (a `404`, a hash mismatch, a size
  past what the feed declares, or no size declared) is skipped at once, and one
  that fails in a way that may clear holds the feed already cached for at most
  `MEDIA_HOLD_SECONDS`, after which the new feed is cached without it and with
  no ETag, so every later poll asks for it again (`Pull._pull_manifest`,
  `_adopt_feed`, `_hold_or_adopt`).
- `pull` never reads what the server sends without a bound: a master is stopped at
  the bytes its feed declares, a feed at `MANIFEST_MAX_BYTES` (16 MiB), the client
  and label documents at `DOCUMENT_MAX_BYTES` (1 MiB). Both are the contract's
  `max_bytes` for their routes, held to it by `tests/test_transport_table.py`; a
  document past its bound is refused as unreadable and what the cache holds stays
  (`Pull._cache_media`, `_walk`, `_read_capped`).
- Nothing but `displays/frame` and `tv/*` talks to the television; nothing in
  shipped code presses power. The second is Arrt's norm, tested by
  `tests/test_frame_wall.py::test_a_television_somebody_is_watching_is_left_alone`
  and the rows `project-preferences.md` lists for it.
- No verb that changes what the set shows or stores is sent without reading the
  set first. Each is a final method of `tv.client.TvClient` that calls `_permit`
  (the read) and then the client's `_send_<verb>`; a subclass that defines one
  is refused when the class is made (`TvClient.__init_subclass__`), and a verb
  added to the seam is in `tv.client.WRITES` or `READS` or a test fails by name
  (`tests/test_samsung_client.py::test_every_verb_a_client_implements_is_a_read_or_a_write`).
- The Frame uses no binding on a set it has not identified on this connection, and
  sends no removal from a store that does not record the connected set
  (`FrameDisplay._know_the_set`, `_may_remove`; `security-model.md` § What it does
  to the television).
- **The Frame removes from a set only a picture its upload records name there**
  (`DisplayState.uploads_on`, read by `FrameDisplay._remove_orphans`), and sends no
  upload whose record it could not write (`record_sending`). The household's
  pictures, an older Player's and any of its own the records cannot match are left
  where they are, said once per set (`tv.others_left_alone`).
- Nothing under `src/` renames a temporary file into place but `safe_write` and the
  two writers that stream a download (`tests/test_safe_writes.py`).
- No label failure stops a wall: `LabelPanel.draw` never raises (its broad
  `except` with a pragma), and the label runs in its own task.

## Communication & Boundaries

| # | Channel | Transport | Direction | Sync | Contract |
|---|---|---|---|---|---|
| 1 | Client document | `GET /client`, ETag | server → Player | async poll, 30 s | `player-contract.md` § Transport; `pull.ClientPull.fetch` |
| 2 | Client heartbeat | `POST /client/heartbeat`, JSON: the outputs, the label outputs, the Frame's `device.duid` and the settings page's address (`settings_url`) | Player → server | on change, else every 60 s; one the server refused in its error form is held until it changes or 300 s pass | `client-heartbeat.v1`; `client.client_heartbeat`, `Supervisor._report_outputs`, `ClientPull.report` |
| 3 | Wall feed | `GET /walls/{wall_id}/manifest/v{major}`, ETag, highest major first | server → Player | async poll, `MANIFEST_POLL_SECONDS` (default 1 s) | `player-contract.md` § Major 2, § The cutover; `Pull._request_manifest` |
| 4 | Media | `GET` of each work's `media.url`, resolved against the manifest URL; streamed to a temporary file under a running SHA-256, stopped past the bytes the feed declares; 30 s without a byte ends it, with no total | server → Player | inside a feed poll | `Pull._cache_media` |
| 5 | Wall heartbeat | file in the wall's cache, then `POST /walls/{wall_id}/heartbeat` | Player → server | file every 60 s or on change; POST when the file changes, on the pull's own loop at the poll interval while the server answered the last manifest poll | `heartbeat.v1`; `heartbeat.write`, `Pull._heartbeats`, `Pull._forward_heartbeat` |
| 6 | Label document | `GET /labels/{label_id}`, ETag; each accepted document cached under `CACHE_DIR/.labels/` | server → Player | poll, `LABEL_POLL_SECONDS` (default 1 s) | `labels-and-surfaces.md`; `pull.LabelPull.fetch`, `cached` |
| 7 | Frame art channel | websocket via `samsungtvws` | Player ↔ television | async, with the set's own announcements | foreign; `tv/client.TvClient`, `samsung-tv-state-findings.md` |
| 8 | Frame REST | HTTP via the library's REST helper | Player → television | one-shot reads: `PowerState`, and the set's `device.duid`, read by the client for its heartbeat once a minute (a failed read keeps the last) and by the wall on every new connection, before its first art-mode read of the pass, whatever the set is showing | `SamsungTv.power_state`, `SamsungTv.read_identity`, `FrameIdentity.refresh`, `FrameDisplay._know_the_set` |
| 9 | HDMI | `ioctl` on `/dev/dri/card*` through libdrm (`ctypes`) | Player → screen | blocking, on a thread | `kms.py`; `hdmi-output-findings.md` |
| 10 | E-paper | SPI and GPIO through `omni_epd` | Player → panel | blocking, on a thread | `panel/epaper.py`; `platform-and-dependency-findings.md` § The e-paper panel |
| 11 | Connector discovery | reads `/sys/class/drm` | kernel → Player | each client poll | `client.hdmi_outputs` |
| 13 | Reddit listing and API | `GET` over https to `www.reddit.com`, `reddit_session` cookie as a header; no redirects followed | Reddit → Player | the listing task | foreign; `sources/reddit.py`, `sources/fetch.Fetcher.get_api` |
| 14 | redgifs API | `GET` over https to `api.redgifs.com`, a temporary token taken without an account | redgifs → Player | per clip resolved | foreign; `sources/resolvers/redgifs.py` |
| 15 | Media downloads | `GET` of any host a post links, public addresses only, checked at connect; up to `MAX_REDIRECTS` hops by hand; kept only if the bytes sniff as a picture or clip | host → Player | the fill task | `sources/fetch.Fetcher.download` |
| 16 | mpv | JSON IPC over a Unix socket in `CACHE_DIR/.mpv/` | Player ↔ mpv | blocking, on a thread, each call with a timeout | `mpv.py`; `hdmi-output-findings.md` § mpv |
| 12 | Settings page | HTTP served on `UI_BIND`:`UI_PORT` (default all interfaces, 8771), session cookie; refuses with a 421, before any page, a `Host` that is not an IP address, `localhost`, this machine's own name, `<name>.local` or a name in `UI_ALLOWED_HOSTS` | owner's browser → Player | request/response | `settings_ui/server.py`; `security-model.md` § To the Player: the settings page |

**Trust boundaries.** Channels 1–6 cross the LAN and carry `CLIENT_TOKEN` as a
Bearer header (`Pull._auth`, `ClientPull._auth`, `LabelPull.fetch`). A media URL
on another origin is fetched without the token (`Pull._cache_media`,
`pull._same_origin`), so a manifest cannot send the token elsewhere. Nothing the server sends is
read without a bound (`Pull._cache_media`, `MANIFEST_MAX_BYTES`, `DOCUMENT_MAX_BYTES`).
Every document from the server is parsed and refused whole if it is malformed
(`client.parse_client_document`, `manifest.parse`, `label_rule.parse_label_document`).
Channels 7–8 cross the LAN to a device that authenticates the Player by a token
it issued, held in `TV_TOKEN_FILE` (default `CACHE_DIR/token_file`,
`config._frame`). Arrt's `security-model.md` owns the server side.
Channels 13–15 cross the internet in slideshow mode. The cookie goes to
`www.reddit.com` and nowhere else: there is no cookie jar, media downloads carry
none, and redirects are not followed on API calls. Channel 15 is the one where
the far end is chosen by whoever posted: every address is checked at connection
time to be public (`PublicOnlyResolver`), so a post linking the router cannot
make the Player fetch from it (`sources/fetch.py` docstring;
`security-model.md` § To Reddit: the session cookie).

**Channel 5 is a file first.** The wall loop writes
`CACHE_DIR/<wall_id>/display-heartbeat-<wall_id>.json` atomically
(`heartbeat.write`, through `safe_write.write_whole`: temp, `fsync`, rename) and the pull forwards it when its
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
  .labels/<label_id>.json      last label document accepted for each label this client renders (pull.LABEL_CACHE_DIRNAME; written by LabelPull, removed by __main__.LabelCacheSweep when no mapped label reads it)
  token_file                   the Frame's pairing token, unless TV_TOKEN_FILE says otherwise (config._frame; removed by the settings page when it saves a new TV_ADDRESS)
  <wall_id>/                   one per wall                  (ClientSettings.wall)
    manifest.json              last good feed                (config.CACHED_MANIFEST_FILENAME; written by Pull)
    manifest.etag                                            (pull.ETAG_FILENAME)
    media/sha256-<hash>        verified masters              (manifest.MEDIA_DIRNAME, manifest.media_name)
    composed/                  pictures composed for this display (config.COMPOSED_DIRNAME; Schedule), each flushed to disk before it takes its name
    display-heartbeat-<wall_id>.json                         (heartbeat.HEARTBEAT_FILENAME_TEMPLATE; Wall._beat)
    display-state.sqlite       Frame walls only, schema 5    (config.STATE_FILENAME; state.DisplayState)
    .left.json                 only after the wall left this client: when, and for how long it has been seen gone (client.LEFT_STAMP_FILENAME; client.WallCaches)
  .settings.json               the settings page's file, layout version 2, or 3 while it holds TV_ADDRESS (settings_ui.store.SETTINGS_FILENAME, layout_for)
  .slideshow/<item_id>.<ext>   slideshow media, at most 2 GB (config.SLIDESHOW_MEDIA_DIRNAME; MediaCache)
  .slideshow-listing.json      the last listing taken        (config.SLIDESHOW_LISTING_FILENAME; Slideshow._take)
  .mpv/                        mpv's socket and pid          (config.MPV_RUNTIME_DIRNAME; MpvOutput)
```

**Single writer per file.** Each file above has one writer, named in the
right-hand column. Two walls cannot share a directory: a wall id must match
`config.PLAIN_COMPONENT` (no `..`, no `/`, no leading dot) or
`ClientSettings.wall` raises `WallIdUnusable`, and the supervisor reports it and
does not start the wall. The leading-dot rule is what keeps `.client.json` apart
from every wall directory, and a wall id equal to any other name the cache
directory reserves (the settings file, the token file, the client document and
the rest, derived from the constants that name them) is refused the same way.

**Slideshow mode's files** start with a dot, so no wall id can name them. A
download is written beside its name and moved into place whole; one a crash left
unfinished is removed at the next start (`MediaCache.__init__`). Files of posts
no longer listed are deleted when a listing is taken. The settings file's layout
is versioned: version 2 added the slideshow's keys and version 3 the Frame's address.
A write names the oldest layout that holds its keys (`store.layout_for`), so the
release before this one reads every file this one writes until the page is given
the Frame's address; an older Player refuses a newer layout by name, and rolling
back means deleting the newer keys and setting `version` to 2, or to 1 across the
slideshow's (`settings_ui/store.py` docstring; `deploy/README.md` § The slideshow).

**Atomic writes.** One helper, `safe_write.write_whole`, writes every file the
Player holds in hand: the pull's client document, manifest, ETags and label
documents (`pull._write_atomically`), the heartbeat, a departed wall's stamp, the
settings file (`0600` whatever the umask), the file outputs, the slideshow's saved
listing, and a composed picture (`write_whole_with`). It stages beside the target,
forces the bytes to the card, renames, and leaves no temporary file on any exit; each
caller keeps its own temporary name, and it forces no directory entry, so a cut can
still leave the old file, which is whole. Media streamed as they arrive
(`Pull._cache_media`, `sources.fetch`) keep their own staging. A label
document's modification time is the instant the server last answered for it
(moved forward at most once a minute, `LABEL_ANSWER_STAMP_SECONDS`), and its ETag
is not kept, so a restarted Player always asks for the whole document. A composed
picture found on disk is read once per file stamp and removed if it is not a
whole JPEG, so the work is composed again (`compose.is_intact`,
`Schedule._intact`). Media are evicted only
after a new feed is cached, and a master either feed names is kept
(`Pull._evict`, called with `held | previous`).

**The Frame's store.** `state/store.py:DisplayState`: tables `tv_binding`,
`daemon_state`, `tv_binding_aside` and `tv_upload`, version in `PRAGMA user_version`
(`SCHEMA_VERSION = 5`; the migration is `_widen`, 2 to 3 to 4 to 5 in one function,
and `data-model.md` § DisplayState has the columns). A store from a newer Player
raises `StateSchemaTooNew`, and `__main__.run_frame_wall` parks that one wall
(`daemon.state_too_new`) rather than opening it. `tv_binding` cannot hold
`uploaded` without a content id (the table's check constraint, per the module
docstring). **The store records which television its bindings were made on**
(`daemon_state.tv_identity`, the set's `device.duid`): a content id is the set's
own, so on another set the rows are moved aside under the old identity in one
transaction, the work on the wall and the set's own-slideshow flag go with them,
and the wall's running time with a feed (`feed_seconds`) stays, because it is the
wall's and not a set's. **`tv_upload` records every picture the Player sends,
before it is sent, with the set it goes to and the `image_date` marker it carries**:
what the Player may remove from a set is what those rows name there, and nothing
else. The store is the only per-device state that costs anything to lose: losing it
costs re-uploading the theme to the set, and the Player's record of what it put there,
so the pictures already on the set stay until somebody removes them.

**Not persisted.** Brightness (recomputed from the sun, `brightness.py`), the
supervisor's view of running workers, every `ReportOnce` episode, the label's
ETag, and the label renderer's own state (`LabelRenderer._document` starts from
the cached document, `LabelRenderer._seed`). A restart re-announces any
condition still true.

## Failure Modes & Resilience

The intended answer to every failure below is that the walls keep showing what is
cached. Arrt's `nonfunctional-requirements.md` § Direction is the norm;
`tests/test_pull.py::test_the_server_stopped_while_the_wall_runs_and_the_schedule_continues_from_the_cache`
is its test.

| Failure | What happens | Bounds |
|---|---|---|
| Server unreachable, 5xx, timeout | Walls rotate from cache; feed poll backs off; heartbeat POSTs stop; client document kept | `Pull`: 2 s doubling to 60 s (`BACKOFF_START_SECONDS`, `BACKOFF_MAX_SECONDS`), request 60 s total / 10 s connect, a master 30 s without a byte and no total (`_MEDIA_TIMEOUT`). `ClientPull`: fixed 30 s poll, 30 s / 10 s. `LabelPull`: 1 s poll, 10 s / 5 s |
| Server down at boot | `Supervisor.run` reconciles from `.client.json` before the first request | — |
| Token refused (401), wall not this client's (403), nothing published (404 at every major) | Logged once per status per episode; cache kept; poll continues at the normal interval | `Pull._report_refused` |
| Feed unparseable, past 16 MiB, staging a work twice, or an unknown major | Refused before caching; the last good feed stays, said once per episode (`pull.manifest_refused`) and the next poll asks again | `Pull._pull_manifest`, `_walk`, `manifest.Watcher.poll` |
| A master 404s, fails its hash, runs past the bytes its feed declares, or has no size declared | That work is skipped; the rest of the feed is cached | `Pull._cache_media` |
| A master fails transiently (a 5xx, a timeout, a dropped connection) | The feed already cached stays and the poll backs off, for at most 10 minutes; the heartbeat still goes, on its own loop. Then the new feed is cached without that master and without an ETag, and the master is asked for again on every poll until it arrives | `MEDIA_HOLD_SECONDS`, `Pull._hold_or_adopt`, `Pull._heartbeats`, `_Poll.MEDIA_FAILING`; `pull.media_failing`, `pull.hold_ended` |
| The server's wall heartbeat answer is a 4xx or a 5xx | A 4xx is not sent again until the report changes; a 5xx is sent again next poll | `Pull._heard` |
| The server refuses the client heartbeat in its error form (any `4xx` but `401`, `408` and `429`) | **The report is held**: not sent again until it changes (a screen plugged in, a new settings address; its instant aside) or 300 s have passed, so a report the server always refuses costs one request in five minutes. A `401`, `408`, `429`, a `5xx`, no answer, or a `4xx` in no error form of the server's (a proxy's page, a framework's own body) is no verdict on the report and it is sent again at the next poll. Said once per episode; the settings page says "Report unreadable" after a `400` until a report is taken, as a server of Arrt's wave 3 or later does (it remembers a refusal in memory until it accepts a report, and forgets it at a restart); a server before that records nothing of a refusal and says "Reporting", then "Not heard from". **A bounded departure from the contract's transport table**, which says only "when the report changes" and names no lapse (`build-plan-audit-wave-3.md` § Decisions made mid-build) | `ClientPull.report`, `NOT_A_VERDICT_ON_THE_REPORT`, `REFUSED_REPORT_RESEND_SECONDS`; `client.heartbeat_refused`, `client.heartbeat_ok` |
| A composed picture is empty, cut short, or not a JPEG | Removed and composed again; a master that cannot be read for a reason that may clear is retried | `Schedule._intact`, `compose.is_intact`; `schedule.composed_damaged`, `schedule.compose_read_failed` |
| The cache refuses a write (a full or read-only disk) | The wall keeps showing the feed already cached; the new feed is not cached and **no ETag is stored for it**, so the next poll, under the backoff, is answered whole and tries again; the heartbeat still goes, because the server answered. Said once per episode, and again when it ends. The client document's and a label document's refused writes are caught the same way, the walls and the label following the server meanwhile | `Pull._cache_feed`, `_Poll.UNWRITABLE`, `Pull._report_unwritable`; `pull.cache_unwritable` / `pull.cache_writable`, `client.cache_unwritable` / `client.cache_writable`, `label.cache_unwritable` / `label.cache_writable` |
| A wall's pull raises | `__main__._pull_ended` logs and ends that wall's worker; the supervisor restarts both | 5 s doubling to 300 s (`client.RESTART_MIN_SECONDS`, `RESTART_MAX_SECONDS`); the ladder resets after a run longer than 300 s |
| A wall loop or label renderer raises | `Supervisor._keep_running` logs and restarts it; other walls untouched | same ladder |
| The supervisor raises | The process ends; systemd restarts it | `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0` |
| Television asleep or gone | `TvUnavailable`; the wall holds; a heartbeat is still written | `TV_RETRY_MIN_SECONDS`..`TV_RETRY_MAX_SECONDS` (5 s..300 s); connect 30 s; upload 60 s; selection confirm 8 s (`config` defaults) |
| Television in use (not art mode), or its power cannot be read | Nothing is sent to it: a selection is held whenever the set is not showing art, and an upload, removal, brightness change or slideshow switch-off whenever it is not showing art or dark. Held writes share one backoff (`FrameDisplay._writes`, a `frame._WaitedEpisode`) | `TvClient._permit` (`TvNotOurs`), `FrameDisplay.is_ours`, `_hold_the_writes`; Arrt's norm; `tv.writes_held`, `tv.writes_resumed` |
| Selections accepted, none displayed | Said once, rotation stops trying for the pass | `Shown.WALL_UNCHANGED`, `FrameDisplay._report_wall_unchanged` |
| The Frame's store is locked, or its disk or file fails | The pass ends and not the wall: the next comes after a wait on the same ladder as an unreachable set, said once per episode at WARNING for a lock that may clear and at ERROR for a full or read-only disk, an I/O error or a damaged file, and the heartbeat's `last_error` names it. What the store answers afterwards is what is on disk (a write that raised is rolled back). Anything else SQLite raises is this code's mistake and still reaches the supervisor's restart. **An upload whose record cannot be written is not sent**, so a lasting write fault puts no picture on the set: the wall shows what it has already put there, and `store.failed` says why the theme stops filling | `state.store.StoreFault`, `_FAULTS`, `FrameDisplay.went_away`, `_the_store_failed`; `store.busy`, `store.failed`, `store.recovered` |
| The set will not say which set it is | The wall uses no binding and asks again on its own ladder, said once (`tv.unidentified`); after 15 minutes it shows art from an empty store held in memory, uploading the work it shows and then the feed's other works one a pass, removing nothing; an outage drops that store and starts the 15 minutes again. Those uploads are recorded on the device's store with no set. When the set says, the wall returns to the device's store and owes a reconciliation, which takes them off a set they were sent toward (the one the store last recorded, listed at that address) or that said which it is on the connection they went on, found by id and marker, the picture on the wall only once another has been selected. **Known limits, not fixed:** a set that opens its art channel and never gives its id gains one theme of copies per outage and its 15 minutes, and nothing removes them until a set they may be on says; and copies stay on the set for good when it turns out to be a third set, when the store knew no set and the spell was lost, when a restart lands on another set, or when the set does not echo the marker (item 14 of `build-plan-audit-wave-3.md` § Operator's checklist; said once per set in a process, `tv.markers_unconfirmed`). A failure that may have taken the connection without ending the pass starts a new spell with the same store, so a set heard from on the next connection is never taken for the one the earlier uploads went to. The first is retired by measuring whether any set does this (`samsung-tv-state-findings.md` § What is still owed, item 8). The heartbeat reports the television as not established rather than unreachable | `FrameDisplay._know_the_set`, `_become` (entering `GoingOn`), `_the_connection_may_have_gone` (a new spell), `IDENTITY_PATIENCE_SECONDS`; `tv.unidentified`, `tv.unidentified_going_on`, `tv.identified` |
| A different television answers at the Frame's address | Its bindings are set aside under the old identity and it starts as on a first run, with every work uploaded; **what that set already lists is left alone, the household's own pictures included** (said once per set, with how many, `tv.others_left_alone`), and the old set keeps the Player's pictures until it is met again or they are removed there. A store that recorded no set takes the first one read as its own, and its bindings as uploads to it. The client heartbeat's `FrameIdentity` reads the identity again once a minute, so the server hears of the other set within an interval and without a restart (`client.identity_changed`); a failed read keeps the last one. The read is one REST GET a minute, 2 s at most for a set that is asleep, and writes two journal lines for an episode (`client.identity_unreadable`, `client.identity_read`) | `DisplayState.belongs_to`, `FrameDisplay._met`; `binding.set_changed`, `binding.set_adopted` |
| A work no feed has named for 14 days of the wall running | Its image is removed from the set through the gated removal when an upload record of that set names it there, the set's own list confirms it, and the row goes (a picture no record names stays); held while the set shows a programme, and never for the work on the wall | `FrameDisplay._retirements`, `_retire`; `binding.retired`, `binding.retirement_unconfirmed` |
| A wall leaves this Player | Its directory is stamped and removed a week later, counted only while documents are read; never from a document that was not read | `client.WallCaches`, `Supervisor._sweep_walls`; `client.wall_cache_stamped` / `_returned` / `_removed` / `_unsweepable` |
| The Player stops making progress (a supervisor that does not finish a cycle, a slideshow step that does not end), or cannot tell systemd | No `WATCHDOG=1`; after `WatchdogSec` (300 s) systemd stops it with SIGTERM and `Restart=always` starts it again; `READY=1` is owed by every mode or the start fails at `TimeoutStartSec` | `notify.Watchdog`, `PettingSupervisor`, `petting`; `deploy/display.service`; `notify.unusable`, `notify.unsent` |
| HDMI screen absent or the card refuses | The wall rotates unseen; drawn again when a screen returns | `KmsOutput`, `ScreenDisplay._refresh` every poll |
| A screen refuses a picture | The work is skipped and the schedule tries again at its next slot; meanwhile the display asks the output to draw the refused picture again on each following pass, so a draw the output completes on its own is seen and said once, with the work reported as selected | `ScreenDisplay._put_up`, `_refresh`; `screen.draw_failed`, `screen.draw_recovered`, `rotation.selected` |
| A file output's write fails (a Player started with `FILE_OUTPUT_DIR`) | `refresh` writes again on every poll until both the picture and its record have landed, the record last, so a record never names a picture that is not there; a reader tells the one window by `picture_sha256` not matching the picture. A picture the output refused is asked for again by the display (the row above); each episode is said once and its end once | `FileScreenOutput._owed`, `refresh`; `screen.draw_failed` / `screen.draw_recovered`, `screen.refresh_failed` / `screen.refresh_recovered` |
| A connector the kernel lists after the Player started, or stops listing | The walls are matched to the last document again, without waiting for the server to say anything new (it answers 304) | `Supervisor.cycle`, `_current_outputs` |
| A wall is unassigned or moved off an HDMI connector | Its screen is drawn black, once its loop and pull have ended; a screen unplugged at that moment may show its old picture when plugged back in, until another wall is drawn there | `Supervisor._stop`, `__main__.run_screen_wall`, `displays/screen.go_dark`, `KmsOutput.blank`; `screen.blanked`, `screen.blank_failed` |
| The connector goes, or the Player stops, or the server also gives the connector to another wall | The screen is left as it is | `client._why_stopped` |
| A wall is retired while its worker waits to restart after a crash | The worker is called once more, already stopped, to leave its output | `Supervisor._keep_running`; `client.worker_retire_failed`, `client.label_retire_failed` when that call fails |
| Panel will not open, or no text stack, or viewing distance unset | No label; the walls run; the client heartbeat reports the label output not connected | `__main__.label_panel`, `LabelPanel.report` |
| A label is mapped to an output this client holds no panel for | Said once by name and not drawn, never drawn on another panel; its worker waits to be stopped rather than ending | `__main__.run_label`; `label.no_panel` |
| A panel draw hangs | The renderer stops waiting at 15 s; the gate stays shut until the thread returns. At shutdown the panel is left open rather than powered down under the draw | `LABEL_DRAW_BUDGET_SECONDS`, `LabelPanel.close`; `label.close_abandoned` |
| The Player restarts while the server is down | Each label captions from the document cached when the server last answered, for what is left of the rule's 30-minute hold (never more than that from the restart); a cache that cannot be read is absent | `LabelRenderer._seed`, `_held_from_the_cache`, `LabelPull.cached` |
| The wall clock steps while the server is down | The label's hold is measured on the monotonic clock, so it neither blanks at once nor holds for ever | `LabelRenderer._last_answer` |
| The Pi runs out of memory | The kernel starts reclaiming from the Player at 60% of RAM and kills inside the unit at 75%; systemd stops the rest and restarts it | `deploy/display.service`, `MemoryHigh`, `MemoryMax` |
| Heartbeat file cannot be written | Said once; the wall is unaffected | `Wall._beat` |
| Frame store from a newer Player | That wall is parked until a rollout | `__main__.run_frame_wall` |
| Slideshow: Reddit gives no listing (refused, cookie rejected, rate limited, unreachable) | The last listing plays on from the cache; asked again later; the page says why | `FAILED_REFRESH_S` (120 s) or Reddit's retry-after; `Slideshow._take` |
| Slideshow: Reddit lists nothing showable | Taken as no listing, not as "delete everything"; the cache plays on | `slideshow.listed_nothing` |
| Slideshow: a restart while Reddit is down | The saved listing plays from the cache | `Slideshow.run`, `load_listing` |
| Slideshow: a download fails | That item waits before being tried again; a run of unreachable items is taken as the network being down, and those are not held back | `RETRY_AFTER_S` (30 min), `UNREACHABLE_RUN` (3) |
| Slideshow: a host resolver fails | The item keeps Reddit's own copy | `resolvers._fell_back` |
| Slideshow: the media cache is full | The fill stops until the listing moves on | `CAP_BYTES` (2 GB), `MAX_FILE_BYTES` (200 MB) |
| Slideshow: mpv missing, or the account not in `render` | Said once at start in the journal and on the page; the slideshow, page and label run on | `mpv.renderer_problems` |
| Slideshow: mpv will not start, dies, or reports a failure line | That item is not shown; the next waits `NOT_SHOWN_PAUSE_S`; mpv is restarted with backoff | `RESTART_BACKOFF_SECONDS` (1 s doubling to 60 s), `START_TIMEOUT_SECONDS` 10, `LOAD_TIMEOUT_SECONDS` 15 |
| Slideshow: a Player killed outright leaves mpv holding the screen | Stopped at the next start | `mpv.stop_stale` |
| Disk full | A write the pull makes is the cache row above; the heartbeat file and the Frame's store say theirs (`heartbeat.failed`, `store.failed`). Nothing checks free space ahead | — |

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
  migration parks its Frame wall: a release that writes schema 2 refuses a store
  at 3, 4 or 5 by name (measured on 2026-11-19 against the code at the end of wave 1,
  with `PRAGMA user_version = 3` still refused and `= 2` opening it, and again on
  2026-11-25 with v0.8.0 against a schema-5 store). **What that release then does
  is the old sweep, and the rollback is the owner's to allow each time**
  (2026-11-25): v0.8.0 meets the schema-5 store, journals `daemon.state_too_new`,
  parks only the Frame wall and touches nothing on the set. Only after the runbook's
  step 19b sets the store's version to 2, or after the store is deleted, does its
  sweep remove every uploaded picture its records do not name: the household's, an
  older Player's leftovers, even the one on screen. The way back is stopping the
  Player and setting `PRAGMA user_version` to what the older release writes, on
  each Frame wall's store (the runbook's § Going back); the older release ignores
  the new columns and tables, at the cost of one re-upload per work, and rolling
  forward again forgets the recorded identity and takes the first one read.
- **System packages.** Slideshow mode needs mpv from the distribution (`apt-get
  install --no-install-recommends mpv`) and the account in `render`
  (`deploy/display.service`, `SupplementaryGroups`). Neither is in the lockfile;
  `platform-and-dependency-findings.md` § mpv records the measured version.
- **The unit is frozen, bounded, partly sandboxed and watched.** `ExecStart` is
  `uv run --frozen --no-dev --group raster --group epaper`, so a start never
  re-resolves; `MemoryHigh` and `MemoryMax` are shares of the Pi's RAM;
  `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `ProtectHome`,
  `RestrictAddressFamilies` and the restrictions that move no path or device
  (`LockPersonality`, `RestrictNamespaces`, `RestrictSUIDSGID`, `RestrictRealtime`,
  `ProtectHostname`, `ProtectKernelLogs`, `ProtectControlGroups`,
  `SystemCallArchitectures=native`, an empty `CapabilityBoundingSet`) limit what
  the Player and its mpv can reach, and nothing sets `PrivateDevices`,
  `DevicePolicy` or `ProtectClock` (`deploy/display.service`,
  `tests/test_deploy_unit.py`). It is `Type=notify`: **`NotifyAccess=all`, not
  `main`, because the unit's main process is `uv`**, which starts the Player's
  Python as its child, and `WatchdogSec=300` restarts a Player that stops
  making progress (above). mpv runs as the same account and can read the settings,
  which is the owner's accepted state (2026-11-19). Whether the kernel enforces the
  memory bound, whether the sandbox lets the Player draw, and whether each mode
  reaches `active` and the watchdog restarts a stopped one, are the operator's
  checks at the Pi (the runbook's steps 9a and 13a to 14c).
- **A `*_SECONDS` setting of zero, below zero, `nan` or `inf` is refused by name**
  at start (exit 2), as are latitude and longitude out of range
  (`config._seconds`, `config._float`), so an `.env` that held a zero interval stops the Player at
  the first start after the release.
- **Settings retired by a release are refused by name** (`config.RETIRED_SETTINGS`),
  so an old `.env` fails at start rather than being half-read.

## Scaling Model

Not a concern at this size: one household, a handful of outputs per Pi. The
first limit is the Pi's own: a master is streamed to disk and hashed as it comes
(`Pull._cache_media`), so its size costs time rather than memory, and what does
cost memory is composing a picture, about a gigabyte a work, one at a time per
wall; the unit bounds the Player's whole share (`MemoryHigh`, `MemoryMax`).
Composition and the label draw are seconds of CPU on a thread. Nothing here is designed to scale out.

## Cross-Cutting Runtime Concerns

- **Time.** `wall.Clock` carries two readings: `now()` (aware local time) for the
  schedule and the sun, `monotonic()` for every interval and backoff, so an NTP
  step on a Pi with no RTC cannot stall or fire waits (`Clock`'s docstring;
  `episodes.Backoff`). Instants in documents are RFC 3339 with an offset
  (`player-contract.md` § Time). The server's clock and the Player's are not
  reconciled; the label rule's offline hold is
  measured on the monotonic clock from the server's last answer
  (`LabelRenderer._last_answer`), and after a restart from the cached
  document's stamp, bounded from the restart (`LabelRenderer._held_from_the_cache`).
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
  | Television | `tv/client.TvClient` (ABC) | `tests/fake_tv.py:FakeTv` (re-exported by `tests/fakes.py`) subclasses it, so a new verb fails the double at import; it keeps a clock of its own for the tests that wait on the set | `displays/frame.frame_wall` takes a `TvClient`; only `__main__.frame_tv` builds `SamsungTv` |
  | Label surface | `panel/surface.LabelSurface` (ABC) | `tests/fakes.py:FakeSurface` for the renderer; `tests/test_epaper.py:FakeEpd` and `FlatRasterizer` for `EpaperSurface` itself | `panel/epaper.EpaperSurface` takes its driver (`epd=`) and a rasterizer as arguments; `omni_epd` is imported only inside `open_panel` |
  | HDMI | `displays/screen.ScreenOutput` (Protocol); `kms.Device` (Protocol) | `tests/fakes.py:RecordingOutput`; `tests/fakes.py:drm_tree` builds a fake `/sys/class/drm` | `client.hdmi_outputs(drm_root=…)` and `kms.KmsOutput(drm_root=…, dev_root=…, device_for=…)` are injectable |
  | Server | `client.ClientLink` (Protocol) for the supervisor; real `aiohttp` against a local server for the pull | `tests/server_double.py:ServerDouble`, an `aiohttp.web` app on the contract's own routes that decides which case a request is and answers from the contract's transport table (`arrt_contract.transport()`: each answer is a row of it), validating client heartbeats against `client-heartbeat.v1` and wall heartbeats against `heartbeat.v1`, served by `aiohttp.test_utils.TestServer`; `tests/test_transport_table.py` replays every row against the double and the Player's reaction | tests set `server_url` to the test server's URL (`tests/test_pull.py`) |
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
7. **The panels belong to the client, not to a wall**: opened once per process,
   held by output name, and mapped by the server (`__main__.label_panels`; Arrt's
   `labels-and-surfaces.md`). One panel is the configuration's limit by the
   owner's ruling of 2026-11-19, not the design's; the ruling also says nothing is
   to be designed that closes off several, or a Player with labels and no
   display.
8. **Slideshow mode is a second mode, not a second kind of wall** (owner,
   2026-11-10): no server, one listing, one HDMI connector, never at the same
   time as Arrt mode (`config` docstring; `build-plan-slideshow.md` § Goals).
9. **Moving media goes through mpv, a process the Player owns**, not through
   `kms.py`'s buffers, because only mpv has the Pi's hardware decoder and scaler
   (`mpv.py` docstring; `hdmi-output-findings.md` § mpv). It is handed files,
   never URLs, so it adds no network client.
10. **A second HTTP client, bounded by what it can reach** (`sources/fetch.py`):
    API calls to two named hosts, media from any public address, owner's
    request 2026-11-10 (`tests/test_plane_isolation.py` docstring). It narrows
    decision 3 rather than reversing it: the client cannot learn the server's
    address or token.
11. **The household's rule is kept in the seam, not by each caller.** Every write
    to the set reads its state immediately before it is sent, in a final method
    of `TvClient` that calls the client's `_send_<verb>`, and a verb the
    measurements do not show safe in the television state is held there
    (`tv/client.py` docstring; `samsung-tv-state-findings.md` § What works in each
    state). A verb added later inherits the read.
12. **A feed whose master fails transiently is held for at most ten minutes**, then
    cached without that master and with no ETag, so the wall's schedule does not
    freeze behind one file and the master is still asked for on every poll
    (`pull.py`, `MEDIA_HOLD_SECONDS`).
13. **A wall that leaves an HDMI connector draws it black; a Player that stops
    leaves its screens as they are**, mirroring the labels
    (`displays/screen.py` docstring; `hdmi-output-findings.md`).
14. **A binding belongs to a set, a removal needs a store that knows which, and
    the Player removes only what it recorded uploading**: a set's content ids are
    its own, so the store records `device.duid` and sets another set's bindings
    aside (`state/store.py` docstring), and every upload is recorded on the store
    before it is sent, with the set and the `image_date` marker it carries, so
    that what a listing shows is the Player's only where a record names it there
    (`security-model.md` § What it does to the television). The costs accepted: the
    old set keeps the Player's pictures until it is met again; and the pictures an
    older Player put up and never recorded, the household's own, and an earlier
    generation's stay on a set for good, which the owner ruled on 2026-11-19 and
    accepted before any deploy on 2026-11-25. The rule rests on the set echoing the
    marker in its listing, which has been measured once, on one upload
    (`platform-and-dependency-findings.md` § `upload()` reports failure on uploads
    that succeeded); a set that does not echo it leaves every picture the Player
    uploads there for good.
15. **The watchdog is petted only for progress, never because the loop is
    alive**, and its signal is SIGTERM, so a restart closes the art channel
    (`notify.py` docstring; `deploy/display.service`).
16. **A Player can draw to a directory and drive no hardware**, an output kind
    the product ships and never beside hardware, so a real Player process can be
    tested against a real server (`displays/file_output.py` docstring).
17. **A departed wall's directory goes after a week counted only while documents
    are read, and a binding goes after 14 days counted only while the wall runs
    with a feed**: both on the elapsed clock, so a month switched off or a clock
    step retires nothing (`client.WallCaches`; `state/store.py` docstring).
18. **A file is written whole by one helper** (`safe_write.py`), so a change in how
    a write survives a power cut reaches every writer at once. It keeps each
    writer's temporary name and adds no directory `fsync`: whether the Pi's ext4
    needs one is a measurement the operator owes (`build-plan-audit-wave-2.md`
    § Operator's checklist, item 8).
19. **The wall's state with its set is one value of four** (`SetPhase`), changed
    only by `FrameDisplay._become`, so an outage, a reconnect to another set and a
    store fault cannot leave it half in one phase and half in another; and the wall's
    running time with a feed is counted and saved on the device's store whichever
    store its bindings come from.
20. **A client heartbeat the server refused in its own error form is held for at
    most five minutes**, and any other answer is no verdict on the report: a
    departure from the pinned contract's table, bounded and recorded with its cost
    (`build-plan-audit-wave-3.md` § Decisions made mid-build).

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
- Arrt names the Player "display" or "the display plane" throughout. This repo
  keeps "display" only where it is an interface: the context variables
  `display_work_id`, `display_wall_id` and `display_label_id` in `logs.py`, the
  unit name `display.service` and its `SyslogIdentifier=display`. A Frame wall's
  lines were filed under `daemon.*` (`FrameDisplay.journal_name`) until
  2026-11-19 and are `frame.*` now; the `daemon.*` events that remain
  (`daemon.misconfigured`, `daemon.setup_mode`, `daemon.state_too_new`) are the
  process's and a parked wall's, not a wall loop's. The unit and journal names are operational
  interfaces; changing them is a migration, not a rename. The stderr message in
  `__main__.main` says "Arrt Player cannot start".
