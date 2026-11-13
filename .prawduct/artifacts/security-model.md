---
artifact: security-model
version: 1
depends_on:
  - artifact: product-brief
  - artifact: data-model
last_validated: 2026-11-10
---

# Security Model — Arrt Player

**What this is.** The Player's own security posture: the two credentials it
holds, the two boundaries it crosses, what it accepts from each side, how the
deploy unit runs it, and what it must never do to the television. Written
2026-10-27 from the code at `b82ba74`. Every claim names the file and symbol it
was read from; where it says "never" or "only", it says what enforces it.

**Proportion.** One household, one operator, a Pi on a home LAN. The Player
serves one thing, its own settings page (§ The settings page), and nothing else:
`settings_ui/server.py` is the only file in `src/` that opens a listening socket
(searched 2026-11-10 for `Application(`, `start_server`, `TCPSite`,
`HTTPServer`, `.bind(`, `listen(`; `settings_ui/server.py:address_to_visit`'s UDP
`connect` only asks the kernel for a route and sends nothing). In Arrt mode it
makes outbound requests to two peers, the server and the television. In
slideshow mode it has no server: it calls `www.reddit.com` and `api.redgifs.com`
and downloads media from whatever public host a post links (§ To Reddit: the
session cookie, § Abuse Prevention). It draws on local hardware in both. The
realistic attacker is something already on the LAN, and in slideshow mode also
whoever writes a post in the listing.

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

The Player authenticates outward to two peers, the server and the television, or in slideshow mode to Reddit. One thing authenticates to it: its owner, at the settings page (§ To the Player: the settings page).

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
  (`samsungtvws/connection.py:_set_token`, `_get_token`), so the file takes the
  process umask. **Kept private two ways** (2026-11-11, #3): the unit sets
  `UMask=0077`, so on the Pi it is 0600 from creation, and `SamsungTv.connect`
  sets 0600 after every attempt, which covers runs outside the unit and files an
  older Player left (`tv.token_file_open` if it cannot). The Player never creates
  the file, since the library reads a missing file and an empty one differently.
  Held by `tests/test_samsung_client.py::TestTheTokenFileIsPrivate` and
  `tests/test_deploy_unit.py::test_what_the_player_writes_is_its_own_to_read`.
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

### To the Player: the settings page

The Player's one inbound surface (`settings_ui/server.py`, served from
`__main__.run_settings_page` on `UI_BIND`:`UI_PORT`, default every interface and
8771, `config.py:load_ui`). It can set `SERVER_URL` and `CLIENT_TOKEN`, so whoever
holds it can point the Player at a host of their choosing, which makes it as
sensitive as the token itself.

- **First password needs the setup code.** `__main__.settings_page` mints one
  (`auth.new_setup_code`, 12 random bytes) only when no password is stored, and
  prints it inside the page's address. It is spent before the new password is
  hashed, so two requests carrying it cannot both set one. `/setup` refuses without it
  (`tests/test_settings_ui.py::TestTheFirstRun`), refuses once a password is
  set, and clears it on use (`::test_setting_a_password_signs_you_in_and_spends_the_code`).
  **The code is the one credential the Player prints**, a recorded exception
  (`build-plan-settings-ui.md` § Goals): it is new each start and stops working
  when the password is set, so a journal read afterwards grants nothing.
- **The password** is kept as a salted scrypt hash in `$CACHE_DIR/.settings.json`
  (`auth.hash_password`, n=2^14, r=8), at least `auth.MINIMUM_PASSWORD_LENGTH`
  characters. Wrong passwords are throttled per process, doubling to a minute
  (`auth.Throttle`; `::test_wrong_passwords_are_throttled_and_the_right_one_waits_too`).
  Each attempt is charged before its password is checked, so concurrent attempts
  cannot share one window (`::test_concurrent_wrong_passwords_share_one_window`),
  and scrypt runs on a thread, off the loop that drives the walls.
  To recover a forgotten one, delete `auth.password` from the file and restart.
- **The session** is a cookie holding an expiry and an HMAC-SHA256 over it under
  `auth.session_secret`, stored with the password, so it survives a restart. It
  is HttpOnly, `SameSite=Strict` and lasts 30 days. Setting a password mints a
  new secret, which ends every older session (`TestTheCookie`).
- **Cross-site requests.** Every POST must carry an `Origin` equal to the page's
  own (`server._same_origin_posts`); one with none or another is refused before
  the handler runs (`TestWhatThePageRefuses`). Every response sets a CSP that
  loads its stylesheet and typefaces from the page's own `/static` and nothing
  else, with no inline style and no script (`tests/test_settings_ui_style.py`).
  `/static` is served without a session, since the login page uses it too; it
  holds the stylesheet and two OFL typefaces and nothing secret, and paths
  outside it are refused (`::test_nothing_outside_the_static_directory_is_served`).
  Every response also sets `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: same-origin` (not `no-referrer`, under which a browser sends
  `Origin: null` and every form would be refused) and `no-store`, except `/static`, which a browser may keep a day (`server.SECURITY_HEADERS`, `STATIC_CACHE_CONTROL`).
