---
artifact: security-model
version: 1
depends_on:
  - artifact: product-brief
  - artifact: data-model
last_validated: 2026-11-25
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
- **Kept out of the journal and the heartbeat**: the library logs `New token %s`
  at INFO, and `websockets`, which carries the art channel, logs the request line
  with the token in its URL at DEBUG; `logs.py:configure` floors both loggers at
  WARNING (`_TELEVISION_LOGGERS`, `_TELEVISION_CLIENT_FLOOR`), held by
  `tests/test_logs.py::test_a_token_logged_at_info_by_that_logger_is_not_emitted`
  and its DEBUG sibling. A library failure that quotes the token (in the URL or
  in a reply's `token` field) is written with the value withheld before it is
  logged or kept as the wall's `last_error`, which the heartbeat carries to the
  server (`tv/samsung.py:_TOKEN`, `_named`; `tests/test_samsung_client.py`).
  The startup line names the token's path, never its contents
  (`config.py:FrameSettings.frame_lines`).

### To the Player: the settings page

The Player's one inbound surface (`settings_ui/server.py`, served from
`__main__.run_settings_page` on `UI_BIND`:`UI_PORT`, default every interface and
8771, `config.py:load_ui`). It can set `SERVER_URL` and `CLIENT_TOKEN`, so whoever
holds it can point the Player at a host of their choosing, and the Frame's
address (`TV_ADDRESS`), so whoever holds it can point the Player at a television
of their choosing too. That makes it as sensitive as the token itself.

- **First password needs the setup code.** `__main__.settings_page` mints one
  (`auth.new_setup_code`, 12 random bytes) only when no password is stored, and
  prints it inside the page's address. That line goes to stdout, which the unit
  sends to the journal (`StandardOutput=journal`), where the groups that already
  administer the Pi can read it; the code is new at each start. It is spent before the new password is
  hashed, so two requests carrying it cannot both set one. `/setup` refuses without it
  (`tests/test_settings_ui.py::TestTheFirstRun`), refuses once a password is
  set, and clears it on use (`::test_setting_a_password_signs_you_in_and_spends_the_code`).
  **The code is the one credential the Player prints**, a recorded exception
  (`build-plan-settings-ui.md` § Goals): it is new each start and stops working
  when the password is set, so a journal read afterwards grants nothing.
- **The password** is kept as a salted scrypt hash in `$CACHE_DIR/.settings.json`
  (`auth.hash_password`, n=2^14, r=8), at least `auth.MINIMUM_PASSWORD_LENGTH`
  characters. Wrong passwords are throttled **per source address**, each address's
  wait doubling to a minute, under a ceiling for all addresses together of 30
  wrong in any 10 minutes (`auth.Throttle`, `CEILING_FAILURES`,
  `CEILING_WINDOW_SECONDS`; `::test_wrong_passwords_are_throttled_and_the_right_one_waits_too`,
  `::TestAGuesserCannotLockTheOwnerOut`). One throttle for the process would let a
  guesser delay the owner's right password with wrong ones; per address, a
  guesser at another address does not slow the owner, and the ceiling bounds a
  guesser with many addresses. Two addresses reach the ceiling, so **it does not
  hold an address that has signed in**: a right password, or a visit carrying a
  valid session, gives the address standing for the session's 30 days, a new
  password ends every standing (`Throttle.vouch`, `forget_standing`), and its
  failures are not charged to the ceiling. Such an address is still held by its
  own wait. A device that has never signed in, or not since the password
  changed, is held whenever the ceiling is full, and two guessing addresses keep
  it full for as long as they keep guessing: each slot that ages out goes to
  whoever asks first. **Behind a reverse proxy** every
  caller is the proxy's address: the owner and a guesser through it share one
  wait, and once it has signed in the bound on guessing through it is that one
  address's, about fifteen wrong in ten minutes. Each attempt is charged before its password is checked, so concurrent attempts
  cannot share one window (`::test_concurrent_wrong_passwords_share_one_window`),
  and scrypt runs on a thread, off the loop that drives the walls.
  To recover a forgotten one, delete `auth.password` from the file and restart.
- **The session** is a cookie holding an expiry and an HMAC-SHA256 over it under
  `auth.session_secret`, stored with the password, so it survives a restart. It
  is HttpOnly, `SameSite=Strict` and lasts 30 days. Setting a password mints a
  new secret, which ends every older session (`TestTheCookie`).
