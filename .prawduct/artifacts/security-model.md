---
artifact: security-model
version: 1
depends_on:
  - artifact: product-brief
  - artifact: data-model
last_validated: null
---

# Security Model — Arrt Player

**What this is.** The Player's own security posture: the two credentials it
holds, the two boundaries it crosses, what it accepts from each side, how the
deploy unit runs it, and what it must never do to the television. Written
2026-10-27 from the code at `b82ba74`. Every claim names the file and symbol it
was read from; where it says "never" or "only", it says what enforces it.

**Proportion.** One household, one operator, a Pi on a home LAN. The Player
serves nothing: no code in `src/` opens a listening socket (searched 2026-10-27
for `web.Application`, `start_server`, `TCPSite`, `HTTPServer`, `.bind(`,
`listen(`). It makes outbound requests to two peers and draws on local
hardware. The realistic attacker is something already on the LAN.

**What is Arrt's.** The server's side of the trust boundary (LAN-only, no login,
the public-exposure cliff) is Arrt's `security-model.md` § Trust Boundary. The
client token's issue, verifier, rotation and blast radius are Arrt's
`security-model.md` § Credentials and Secrets › Inventory. The history of the TV
token leak is Arrt § `token_file` was a leak, and it is closed. The rule that
the television belongs to whoever is using it is Arrt's
`nonfunctional-requirements.md` § The television belongs to whoever is using it.
This file states how the Player holds up its end of each.

No norm is ratified here, so there is no § Direction.

## Authentication

The Player authenticates outward only. Nothing authenticates to it.

### To the server: the client token

- **Held** in `CLIENT_TOKEN`, read from the environment or `.env`
  (`config.py:load`), kept on `ClientSettings.client_token` and
  `WallSettings.client_token`, both `field(repr=False)`.
- **Sent** as `Authorization: Bearer <token>` on every request to the server's
  routes: `GET /client`, `POST /client/heartbeat` (`pull.py:ClientPull._auth`),
  `GET /walls/{id}/manifest/v{major}`, `POST /walls/{id}/heartbeat`
  (`pull.py:Pull._auth`), `GET /labels/{id}` (`pull.py:LabelPull.fetch`).
- **Sent with a master only to the server's own origin.** `pull.py:Pull._cache_media`
  adds the header only when `pull.py:_same_origin` matches the master's resolved
  URL to `SERVER_URL` on scheme and netloc. A master on another host is fetched
  with no token (`tests/test_pull.py::test_the_token_is_not_sent_to_another_host`).
- **Dropped on a cross-origin redirect by aiohttp, not by the Player.** aiohttp
  3.14.3 (`client.py`, the redirect loop) pops `Authorization` when the redirect
  origin differs; `pyproject.toml` floors aiohttp at 3.14. No Player test covers
  a redirect.
- **Transport is whatever `SERVER_URL` says.** `config.py:load` does not require
  `https`. On `http`, the token crosses the LAN in clear, which Arrt's LAN-only
  boundary accepts. No certificate is pinned.
- **A refusal** (401, 403) is logged once per episode with the status and the
  name `CLIENT_TOKEN`, never the value (`Pull._report_refused`,
  `ClientPull.fetch`, `LabelPull.fetch`). The cache is kept.

### To the television: the pairing token

- **Held** in a file at `TV_TOKEN_FILE`, default `CACHE_DIR/token_file`
  (`config.py:_frame`). Must be absolute when set (`config.py:_absolute`), so a
  redeploy that replaces the checkout cannot delete it.
- **Written and read by the `samsungtvws` library, not the Player.**
  `tv/samsung.py:SamsungTv._construct` passes the path. The library writes it with
  `open(path, "w")` on first pairing and reads it on each connection
  (`samsungtvws/connection.py:_set_token`, `_get_token`). The Player sets no mode
  on it, and the unit sets no `UMask=`, so it takes the process umask.
- **First pairing puts a prompt on the set's screen** and needs a person to
  accept it. It happens on construction when the token file is empty
  (`SamsungTv._construct` docstring), before any art-mode check. Changing
  `TV_CLIENT_NAME` costs another (`config.py:DEFAULT_TV_CLIENT_NAME`).
  `tv/samsung.py:_RestOnly` skips the token check, so the identity read never
  triggers a prompt.
- **Sent** in the websocket URL's query string (`samsungtvws/connection.py:_format_websocket_url`).
  On port 8002 the library uses TLS with `CERT_NONE` (`samsungtvws/helper.py:get_ssl_context`);
  on any other `TV_PORT` it uses plain `ws://` (`_is_ssl_connection` is
  `port == 8002`). The set's certificate is self-signed, so the Player does not
  know it is talking to the set it paired with.
- **Kept out of the journal**: the library logs `New token %s` at INFO;
  `logs.py:configure` floors the `samsungtvws` logger at WARNING
  (`_TELEVISION_CLIENT_FLOOR`), held by
  `tests/test_logs.py::test_a_token_logged_at_info_by_that_logger_is_not_emitted`.
  The startup line names the token's path, never its contents
  (`config.py:FrameSettings.frame_lines`).

