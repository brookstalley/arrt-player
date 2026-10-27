---
artifact: operational-spec
version: 1
depends_on:
  - artifact: nonfunctional-requirements
last_validated: null
---

# Operational Specification — Arrt Player

One Pi, one systemd unit, one checkout, one `.env`, one cache directory. The
server it pulls from is deployed separately and recorded in Arrt's
`deploy/README.md` and the operator's homelab repository. The operator's own
values (addresses, the cache path on the Pi) are in that private repository and
not here.

This file says what exists and where the procedure lives. It does not restate
procedures: `deploy/README.md` is the deployment record, and
`.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md` is the procedure
for a release.

<!-- OPTIONAL norm home. To record a norm, add a `## Direction` heading with entries (a bold
     **Statement.**, then `Why:`, `Status:`, optional `Retroactivity:` / `Rulings:`) — anatomy
     and rules: /prawduct:methodology norms. Add the heading only with a real entry: the
     advisory probes read a bare `## Direction` heading as ratified norms. With no norms, leave
     this comment; "none to ratify" is recorded through /prawduct:doctor, not here. -->

## Deployment

| | |
|---|---|
| Host | Raspberry Pi 4, Debian 13 (trixie), aarch64 (`project-state.yaml` `platform`; `hdmi-output-findings.md` for the measured host) |
| Unit | `deploy/display.service`, installed to `/etc/systemd/system/display.service` |
| Checkout | `/opt/arrt-player` (the unit's `WorkingDirectory=`; confirmed by the owner 2026-10-26, runbook § The first time) |
| Settings | `/opt/arrt-player/.env`, un-prefixed `EnvironmentFile=`, so a missing file stops the unit before the process exists |
| Interpreter | `/usr/local/bin/uv run python -m arrt_player`, named absolutely |
| Account | A system account named by the unit's `User=`, with `SupplementaryGroups=spi gpio video` |
| Dependency groups | `uv sync --group raster --group epaper` on the Pi. A bare `uv sync` removes both, and the label stops (CLAUDE.md, runbook step 8) |
| Inbound | None. The Player serves nothing; it makes outbound HTTP to `SERVER_URL` and talks to the Frame on its LAN address |

**Guarded properties of the unit**, each held by `tests/test_deploy_unit.py`: the
`EnvironmentFile=` is declared and is the one at the checkout's root; there is no
start rate limit (`StartLimitIntervalSec=0`) and `RestartSec=` is at least 5; no
path is under a home directory; the interpreter is absolute; `TimeoutStopSec` is at
least 45 and `KillSignal=SIGTERM`.

**A release** is `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`:
§ The first time moves a Pi off the shared monorepo checkout once, and § Steps
moves it between revisions of this repository. The release's own asks are in
`deploy/README.md`'s dated sections and `.env.example`, which runbook step 2 diffs.
The runbook's header records that it has not yet been run against the standalone
checkout and that § The first time has never been run.

**Order with the server.** Server first unless a release says otherwise (runbook
§ When to use this; `deploy/README.md` § Displays and labels). A Player older than
the server's manifest major refuses the unknown major and keeps the last manifest
(`player-contract.md` § The cutover).

**Stale in `deploy/README.md`, not repeated here.** The file's opening note says
its dated sections were written against the monorepo checkout and stay as written.
Several undated or procedural sections still read as current and are not:
§ The two new units, and where everything they name now lives (its table names
the old checkout and `ART_ROOT`); § Moving a running deployment onto a newer
revision (`WALL_ID`, the curation plane); § Looking at the label without the
daemon and § Measuring what the power keys do (the old checkout path); and § How
to tell your own install worked (reads files under the old art root, names
`curation.service` and `WALL_ID`, and carries a 2026-09-30 note saying to rewrite
it when wave 3 landed). For the current checks, use runbook steps 13–16.

### Configuration

`.env.example` is the specification. Three values are required and have no
default: `SERVER_URL`, `CLIENT_TOKEN`, `CACHE_DIR` (`config.load`). `TV_ADDRESS`
gives the client a Frame output and makes `LOCATION_NAME`, `LATITUDE` and
`LONGITUDE` required (`config._frame`). `EPD_DEVICE` gives it a label output, which
draws only once `EPD_PANEL_DIAGONAL_INCHES` and `EPD_VIEWING_DISTANCE_INCHES` are
set. Values in the process environment win over `.env` (`load_dotenv()` without
`override`).

Some intervals are constants in code and not settable from `.env`: the client poll
(30 s), the label poll (1 s), the heartbeat (60 s), the pull's backoff (2–60 s), the
HTTP timeouts, and the worker restart backoff (5–300 s). Each is listed with where it
is set in `nonfunctional-requirements.md`.

### Continuous integration

`.github/workflows/suites.yml`, on every pull request, on pushes to `main`, and by
hand. Three jobs, each with a 15-minute limit:

- **The suite**: `uv run pytest tests --ignore=tests/raster`, then
  `.github/scripts/assert_tests_ran.py` fails the job if nothing ran.
- **The typesetting**: installs cairo, Pango, girepository headers and a font, then
  `uv run --group raster pytest tests/raster`, with the same guard.
  `tests/test_ci_scope.py` holds the two jobs to covering every test directory.
- **Lint and format**: `ruff check` and `black --check`.

Nothing in CI reaches a television or a panel. The suite drives a double behind
the TV interface (`tests/fakes.py`) and a stand-in server (`tests/server_double.py`).
The contract is taken at the tag in `pyproject.toml` (`[tool.uv.sources]`,
`contract-v1.0.0` today).

### The hand-run tools

Each one talks to real hardware, runs on the Pi from the checkout with the
deployment's `.env`, and needs `display.service` stopped first: the daemon holds
the art channel and the SPI bus. None is run by the daemon or by CI; their pure
parts are tested (`tests/test_tools.py`, `test_power_probe.py`,
`test_panel_check.py`, `test_tv_delete.py`).

| Tool | What it is for | Touches |
|---|---|---|
| `tools/tv_api_check.py` | Exercises the art API against the live set and reports what it does; non-zero exit if any check failed. Re-run after any firmware update (Arrt's `operational-spec.md` § Routine Operations) | With `--image`, uploads an image, selects it only if the set reports art mode `on`, then removes the one it uploaded |
| `tools/panel_check.py` | Not a command. The decision `tv_api_check.py` uses to warn when `TV_PANEL_DIAGONAL_INCHES` disagrees with the set's `modelName` | Nothing itself |
| `tools/tv_delete.py` | Not a command. Deletion confirmed by re-reading the set's list; `tv_api_check.py` uses it | The set's art list |
| `tools/label_preview.py` | Renders the label chain (metadata, layout, Pango) to a PNG, or with `--panel` onto the panel, so legibility can be judged at the wall (`deploy/README.md` § Looking at the label without the daemon) | The panel, with `--panel` |
| `tools/power_probe.py` | Measures what a power key does from each state. Reads only, unless `--click` or `--hold` is given with `--i-am-at-the-set`. The only code in this repository that presses power (`deploy/README.md` § Measuring what the power keys do) | The set's power; a second token file, `<TV_TOKEN_FILE>_probe_remote` |

Their docstrings still show `cd arrt-player && …`, the monorepo layout. From the
standalone checkout the command runs from the checkout's root.

## Backup and Recovery

**Nothing on the Pi is backed up, by design.** Arrt's `nonfunctional-requirements.md`
§ Durability classes the Player's cache and its TV bindings as device state that a
Player rebuilds by pulling again. What each loss costs:

| Lost | Cost | Recovery |
|---|---|---|
| A wall's media or manifest under `CACHE_DIR` | A re-pull from the server. While the server is also down, the wall has nothing to show | Automatic once the server answers |
| `CACHE_DIR/.client.json` | Until the server answers, a restarted client knows no walls (`ClientPull.cached`) | Automatic once the server answers |
| A Frame wall's `display-state.sqlite` | Reconciliation removes every image on the set it cannot account for and uploads the wall again. `deploy/README.md` records about 3.5 minutes for 40 works on a fresh device | Automatic |
| The TV token file (default `$CACHE_DIR/token_file`, so wiping `CACHE_DIR` loses it) | A pairing prompt on the television, which somebody has to accept at the set | By hand, at the set |
| `.env` | The client token is shown once by the server and kept only in `.env` | Issue a new token on the server (Settings › Clients) and rebuild `.env` from `.env.example` |

**One state change is one-way.** `display-state.sqlite` carries a schema version
(`state/store.SCHEMA_VERSION`, 2 today). A Player older than the store refuses it
and parks that wall (`StateSchemaTooNew`). So rolling back across a release that
bumps it needs the store removed, at the cost of a re-upload. The runbook's
§ Going back says "Nothing on the Pi migrates", which holds only while no release
between the two revisions bumps the version.

## Monitoring

Nothing pages anyone. A failure is noticed when somebody looks at one of these.

- **The server's view of the client.** Settings › Clients, or
  `GET /api/clients` (`heartbeat.age_seconds`, `heartbeat.problem`), and
  `GET /api/walls` (`reads_feed`, `feed_notice`). Runbook steps 15–16 give the
  commands and the expected values. While the server is reachable, the client
  heartbeat is sent at least every 60 s (`client.Supervisor._report_outputs`); the
  contract calls a report stale after three intervals, 180 s
  (`arrt_contract.timing()`, held by `tests/test_heartbeat_interval.py`).
- **The wall heartbeat** reports, per wall, the manifest schema, the current work,
  the display state, whether the set answered and whether it showed art, and the
  last error, which is not cleared by a good pass (`heartbeat.Health`). A
  `reported_at` that does not advance is worse than an error: the loop has stopped.
- **The journal**: `journalctl -u display.service`, `SyslogIdentifier=display`, one
  JSON object per line with an `event` key and, where it concerns a work, a
  `work_id` (`logs.py`). Each trouble is logged once when it starts and once when it
  ends (`episodes.ReportOnce`), so a quiet journal is not proof of health. The
  journal is in RAM and does not survive a reboot (Arrt's `operational-spec.md`
  § Risks); after a reboot, diagnose from the server's heartbeat age.

Events worth searching for: `client.started`, `client.wall_started`,
`pull.adopted`, `rotation.selected` (a working start); `pull.unreachable`,
`client.unreachable` (server gone, wall unaffected); `pull.refused`,
`client.refused` (a 401 or 403 is configuration); `pull.media_failing`,
`pull.work_skipped`; `pull.crashed`, `client.worker_crashed`,
`client.worker_ended`, `client.label_crashed`; `rotation.wall_not_ours` (the set is
in use, correctly left alone); `rotation.wall_unchanged`, `tv.unavailable`;
`daemon.misconfigured`, `daemon.state_too_new`; `panel.unavailable`,
`panel.type_scale`, `label.failed`; `heartbeat.failed`; `screen.absent`.

## Failure Recovery

Most failures recover without anyone. The table is what a person does when they
do not. The runbook's § If this doesn't work covers a release that will not start.

| Symptom | First check | Action |
|---|---|---|
| Unit not running after a reboot or a release | `systemctl status display.service`, not the journal | A missing `.env` or `/usr/local/bin/uv` is refused by systemd before the process exists and is named there. `systemctl is-enabled` if it did not start at boot |
| Unit restarting every 5 s | `journalctl -u display.service` for `daemon.misconfigured`, or stderr `display plane cannot start: …` | The message names the variable. Fix `.env` (runbook step 7) and start it |
| Wall frozen on one picture | Server: heartbeat age for the wall | Stale: `systemctl status`, then the journal. Fresh with `display_state` `in_use` or `dark`: somebody has the set, and that is correct |
| Server down or restarting | `pull.unreachable` in the journal | Nothing. The wall rotates its cache; the pull backs off to 60 s and resumes on its own |
| `pull.refused` or `client.refused` with 401 | The client token | Re-issue on the server, update `CLIENT_TOKEN`, restart |
| `pull.refused` with 403 or 404 | The wall's assignment on the server | Assign the wall to this client and output, or hang a theme on it |
| A wall worker crashing repeatedly | `client.worker_crashed` tracebacks | The other walls keep running; the worker retries at up to 300 s. Fix the cause; a restart of the unit is not needed |
| `daemon.state_too_new` | The revision on the Pi | Roll forward. Or remove that wall's `display-state.sqlite` and accept a re-upload |
| Label blank or stale, wall fine | `panel.unavailable` at start, `label.failed` later | The message names the cause: unstated viewing conditions, missing text stack (`uv sync --group raster --group epaper`), or a panel that will not open. The walls are unaffected by design |
| Art channel refused for minutes | `tv.unavailable` repeating | Wait. The set holds abandoned clients for minutes (`samsung-tv-state-findings.md` § The art channel can refuse to open). Do not SIGKILL or restart in a loop, which makes it worse |
| Pairing prompt on the television | The token file | The token file was empty or lost. Accept the prompt at the set |
| HDMI wall shows the text console | `systemctl is-active display.service` | Expected while the Player is stopped (`deploy/README.md` § The Player as a client of the NAS). `screen.absent` while running means no screen seen on the connector (`hdmi-output-findings.md`) |

**Stopping it.** `sudo systemctl stop display.service`, which sends SIGTERM and
waits up to 90 s. Never SIGKILL: the set holds the abandoned art channel for
minutes (runbook step 3).

## Discrepancies with Arrt's record

Arrt's operational text about the Player that is now wrong for this repository.
None is fixed here; each is a change on Arrt.

1. **Deploy and rollback.** Arrt's `operational-spec.md` § Routine Operations says
   the Player deploys "with `git pull` and a restart, until the wave-5 repo split".
   The split has happened. A release is a detached checkout of a commit in
   `/opt/arrt-player`, and the procedure is this repository's runbook.
2. **Start-up failure.** Arrt § Failure Recovery, "Both planes start, then exit at
   import", describes `ART_ROOT` resolution in `config.py` at import. The Player has
   no `ART_ROOT`. It resolves `.env` in `config.load`, called from `__main__._run`,
   and exits 2 naming `SERVER_URL`, `CLIENT_TOKEN`, `CACHE_DIR` or a retired setting.
3. **Re-pairing.** Arrt § Routine Operations, "Re-pair the TV", says the token
   resolves under `ART_ROOT`. The Player's default is `$CACHE_DIR/token_file`
   (`config._frame`), set otherwise by `TV_TOKEN_FILE`.
4. **The heartbeat's file.** Arrt § Risks says the heartbeat writer writes into the
   art root on the Pi, and that after wave 3 it stops writing to the card. The
   Player writes it into `CACHE_DIR/<wall id>/` and posts it; it still writes to
   the card (`nonfunctional-requirements.md` here, § Discrepancies, item 3).
5. **`tv_api_check.py`.** Arrt § Routine Operations and § Risks name
   `python tv_api_check.py`. It is `tools/tv_api_check.py` in this repository, run
   with `uv run` from the checkout.
6. **Units.** Arrt § Deployment Target and § Process Management describe two
   units on the Pi. The Pi runs `display.service` alone. `curation.service` was
   stopped and disabled on 2026-10-02 (`deploy/README.md`, opening note).