- **It answers only to its own names, which closes DNS rebinding.** A web page in
  a browser on the LAN can point its own domain at this Player after loading, and
  its requests then reach the page with an `Origin` and a `Host` that both name
  the rebound domain, so comparing the one with the other passes them. A request
  whose `Host` is not an IP address, `localhost`, this machine's own name,
  `<name>.local`, the address the page is bound to or a name in `UI_ALLOWED_HOSTS`
  is refused with a 421 before any page is served (`server._known_hosts`,
  `settings_page.host_refused`, said once per name per episode, not per request,
  since the caller chooses the rate; `::TestTheNamesItAnswersTo`). A `Host` whose
  port is not ASCII digits naming 1 to 65535 is refused the same way. **Upgrading:** a name only the
  router's DNS gives the Player (`<name>.lan`, say) is refused until it is listed
  in `UI_ALLOWED_HOSTS`, and the refusal says so.
- **Cross-site requests.** Every POST must carry an `Origin` equal to the page's
  own, or a name `UI_ALLOWED_HOSTS` lists (`server._same_origin_posts`,
  `origin_allowed`); one with none or another is refused before
  the handler runs (`TestWhatThePageRefuses`, `::TestWhichOriginsMayPost`). Every response sets a CSP that
  loads its stylesheet and typefaces from the page's own `/static` and nothing
  else, with no inline style and no script (`tests/test_settings_ui_style.py`).
  `/static` is served without a session, since the login page uses it too; it
  holds the stylesheet and two OFL typefaces and nothing secret, and paths
  outside it are refused (`::test_nothing_outside_the_static_directory_is_served`).
  Every response also sets `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: same-origin` (not `no-referrer`, under which a browser sends
  `Origin: null` and every form would be refused) and `no-store`, except `/static`, which a browser may keep a day (`server.SECURITY_HEADERS`, `STATIC_CACHE_CONTROL`).
- **What it can change, and how.** `SERVER_URL`, `CLIENT_TOKEN`, `PLAYER_NAME`
  and `TV_ADDRESS` (and the slideshow's keys), and only where the environment does
  not set them (`config.load_ui` › `pinned`); a pinned key is shown read-only and
  a POST cannot change it. An empty `TV_ADDRESS` in `.env` does not pin it
  (`config._beneath_the_environment`), because `.env.example` ships it empty. **The server's address and its token move together**: when
  the environment pins either, the page locks both, and a new address saved
  without a token clears the stored one, so the page can never send a token to
  a host it was not issued for (`server._locked`, `server._next_values`;
  `tests/test_settings_ui.py::TestSavingTheServer`, `::TestWhatTheEnvironmentPins`).
  Values are judged by the functions `config.load` uses (`server_url_problem`,
  which also refuses userinfo since the address is logged, `client_token_problem`,
  `player_name_problem`, `tv_address_problem`), and the save as a whole by
  `config.resolve`, which is `load` with the page's file swapped in, so the page
  cannot write a file the next start would refuse. A save restarts the process
  with SIGTERM to itself only in setup mode or when what the Player runs on
  changes (`server._commit`), and says which.
- **A new Frame address forgets the television's pairing, before anything is
  written.** The pairing token belongs to the set that issued it, so the page
  removes the token file (`server._forget_pairing`, which finds it by the same
  derivation the Frame uses, `config.tv_token_path`) and the set at the new
  address is asked to pair: a prompt somebody accepts at the set. A token file
  that will not go leaves the save unmade and says so
  (`settings_page.pairing_not_forgotten`), so the old token is never kept beside
  the new address. Changing `TV_ADDRESS` in `.env` by hand forgets nothing.
  What the new address does to the pictures on each television is in § What it
  does to the television, and the page says it before and after the save.
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
| Wall id from the document | `config.py:ClientSettings.wall` (`PLAIN_COMPONENT`) | Not one plain path component: `..`, `/`, a leading dot; or a name the cache directory reserves for its own files (derived from the constants that name them). The wall is not started. |
| Manifest (feed) | `manifest.py:parse`, `_feed`, `_work`, `_slot`, `_scene`, `_instant` | Wrong major, malformed (a work staged twice included), a rule of Arrt's `player-contract.md` § Rules a schema cannot state broken, an instant without an offset, a media hash not 64 lowercase hex, or a body past 16 MiB (`pull.py:MANIFEST_MAX_BYTES`). Last good feed kept. |
| Master bytes | `pull.py:Pull._cache_media`, `_stream_to` | SHA-256 does not match the feed, or the body runs past the bytes the feed declares for it, or the feed declares none. Discarded or not asked for; a master is never held whole in memory. |
| Client and label documents | `pull.py:DOCUMENT_MAX_BYTES` | Larger than 1 MiB. Refused whole. |
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
  selection, `programmes/schedule.py:Schedule.step` asks it before `show`, and
  the seam reads it again immediately before the send (`tv/client.py:TvClient._permit`).
  Held by `tests/test_frame_wall.py::test_a_television_somebody_is_watching_is_left_alone`
  and `::test_a_republished_schedule_is_not_put_on_a_television_somebody_is_watching`.
- **Shipped code presses no key.** No `send_key`, `KEY_`, `shortcuts`,
  `set_artmode` or wake-on-LAN in `src/` (searched 2026-10-27); the only
  remote-control channel the daemon opens is the library's own token check
  during construction. The read of `PowerState` is one REST GET
  (`tests/test_samsung_client.py::test_the_librarys_power_read_is_a_get_and_nothing_else`),
  and so is the read of the set's identity (`SamsungTv.read_identity`: one GET of
  the device description through its own short-lived client, no pairing and no
  art channel), which the wall sends on each new connection **whatever the set is
  showing, a programme included**, because it changes nothing on the set, and
  which the client sends once a minute for its heartbeat.
  No test asserts the absence of a key press in `src/`; the norm index records
  that the daemon's power guard arrives with Player power control.
  `tools/power_probe.py` presses power and refuses to without
  `--i-am-at-the-set` (`tests/test_power_probe.py`).