## Authorization

The Player holds no authority of its own to grant. What it may do is decided by
the server (which walls, which outputs, which label) and bounded by two rules it
enforces on itself.

### What it accepts from the server

The server is trusted for content and assignment, not for anything that would
reach outside the cache or onto another host with the token.

| Input | Checked by | Refused when |
|---|---|---|
| Client document | `client.py:parse_client_document` | Not JSON, not an object, no `client_id`/`name`, `walls` or `labels` not a list, any entry missing a key. Refused whole; walls keep running. |
| Wall id from the document | `config.py:ClientSettings.wall` (`_PLAIN_COMPONENT`) | Not one plain path component: `..`, `/`, a leading dot. The wall is not started. |
| Manifest (feed) | `manifest.py:parse`, `_feed`, `_work`, `_slot`, `_scene`, `_instant` | Wrong major, malformed, a rule of Arrt's `player-contract.md` § Rules a schema cannot state broken, an instant without an offset, a media hash not 64 lowercase hex. Last good feed kept. |
| Master bytes | `pull.py:Pull._cache_media` | SHA-256 does not match the feed. Discarded, never written. |
| Master image | `compose.py:compose` | Pillow cannot decode it, or its decompression-bomb guard fires (`Image.DecompressionBombError` is caught). |
| Label document | `label_rule.py:parse_label_document` | Refused whole; last good one kept. |
| Label text | `panel/pango.py` (`set_text`, never `set_markup`) | Never parsed as markup; a tag is drawn as characters. No `set_markup` or `parse_markup` in `src/` or `tools/` (searched 2026-10-27). |

**Not validated against the contract's JSON Schemas at run time.** `jsonschema`
and `arrt-contract` are dev-only (`pyproject.toml` `[dependency-groups] dev`).
The hand-written readers above are held to the contract's fixtures by
`tests/test_player_contract.py`. Unknown keys are accepted, as the schemas allow.

**The requests the Player makes are the contract's.** Only `pull.py` opens an
HTTP client, and it requests only the routes `arrt_contract.routes()` names
(`tests/test_plane_isolation.py`). The one exception by design is a master's
URL, which the feed names and `urljoin` resolves against the manifest URL; it
may be any host, gets no token, and must match its hash.

### What it does to the television

**The television belongs to whoever is using it** (Arrt's
`nonfunctional-requirements.md`). The Player's side:

- **It never selects a picture on a set that is not in art mode.**
  `displays/frame.py:FrameDisplay.is_ours` reads `get_artmode` before every
  selection, and `programmes/schedule.py:Schedule.step` asks it before `show`.
  Held by `tests/test_frame_wall.py::test_a_television_somebody_is_watching_is_left_alone`
  and `::test_a_republished_schedule_is_not_put_on_a_television_somebody_is_watching`.
- **Shipped code presses no key.** No `send_key`, `KEY_`, `shortcuts`,
  `set_artmode` or wake-on-LAN in `src/` (searched 2026-10-27); the only
  remote-control channel the daemon opens is the library's own token check
  during construction. The read of `PowerState` is one REST GET
  (`tests/test_samsung_client.py::test_the_librarys_power_read_is_a_get_and_nothing_else`).
  No test asserts the absence of a key press in `src/`; the norm index records
  that the daemon's power guard arrives with Player power control.
  `tools/power_probe.py` presses power and refuses to without
  `--i-am-at-the-set` (`tests/test_power_probe.py`).
- **Other verbs are not behind the art-mode gate.** Connecting, switching off the
  set's own slideshow (once, `FrameDisplay._connected`), listing, uploading,
  deleting orphans and setting brightness run in `FrameDisplay.prepare` and
  `after` whatever the set is showing. Upload, delete and slideshow were measured
  invisible in the dark state; in the television state they were "not tried"
  (`samsung-tv-state-findings.md` § What works in each state).
- **It deletes from the set every user upload it cannot account for.**
  `FrameDisplay._remove_orphans` removes every content id in `MY-C0002`
  (`tv/client.py:UPLOADED_CATEGORY`, "my-pictures") that no row of this wall's
  `tv_binding` names. A fresh store therefore clears the category. Whatever else
  shares that category on the set is removed with it.
- **It stops on SIGTERM and closes the art channel** (`__main__.py:_run`,
  `client.py:Supervisor.stop_all`), because a killed client holds the set's slot
  for minutes. The unit sends SIGTERM and waits 90 s (`deploy/display.service`).

## Data Privacy

- **No personal data.** The Player stores artworks' ids, images and label text,
  its own settings, and per-set content ids (`data-model.md`).
- **What leaves the device**, all to the server: the client heartbeat (outputs,
  their screen sizes, and the Frame's `device.duid`;
  `client.py:client_heartbeat`, `FrameIdentity`) and each wall's heartbeat
  (`heartbeat.py:Health.document`, including `last_error` as free text from the
  journal). Nothing goes to any third party.
