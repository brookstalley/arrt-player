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
`subprocess.Popen` in its own process group and which the Player owns for its
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
| the supervisor | `__main__._run` (awaited directly) | `client.Supervisor.run`: polls `GET /client` every `CLIENT_POLL_SECONDS` (default 30 s, `config.DEFAULT_CLIENT_POLL_SECONDS`), reconciles workers, POSTs the client heartbeat | the process |
| `wall:<wall_id>` | `Supervisor._start` | `Supervisor._keep_running` around `__main__.run_wall` | while the wall is assigned |
| `pull:<wall_id>`, `stop:<wall_id>` | `__main__._beside_its_pull` | `pull.Pull.run` beside the wall loop, and a relay of the supervisor's stop | with their wall's worker |
| `label:<output>` | `Supervisor._start_label` | `_keep_running` around `__main__.run_label` → `label_renderer.LabelRenderer.run` | while the label output is mapped |
| `compose:<wall_id>:<work_id>` | `programmes/schedule.py:Schedule._start` | composing one work for this wall's geometry, on a thread | one composition |
| `settings-page` (slideshow mode) | `__main__.run_slideshow`, beside the programme | as in Arrt mode | the process |
| `slideshow-listing` | `programmes/slideshow.py:Slideshow` | one read of the listing, every `REFRESH_S` (600 s), or `FAILED_REFRESH_S` (120 s) or Reddit's own retry-after after a failure; taken between items | one read |
| `slideshow-fill` | `Slideshow.run` | `MediaCache.fill`: downloads from the play position onward, after each listing taken or when nothing is ready | the programme |
| `slideshow-caption` | `Slideshow` | one label draw for the post on screen | one draw |

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
| `displays/screen` | An HDMI connector as a `Display`, behind `ScreenOutput` | `compose`, `config`, `displays.screen_brightness`, `episodes`, `heartbeat`, `manifest`, `programmes.schedule`, `wall` |
| `displays/screen_brightness` | An HDMI screen's brightness on the sun, over the `ddc.DdcBus` seam | `brightness`, `config`, `ddc`, `episodes`, `wall` |
| `programmes/schedule` | What the wall should show from a major 2 feed; composes each work for the display | `compose`, `episodes`, `logs`, `manifest`, `wall` |
| `manifest` | Parse and watch the cached feed (`Watcher`, `parse`, `REQUESTED_MAJORS = (2,)`) | — |
| `compose` | Mat composition for a geometry (Pillow) | — |
| `kms` | `KmsOutput`/`LibdrmDevice`: libdrm through `ctypes` | `client`, `episodes` |
| `ddc` | A monitor's DDC/CI features over its HDMI connector's I2C bus (`Ddc`, `I2cTransport`, `bus_for`); no `ddcutil` | `client` |
| `tv/client` | The `TvClient` seam and its exceptions | — |
| `tv/samsung` | `SamsungTv`, the only `samsungtvws` user | `tv.client` |
| `state/store` | `DisplayState`, the Frame's SQLite store | — |
| `label_rule` | The label document and the label rule | — |
| `label_renderer` | `LabelPanel` (the device) and `LabelRenderer` (one mapping) | `client`, `episodes`, `label_rule`, `logs`, `panel` |
| `panel/*` | Label content, layout, legibility, raster, the `LabelSurface` seam (`surface`), Pango (`pango`), the e-paper driver (`epaper`) | `panel.*` only |
| `heartbeat` | The wall heartbeat document and its atomic write | — |
| `logs` | JSON log shape and `work_id` binding | — |
| `episodes` | `ReportOnce`, `Backoff` | — |
| `config` | `.env`, with the settings page's file beneath it, into `ClientSettings` / `WallSettings` / `Settings` / `UiSettings`; `SetupRequired` | `compose`, `heartbeat`, `settings_ui.store` |
| `settings_ui/store` | The settings page's file, `$CACHE_DIR/.settings.json` | — |
| `settings_ui/auth` | The page's password hash, session cookie, setup code and throttle | — |
| `settings_ui/server` | The settings page (`aiohttp.web`); the Player's one listening socket | `config`, `settings_ui.auth`, `settings_ui.store` |
| `brightness` | Sun-angle brightness (astral), for the Frame and the HDMI screens | — |
| `programmes/slideshow` | Slideshow mode's programme: the listing on its own task, the cache filled on another, playback from what is cached, the caption; the saved listing | `dwell`, `episodes`, `label_rule`, `mpv`, `programmes.slideshow_status`, `sources`, `sources.cache` |
| `programmes/slideshow_status` | What the settings page shows of a running slideshow | `sources` |
| `dwell` | How long an item stays up: a still for the minimum, a clip repeated to reach it and cut at the maximum | — |
| `mpv` | `MpvOutput`, an HDMI connector through an mpv this process runs; `renderer_problems`, `stop_stale` | `client`, `episodes`, `kms` |
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
| 13 | Reddit listing and API | `GET` over https to `www.reddit.com`, `reddit_session` cookie as a header; no redirects followed | Reddit → Player | the listing task | foreign; `sources/reddit.py`, `sources/fetch.Fetcher.get_api` |
| 14 | redgifs API | `GET` over https to `api.redgifs.com`, a temporary token taken without an account | redgifs → Player | per clip resolved | foreign; `sources/resolvers/redgifs.py` |
| 15 | Media downloads | `GET` of any host a post links, public addresses only, checked at connect; up to `MAX_REDIRECTS` hops by hand; kept only if the bytes sniff as a picture or clip | host → Player | the fill task | `sources/fetch.Fetcher.download` |
| 16 | mpv | JSON IPC over a Unix socket in `CACHE_DIR/.mpv/` | Player ↔ mpv | blocking, on a thread, each call with a timeout | `mpv.py`; `hdmi-output-findings.md` § mpv |
| 12 | Settings page | HTTP served on `UI_BIND`:`UI_PORT` (default all interfaces, 8771), session cookie | owner's browser → Player | request/response | `settings_ui/server.py`; `security-model.md` § To the Player: the settings page |