- **Every other write is behind the same read.** Uploading, deleting, setting
  brightness and switching off the set's own slideshow (once,
  `FrameDisplay._connected`) are sent only when `get_artmode` says on, or says off
  while `PowerState` says `standby`; they are held when the set is lit and not in
  art mode, and whenever `PowerState` cannot be read (`tv.client.WRITES`,
  `TvNotOurs`). Upload, delete and slideshow were measured invisible in the dark
  state; in the television state they were "not tried", so they are held there
  (`samsung-tv-state-findings.md` § What works in each state). Connecting and
  listing are reads and are not held. Whether a held write would have disturbed a
  programme is unmeasured, and the gate does not find out.
- **It removes from the set only what it recorded uploading.** Every upload is
  recorded on the device's store before it is sent, with the set it goes to and
  the `image_date` it carries (`tv_upload`; `DisplayState.record_sending`;
  `tv/client.py:upload_marker`), and `FrameDisplay._remove_orphans` removes, of
  what the set lists in `MY-C0002` (`tv/client.py:UPLOADED_CATEGORY`,
  "my-pictures"), only the pictures those records name there
  (`DisplayState.uploads_on`) that no binding in use wants or that are due to
  retire. Everything else the set lists stays: **the household's own pictures,
  those an older Player put up and never recorded, and any of this Player's own
  its records cannot match.** That holds for a television the Player meets with no
  record of it, which includes a different set at the Frame's address, and for a
  store that has just been deleted. The journal says once per set, in each
  process, how many it leaves (`tv.others_left_alone`), and the settings page
  says so before a new address is saved and after. A picture is the Player's on a
  set when a record of that set's holds its id with the date the listing shows
  equal to the marker the upload carried, or, for an upload whose id was never
  learned, when it is the one listed picture carrying that marker; or, for a
  record carried from a binding an older store wrote, which has no marker, when
  the listing shows its id (the date shown is then kept as its marker). A
  household picture the set stamps with the same second as an upload of the
  Player's on that set is the residue; so would be one under a carried record's
  id, were the set to give a removed picture's id to a new one before the first
  listing after the upgrade (not measured). **An upload whose record cannot be written is not
  sent** (`StoreFault`), because a picture with no record is one the Player could
  never take off. **The rule rests on the set echoing the marker**, measured once,
  on one upload (`platform-and-dependency-findings.md`); on a set that does not,
  the Player's records never match and nothing it uploads there is removed, which
  the journal says once per set in a process (`tv.markers_unconfirmed`). The
  owner ruled this on 2026-11-19 ("yes to recommendations") and accepted its costs
  on 2026-11-25: the pictures an older Player put up stay until a person deletes
  them on the set (no tool of this repository removes a picture it did not
  upload), and a rollback across it needs the
  owner's say-so each time, because the release before it sweeps by the old rule
  (`deploy/README.md`, the runbook's § Going back).
- **A binding is used only on the set it was made on.** A set's content ids are its
  own, so an id recorded on one set names some other picture on the next one:
  selecting it would show a stranger's picture, and removing it delete one. The
  store records the set's identity, `device.duid`, and the wall reads it from the
  connected set on every new connection (the wall's start, after every outage,
  after a failure that may have dropped the connection) before it selects,
  uploads or removes by any binding (`FrameDisplay._know_the_set`,
  `SamsungTv.read_identity`, `DisplayState.belongs_to`). On a different set the
  bindings are set aside under the old identity and the wall starts on the new
  one as on a first run; the first set's come back, with a fresh fourteen-day
  wait, if the wall returns to it (`data-model.md` § DisplayState).
- **A removal is sent only from a store whose recorded identity is the connected
  set's** (`FrameDisplay._may_remove`, checked in `_remove_orphans`, which every
  removal passes through, retirements included). A store that cannot say which
  set it was made on sends no removal: one written before schema 4 takes the
  first identity read as its own and removes nothing until then, and one the wall
  holds in memory while the set will not say has no recorded identity at all.
  The cost of the rule is that **the old set keeps the Player's pictures** until
  the Player is pointed at it again or somebody removes them there by hand; the
  journal (`binding.set_changed`) and the settings page say so.