- **What it can change, and how.** `SERVER_URL`, `CLIENT_TOKEN` and
  `PLAYER_NAME`, and only where the environment does not set them
  (`config.load_ui` › `pinned`); a pinned key is shown read-only and a POST
  cannot change it. **The server's address and its token move together**: when
  the environment pins either, the page locks both, and a new address saved
  without a token clears the stored one, so the page can never send a token to
  a host it was not issued for (`server._locked`, `server._next_values`;
  `tests/test_settings_ui.py::TestSavingTheServer`, `::TestWhatTheEnvironmentPins`).
  Values are judged by the functions `config.load` uses (`server_url_problem`,
  which also refuses userinfo since the address is logged, `client_token_problem`,
  `player_name_problem`). A save restarts the process with SIGTERM to itself.
- **The token is written, never read back.** The token field is always empty;
  the page says only whether one is set
  (`::test_the_token_is_never_shown_only_whether_one_is_set`). A save logs the
  names of the keys it changed, never their values
  (`::test_a_save_logs_which_keys_changed_and_never_their_values`).
- **Changing the password** needs the current one, is throttled like a login,
  and mints a new session secret, which ends every other session
  (`::TestChangingThePassword`).
- **Plain HTTP.** The password and the cookie cross the LAN in the clear, as
  the server's traffic does; this is Arrt's LAN-only boundary
  (Arrt's `security-model.md` § Trust Boundary). `UI_BIND=127.0.0.1` keeps the page
  to the host.
- **What it never says.** No password, hash, session secret or cookie reaches a
  log record (`tests/test_settings_ui.py::test_no_credential_reaches_a_log_record`).
  The settings file is created 0600 (`store.FILE_MODE`) and is gitignored as
  part of `CACHE_DIR`, which is outside the checkout.
- **It is not the pull's channel.** It takes only `aiohttp.web`, never a client,
  and only the entry point imports it (`tests/test_plane_isolation.py`).

### To Reddit: the session cookie

Slideshow mode reads a Reddit listing as the owner's account, with the
`reddit_session` cookie copied from a logged-in browser (owner, 2026-11-10; the
page says how). Reading Reddit this way is probably outside its terms for
automated clients; the owner accepted that the risk is a rate limit or a revoked
cookie.

- **Where it goes.** Only to `www.reddit.com`, and only from
  `sources/fetch.py:Fetcher.get_api`, which adds it for that host alone and
  refuses a caller's own `Cookie` header. Media downloads and redgifs' API carry
  none (`tests/test_fetch.py::test_the_cookie_goes_to_reddit_and_to_no_other_host`,
  `::test_a_caller_cannot_send_a_cookie_header_of_its_own`).
- **What it can do.** Whatever the account can on Reddit, for as long as Reddit
  accepts it: about six months from issue (a JWT `exp`, measured 2026-11-10).
  The Player only reads.
- **Where it is kept.** In `REDDIT_SESSION`, in `.env` or the settings page's
  file (0600, `store.FILE_MODE`), beside the client token.
- **Where it never appears.** No `repr`, no startup line, no log record
  (`tests/test_config.py::TestSlideshowMode::test_the_cookie_is_in_no_repr_and_no_startup_line`,
  `tests/test_fetch.py::test_the_cookie_is_in_no_log_record`,
  `tests/test_slideshow.py::test_a_whole_slideshow_run_logs_no_cookie`). The
  page takes it and never shows it back, the environment's value included
  (`config.UiSettings.pinned_values` holds no secret key;
  `tests/test_settings_ui.py::TestTheSlideshowsSettings`).
- **Its expiry.** The page reads the JWT's `exp`, unverified, and warns two
  weeks ahead (`settings_ui/server.py:EXPIRY_WARNING_S`). Nothing else is
  decided on the claim: whether Reddit still accepts the cookie is told by
  asking it (`sources/reddit.py:RedditSource._why_refused`).

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

**The requests the Player makes to its server are the contract's.** Only
`pull.py` opens an HTTP client to the server, and it requests only the routes `arrt_contract.routes()` names
(`tests/test_plane_isolation.py`). The one exception by design is a master's
URL, which the feed names and `urljoin` resolves against the manifest URL; it
may be any host, gets no token, and must match its hash.

The slideshow, which has no server, makes its requests through
`sources/fetch.py`: listing and API calls to a fixed host set, the Reddit
cookie to `www.reddit.com` only, and media downloads from public addresses only,
every redirect hop included (its docstring, and `tests/test_fetch.py`).

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
- **What leaves the device in Arrt mode**, all to the server: the client heartbeat (outputs,
  their screen sizes, and the Frame's `device.duid`;
  `client.py:client_heartbeat`, `FrameIdentity`) and each wall's heartbeat
  (`heartbeat.py:Health.document`, including `last_error` as free text from the
  journal). Nothing goes to any third party.
- **What leaves the device in slideshow mode**: the listing request and the
  cookie, to Reddit; a request for a temporary token, then a lookup per redgifs
  post, to redgifs (whose token is bound to the caller's address and user
  agent, measured 2026-11-10); and a GET per media file, to whatever host the
  post links. Each request carries `fetch.USER_AGENT`, which names the program
  and no person. No heartbeat: there is no server.
- **Secrets at rest.** `CLIENT_TOKEN` and `REDDIT_SESSION` in `.env` or the
  page's file (0600); the deploy runbook installs it
  `0640`, owner `tvpi` (`deploy/README.md`), and nothing in the code checks the
  mode. The TV token in `TV_TOKEN_FILE`, 0600 (above). Both are
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

**The Player has one user and one inbound surface, the settings page.** What
a hostile or broken peer on the LAN can do, and what bounds it:

| Peer | Can | Bounded by |
|---|---|---|
| A server, or anything holding the client token | Choose what hangs on this client's walls; forge nothing on other clients | Arrt § Inventory; the Player shows only what the feed names |
| A server | Point a master at any URL the Player will GET | No token sent off-origin; bytes discarded unless the hash matches; GET only |
| A server | Send a very large master | `aiohttp.ClientTimeout(total=60)` per request (`Pull.run`); `response.read()` has no size cap |
| A server | Name a wall id that is a path | `_PLAIN_COMPONENT` |
| Anything on the LAN | Impersonate the television (no certificate check) and receive uploads | The LAN boundary; the uploads are public artworks |
| Anything on the LAN with the TV token | Drive the set | Arrt § Inventory ("LAN-scoped") |
| Anything on the LAN | Reach the settings page before its owner, and set its password | The setup code, printed at start and spent on use |
| Anything on the LAN | Guess the page's password | Length floor; a throttle doubling to a minute per attempt |
| A page elsewhere in the owner's browser | Post a form to the settings page | `Origin` check on every POST; `SameSite=Strict` cookie |
| Anything on the LAN that sees the owner's traffic | Read the password or the cookie off the wire | Not bounded: plain HTTP, as for the server (Arrt § Trust Boundary) |
| Whoever posts in the slideshow's listing | Make the Player fetch a LAN address (SSRF through a listing) | Every host, and every redirect hop, resolved and refused unless public, in the resolver aiohttp connects through so a name cannot change between check and connect; at most 5 redirects (`fetch.PublicOnlyResolver`, `_check_target`; `tests/test_fetch.py`) |
| Whoever posts in the slideshow's listing | Send something huge or something that is not media | 200 MB a file, the cache capped at 2 GB, and the bytes, not the header, decide whether it is a picture or a clip (`fetch.MAX_FILE_BYTES`, `cache.CAP_BYTES`, `fetch.sniff`) |
| Whoever posts in the slideshow's listing | Hand mpv a URL, so the renderer becomes a second network client | mpv is given only regular files under the media cache (`mpv.MpvOutput._refusal`; `tests/test_mpv.py`) |
| Whoever posts in the slideshow's listing | Put anything at all on the screen | Not bounded, by the owner's choice: there is no content filter, and what the listing names is the owner's to choose (build plan, 2026-11-10) |

### The deploy unit (`deploy/display.service`)

Runs as the unprivileged `tvpi` user and group, with `spi gpio video` added for
the e-paper panel and `/dev/dri`, `render` for the GPU's render node that mpv
draws through in slideshow mode, and `i2c` for a screen's DDC/CI bus
(`tests/test_deploy_unit.py` checks them). It refuses to start if `.env` is missing
(un-prefixed `EnvironmentFile=`). It sets `UMask=0077` (2026-11-11), so every
file the Player creates is `tvpi`'s alone. Otherwise it names no systemd
sandboxing: no `NoNewPrivileges=`, `ProtectSystem=`, `ProtectHome=`,
`PrivateTmp=`, `ReadWritePaths=` or `CapabilityBoundingSet=` (read 2026-11-11).
`tests/test_deploy_unit.py` checks the environment file, the restart policy, the
absolute interpreter path, no home-directory path, the stop timeout and the
umask, and no other hardening.

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