**Trust boundaries.** Channels 1–6 cross the LAN and carry `CLIENT_TOKEN` as a
Bearer header (`Pull._auth`, `ClientPull._auth`, `LabelPull.fetch`). A media URL
on another origin is fetched without the token (`Pull._cache_media`,
`pull._same_origin`), so a manifest cannot send the token elsewhere. Every
document from the server is parsed and refused whole if it is malformed
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
  .settings.json               the settings page's file, layout version 2 (settings_ui.store.SETTINGS_FILENAME)
  .slideshow/<item_id>.<ext>   slideshow media, at most 2 GB (config.SLIDESHOW_MEDIA_DIRNAME; MediaCache)
  .slideshow-listing.json      the last listing taken        (config.SLIDESHOW_LISTING_FILENAME; Slideshow._take)
  .mpv/                        mpv's socket and pid          (config.MPV_RUNTIME_DIRNAME; MpvOutput)
```

**Single writer per file.** Each file above has one writer, named in the
right-hand column. Two walls cannot share a directory: a wall id must match
`config._PLAIN_COMPONENT` (no `..`, no `/`, no leading dot) or
`ClientSettings.wall` raises `WallIdUnusable`, and the supervisor reports it and
does not start the wall. The leading-dot rule is what keeps `.client.json` apart
from every wall directory.

**Slideshow mode's files** start with a dot, so no wall id can name them. A
download is written beside its name and moved into place whole; one a crash left
unfinished is removed at the next start (`MediaCache.__init__`). Files of posts
no longer listed are deleted when a listing is taken. The settings file's layout
is versioned: version 2 added the slideshow's keys, an older Player refuses it by
name, and rolling back means deleting those keys and setting `version` to 1
(`settings_ui/store.py` docstring; `deploy/README.md` § The slideshow).

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
| Slideshow: Reddit gives no listing (refused, cookie rejected, rate limited, unreachable) | The last listing plays on from the cache; asked again later; the page says why | `FAILED_REFRESH_S` (120 s) or Reddit's retry-after; `Slideshow._take` |
| Slideshow: Reddit lists nothing showable | Taken as no listing, not as "delete everything"; the cache plays on | `slideshow.listed_nothing` |
| Slideshow: a restart while Reddit is down | The saved listing plays from the cache | `Slideshow.run`, `load_listing` |
| Slideshow: a download fails | That item waits before being tried again; a run of unreachable items is taken as the network being down, and those are not held back | `RETRY_AFTER_S` (30 min), `UNREACHABLE_RUN` (3) |
| Slideshow: a host resolver fails | The item keeps Reddit's own copy | `resolvers._fell_back` |
| Slideshow: the media cache is full | The fill stops until the listing moves on | `CAP_BYTES` (2 GB), `MAX_FILE_BYTES` (200 MB) |
| Slideshow: mpv missing, or the account not in `render` | Said once at start in the journal and on the page; the slideshow, page and label run on | `mpv.renderer_problems` |
| Slideshow: mpv will not start, dies, or reports a failure line | That item is not shown; the next waits `NOT_SHOWN_PAUSE_S`; mpv is restarted with backoff | `RESTART_BACKOFF_SECONDS` (1 s doubling to 60 s), `START_TIMEOUT_SECONDS` 10, `LOAD_TIMEOUT_SECONDS` 15 |
| Slideshow: a Player killed outright leaves mpv holding the screen | Stopped at the next start | `mpv.stop_stale` |
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
- **System packages.** Slideshow mode needs mpv from the distribution (`apt-get
  install --no-install-recommends mpv`) and the account in `render`
  (`deploy/display.service`, `SupplementaryGroups`). Neither is in the lockfile;
  `platform-and-dependency-findings.md` § mpv records the measured version.
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