- **While the set will not say which set it is, the wall waits, and after
  fifteen minutes goes on without it.** It uses no binding and says so once
  (`tv.unidentified`), asking again on its own ladder. After
  `displays/frame.py:IDENTITY_PATIENCE_SECONDS` (15 minutes) it shows art from an empty store
  held in memory, uploading the work it shows and then the feed's other works
  one a pass, and removing nothing (`tv.unidentified_going_on`). An outage drops
  that store and starts the fifteen minutes again, so a connection that flaps
  uploads nothing more. Those uploads are recorded on the device's store with no
  set, and once the set says, the wall returns to the device's store, with its own
  bindings, and the reconciliation that leaving the store in memory owes removes
  them from that set **only if it is one they were sent toward** (the set the
  store last recorded, listed at the address they went to) **or the one that said
  which it is on the connection they went on**, found by id and marker: **never
  the picture on the wall before another has been selected**, because what a
  Frame does when the image it shows is deleted has never been measured. The same
  holds for every removal of an image the Player knows is up. A record of no set
  is never matched on any other set, by id or by marker, whatever a picture there
  carries, because the address may reach another set before this connection is
  seen to have gone. **A failure that may have taken the connection without
  ending the pass** (an upload's read-back, an art-mode read) **starts a new
  spell** with the same store in memory, so a set heard from on the next
  connection is never taken as the one the earlier uploads went to.
  **Known limits, recorded and not fixed:** a set that opens its art channel and
  never gives its `device.duid` gains one theme of the store in memory's copies
  per outage and its fifteen minutes, and nothing removes them until a set they may
  be on says. Copies also stay for good when the set turns out to be a third one,
  when the store knew no set and the spell was lost, when a restart lands on
  another set, and when the set does not echo the marker; and a record of one set
  stays as a row after the Frame's address changes. What would retire the first is
  the measurement of whether any set does this (`samsung-tv-state-findings.md` § What is still owed, item 8); no
  cap is built for it. Held by `tests/test_bindings.py::TestABindingIsUsedOnlyOnTheSetItWasMadeOn`
  and `::TestASetThatWillNotSayWhichItIs`.
- **It stops on SIGTERM and closes the art channel** (`__main__.py:_run`,
  `client.py:Supervisor.stop_all`), because a killed client holds the set's slot
  for minutes. The unit sends SIGTERM and waits 90 s (`deploy/display.service`).

## Data Privacy

- **No personal data.** The Player stores artworks' ids, images and label text,
  its own settings, and per-set content ids and each set's `device.duid`, and a
  record of each upload it sent with the `image_date` it carried (`data-model.md`). A `duid` is a device's id, not the address or the MAC the
  same reply carries, neither of which the Player reads or records; the journal
  names a set by a short hash of it, never the id (`logs.py:known_as`).
- **What leaves the device in Arrt mode**, all to the server: the client heartbeat (outputs,
  their screen sizes, the Frame's `device.duid`, and the address of the Player's
  settings page, left out when the page is bound to loopback;
  `client.py:client_heartbeat`, `settings_url`, `FrameIdentity`) and each wall's heartbeat
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
- **The repository is public.** No addresses, hostnames or usernames in any file
  the repository can commit, documents, `deploy/` and `.prawduct/` included
  (`tests/test_repository.py::test_no_file_in_the_working_tree_carries_a_deployment_value`,
  `::test_no_tracked_file_carries_this_machines_deployment_values`, which match
  shapes, not values): private, CGNAT, link-local and unique-local addresses,
  home-directory paths, `.local` and `.lan` names beyond the three the product's
  own examples use, and `user@host` logins. A hit that is recorded rather than fixed
  is listed with its reason in the test's `_ACCEPTED_RESIDUE`.
- **Retention.** The journal is volatile and bounded
  (`deploy/journald.conf.d/10-bound-the-journal.conf`). The cache keeps the
  directory of a wall that left the Player for a week of the Player reading
  documents that do not place it, then removes it (`client.WallCaches`;
  `data-model.md` § The cache). A Frame store holds the bindings of every set the
  wall has been moved away from, for as long as the store lasts
  (`data-model.md` § DisplayState).

## Abuse Prevention

**The Player has one user and one inbound surface, the settings page.** What
a hostile or broken peer on the LAN can do, and what bounds it:

| Peer | Can | Bounded by |
|---|---|---|
| A server, or anything holding the client token | Choose what hangs on this client's walls; forge nothing on other clients | Arrt § Inventory; the Player shows only what the feed names |
| A server | Point a master at any URL the Player will GET | No token sent off-origin; bytes discarded unless the hash matches; GET only |
| A server | Send a very large master | Stopped at the bytes the feed declares for it, streamed to disk; 30 s without a byte ends it (`Pull._cache_media`, `_stream_to`, `_MEDIA_TIMEOUT`). The manifest itself is not capped |
| A server | Send a very large client or label document | Read up to 1 MiB (`pull.DOCUMENT_MAX_BYTES`) |
| A server | Name a wall id that is a path, or one a file of the cache is named | `PLAIN_COMPONENT`, and the reserved names (`WallIdUnusable`) |
| Anything on the LAN | Impersonate the television (no certificate check) and receive uploads | The LAN boundary; the uploads are public artworks |
| Anything on the LAN with the TV token | Drive the set | Arrt § Inventory ("LAN-scoped") |
| Anything on the LAN | Reach the settings page before its owner, and set its password | The setup code, printed at start and spent on use |
| Anything on the LAN | Guess the page's password | Length floor; a throttle per source address doubling to a minute per attempt, under a ceiling of 30 wrong in 10 minutes for all addresses that have not signed in |
| A page elsewhere in the owner's browser | Post a form to the settings page | `Origin` check on every POST; `SameSite=Strict` cookie |
| A page elsewhere in the owner's browser | Rebind its own name to the Player's address and use the page as same-origin | A `Host` that is not one of the page's names is refused with a 421 before any page (`server._known_hosts`) |
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
(un-prefixed `EnvironmentFile=`). It sets `UMask=0077`, so every
file the Player creates is `tvpi`'s alone.

- **Frozen.** `ExecStart` is `uv run --frozen --no-dev --group raster --group epaper`:
  a start takes the lockfile as it is and never resolves over the network, and
  the dev group stays off the Pi (`test_the_unit_starts_frozen_and_without_the_dev_tools`).
- **Bounded in memory.** `MemoryHigh=60%` and `MemoryMax=75%` of the Pi's RAM, so
  an exhausted Pi loses the Player and not sshd (`test_the_unit_bounds_its_memory_below_the_pis`).
- **Sandboxed as far as `tvpi` owning the checkout allows.** `NoNewPrivileges=yes`,
  `PrivateTmp=yes`, `ProtectSystem=full` (`/usr`, `/boot`, `/efi` and `/etc`
  read-only), `ProtectHome=yes` (`/home`, `/root` and `/run/user` empty to the
  Player) and `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`. The
  checkout, venv and `.env` are `tvpi`'s and `uv run` syncs the venv at start, so
  the code directory stays writable: **`ProtectSystem=strict` and root-owned code
  wait for the owner's ruling, which on 2026-11-19 was "not now".** Nothing sets `PrivateDevices=`, `DevicePolicy=`
  or `ProtectKernelModules=`, because the display card, the render node, SPI,
  GPIO and the DDC bus are reached through the groups above
  (`test_the_unit_is_sandboxed_as_far_as_tvpi_owning_the_checkout_allows`,
  `test_the_sandbox_leaves_the_devices_the_walls_draw_through`,
  `test_the_directives_the_sandbox_must_not_move_are_as_they_were`).
- **Told apart from a wedged Player, and restarted.** `Type=notify`: the Player
  sends `READY=1` once its mode is running and `WATCHDOG=1` only for progress (a
  finished supervisor cycle, a slideshow step, or in setup mode the page's loop
  turning), never for the loop merely being alive, and `WatchdogSec=300` ends one
  that stops (`notify.Watchdog`, `PettingSupervisor`; `test_the_unit_waits_for_the_players_word_and_ends_one_that_stops_making_progress`).
  **`NotifyAccess=all`, because the unit's main process is `uv`**, which starts the
  Player's Python as its child, so `main` would have systemd drop every message.
  The watchdog's signal is SIGTERM (`WatchdogSignal=SIGTERM`), the norm's stop, so
  a restart closes the art channel; **a Player whose event loop is itself stuck
  cannot run that handler and is SIGKILLed after `TimeoutAbortSec=90`**, the one
  way out of a loop that will not turn, the same exception the unit already
  accepts for a wedged panel driver. Setup mode's pet proves the loop turns, not
  that a request was answered, because probing the page would need an HTTP client
  beside the pull's and a false failure would restart the Player under someone
  mid-setup. A single stuck wall worker is not watched, by design.
- **Restricted further, with no change of path, device or ownership.**
  `LockPersonality`, `RestrictNamespaces`, `RestrictSUIDSGID`, `RestrictRealtime`,
  `ProtectHostname`, `ProtectKernelLogs`, `ProtectControlGroups`,
  `SystemCallArchitectures=native` and an empty `CapabilityBoundingSet`, each
  checked in the unit's comments against what the Player and mpv open
  (`test_the_unit_is_restricted_as_far_as_needs_no_path_device_or_update_step`).
  `ProtectClock=` is left out and forbidden by that test, because it implies a
  device allowlist that would hide the display card, SPI, GPIO and the DDC bus.
  None of it has run under systemd (the last bullet).
- **mpv starts with only `PATH`, `HOME`, `XDG_CACHE_HOME`, `LANG` and `LC_ALL`**
  (`mpv.MPV_ENVIRONMENT`), so no token, cookie or other `.env` value reaches the
  process that decodes files strangers posted. **mpv still runs as `tvpi`**, which
  can read `.env`, the settings file and the token file; the environment is not
  the only door. **That is the owner's accepted state** (2026-11-19, "not now":
  root-owned code and `ProtectSystem=strict` are "too heavy for right now", and
  the wave-1 sandbox "stands as it is"); closing the other doors waits on that
  ruling being reopened, and no unit directive added since changes it.
- **Not yet run under systemd.** Whether the hardened unit starts and reaches
  `active` in every mode, whether it still draws on the screen, the label and the
  DDC bus, whether mpv draws with the reduced environment, whether the kernel
  enforces the memory bound, and whether a stopped Player is restarted by the
  watchdog are the operator's checks on the Pi (the runbook's steps 9a, 13a–14c).

`tests/test_deploy_unit.py` also checks the environment file, the restart
policy, the absolute interpreter path, no home-directory path and the stop
timeout.

### Supply chain

- The TV client is a git fork pinned to a commit (`pyproject.toml`
  `samsungtvws @ git+…@fe95ef1d…`), unowned and dormant
  (`platform-and-dependency-findings.md`). Its TLS and token handling are as
  described above and not the Player's to change.
- The contract package is taken at a released tag (`[tool.uv.sources]`
  `arrt-contract`, `contract-v1.3.0`); `CLAUDE.md` forbids patching it.
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