- **Secrets at rest.** `CLIENT_TOKEN` in `.env`; the deploy runbook installs it
  `0640`, owner `tvpi` (`deploy/README.md`), and nothing in the code checks the
  mode. The TV token in `TV_TOKEN_FILE`, at the process umask (above). Both are
  gitignored (`.gitignore`) and refused if tracked
  (`tests/test_repository.py::test_no_credential_file_is_tracked`).
- **Secrets in logs.** The client token is in no `repr` and no startup line
  (`tests/test_config.py::test_the_client_token_is_in_no_repr_and_no_startup_line`)
  and no pull log line (`tests/test_pull.py::test_the_token_never_reaches_the_journal`).
  `SERVER_URL` is logged at start (`ClientSettings.startup_lines`), so
  credentials written into it as userinfo would be logged too. The TV token: see
  Authentication.
- **The repository is public.** No addresses, hostnames or usernames in tracked
  files (`tests/test_repository.py::test_no_source_file_carries_a_deployment_value`,
  `::test_no_tracked_file_carries_this_machines_deployment_values`, which match
  shapes, not values).
- **Retention.** The journal is volatile and bounded
  (`deploy/journald.conf.d/10-bound-the-journal.conf`). The cache keeps a retired
  wall's directory indefinitely (`data-model.md` § The cache).

## Abuse Prevention

**Not applicable as abuse; the Player has no users and no inbound surface.** What
a hostile or broken peer on the LAN can do, and what bounds it:

| Peer | Can | Bounded by |
|---|---|---|
| A server, or anything holding the client token | Choose what hangs on this client's walls; forge nothing on other clients | Arrt § Inventory; the Player shows only what the feed names |
| A server | Point a master at any URL the Player will GET | No token sent off-origin; bytes discarded unless the hash matches; GET only |
| A server | Send a very large master | `aiohttp.ClientTimeout(total=60)` per request (`Pull.run`); `response.read()` has no size cap |
| A server | Name a wall id that is a path | `_PLAIN_COMPONENT` |
| Anything on the LAN | Impersonate the television (no certificate check) and receive uploads | The LAN boundary; the uploads are public artworks |
| Anything on the LAN with the TV token | Drive the set | Arrt § Inventory ("LAN-scoped") |

### The deploy unit (`deploy/display.service`)

Runs as the unprivileged `tvpi` user and group, with `spi gpio video` added for
the e-paper panel and `/dev/dri`. It refuses to start if `.env` is missing
(un-prefixed `EnvironmentFile=`). It names no systemd sandboxing: no
`NoNewPrivileges=`, `ProtectSystem=`, `ProtectHome=`, `PrivateTmp=`,
`ReadWritePaths=`, `UMask=` or `CapabilityBoundingSet=` (read 2026-10-27).
`tests/test_deploy_unit.py` checks the environment file, the restart policy, the
absolute interpreter path, no home-directory path and the stop timeout, and
nothing about hardening.

### Supply chain

- The TV client is a git fork pinned to a commit (`pyproject.toml`
  `samsungtvws @ git+…@fe95ef1d…`), unowned and dormant
  (`platform-and-dependency-findings.md`). Its TLS and token handling are as
  described above and not the Player's to change.
- The contract package is taken at a released tag (`[tool.uv.sources]`
  `arrt-contract`, `contract-v1.0.0`); `CLAUDE.md` forbids patching it.
- Everything else resolves through `uv.lock`. No provenance policy beyond the
  lockfile, as in Arrt § Supply Chain.

## Discrepancies with Arrt's record

Recorded, not fixed; Arrt's artifacts are changed on Arrt.

- **Arrt's `security-model.md` says "display plane"** throughout (§ Inventory: "The
  display plane holds no credential except…"). The Player is a separate
  repository and a client. The claim itself holds: the Player holds the client
  token and the TV token and nothing else. `config.py:load` is the only reader
  of the environment in `src/` (searched 2026-10-27), and `CLIENT_TOKEN` is the
  only secret among the keys it reads.
- **Arrt § `token_file` was a leak says the TV token "resolves under `ART_ROOT`
  (`config.tv_token_file`, … `tvart.py`)".** The Player has no `ART_ROOT` and no
  `tvart.py`. The token resolves to `TV_TOKEN_FILE`, default
  `CACHE_DIR/token_file` (`config.py:_frame`). The property the passage relies
  on, a path outside the checkout, now holds by `config.py:_absolute`.
- **Arrt § Supply Chain says "The Player stays on the Pi's system interpreter and
  `apt`".** `deploy/display.service` runs `uv run`, its comments say `/var/lib/tvpi`
  holds "uv's cache and its managed interpreter", and `hdmi-output-findings.md`
  records the Player on "uv's CPython 3.14". So the Player's interpreter is
  patched by uv, not `apt`. Not checked on the Pi itself.
