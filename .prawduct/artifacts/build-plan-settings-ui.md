---
artifact: build-plan
version: 2
scope: settings-ui
branch: feature/settings-ui
base: develop
working-branches: []
depends_on:
  - artifact: security-model
  - artifact: nonfunctional-requirements
  - artifact: architecture
  - artifact: project-preferences
governed_by:
  - artifact: project-preferences
    dispositions:
      - "The manifest and its media are the only channel from the server, and only pull.py opens an HTTP client → amendment proposed: a web SERVER is not a client. The guard is narrowed so that the one settings-UI module may import aiohttp.web, and only aiohttp.web, and may never name ClientSession. See the DECISION in Goals."
      - "No secret ever reaches a log line → exception: the one-time setup code is printed and logged at start. See the DECISION in Goals. The password, its hash, the session secret and CLIENT_TOKEN never are, and a test covers each."
      - "No hardcoded deployment values in source → conforms: the bind address and port come from .env with defaults, and the URL printed at start is derived at runtime."
      - "The Player never requires the server to be reachable → conforms: the UI reads only the cached client document."
      - "The television belongs to whoever is using it → conforms: the UI never edits TV_CLIENT_NAME, and touches nothing on the television."
      - "Catch specific exceptions; async at the I/O boundary, synchronous core; hardware and network behind an interface → conforms."
      - "The rest of the table (label legibility, heartbeat key, derived artifacts, labels as metadata) → inapplicable because this plan touches no label, heartbeat or composition code."
  - artifact: nonfunctional-requirements
    dispositions:
      - "§ Start-up (a missing SERVER_URL or CLIENT_TOKEN exits 2) → amendment proposed by the owner 2026-11-10: the Player starts the settings UI and prints where to go. A missing CACHE_DIR still exits 2, because that is where the settings are kept."
  - artifact: security-model
    dispositions:
      - "'The Player serves nothing' → amendment proposed: it serves one authenticated settings page. A new section records its boundary."
partition: serial — 02 builds its form on 01's store, server and login, and 03 documents and deploys both
last_validated: 2026-11-10
---

## Goals

**Response taken:** Asked in one batch. The owner answered on 2026-11-10: a login with a password set on first run; on startup the Player prints "go to … to configure"; changing the player name must never make the television re-pair; the user is technical (the owner, alone).

**Product:** A minimal settings page on the Player, so the server URL, the client token and a player name can be set without editing `.env` over ssh. It is also the base for wave 2, a server-less slideshow mode whose plan is drawn when that wave starts. Wave 2 reuses this plan's store and login, which is why the store is designed for it now.

**Architecture:**
- One `aiohttp.web` application on the existing event loop, in a new `arrt_player/settings_ui/` package, rendering HTML on the server. No JavaScript build and no new dependency: aiohttp is already one.
- The settings it writes are kept in `$CACHE_DIR/.settings.json` (mode 0600, written atomically). The leading dot keeps it apart from wall directories, the same way `.client.json` is kept apart (`config.CLIENT_DOCUMENT_FILENAME`).
- A key set in the environment, or in `.env`, wins. The page shows such a key as read-only and names where it comes from. *Inferred:* letting the environment win, rather than a precedence order between `.env` and the file, keeps one rule a reader can predict: to manage a key from the page, delete it from `.env`.
- Saving restarts the process: it sends SIGTERM to itself, and `Restart=always` brings it back. The existing shutdown path is the one the television's art-channel slot needs. Reloading settings live is out of scope.
- **The player name** (owner, 2026-11-10): a Player that has paired with a server uses the server's name for itself, taken from the cached client document (`.client.json`). One that has not uses `PLAYER_NAME`, set in `.env` or on the page. A server's name overrides the local one as soon as a client document is cached. The name is shown in the page title and the startup line. *Inferred:* it is derived on each read rather than copied into the store, so a later rename on the server needs no write here. `TV_CLIENT_NAME` is not on the page at all, because changing it costs a pairing prompt at the set (owner).

**Constraints:**
- **Setup mode** (owner): when `SERVER_URL` or `CLIENT_TOKEN` is missing, the Player serves only the settings page and prints `go to http://<address>:<port>/setup?code=<code> to configure`.
- **First run** (owner): the page asks for a password before anything else. The stored password is a scrypt hash (stdlib `hashlib`).
- **The settings file layout is persisted, so it is locked in.** It must answer these questions:
  - (a) which keys did the page set, and to what;
  - (b) is a password set, and is a given password correct;
  - (c) is this session cookie valid, using a secret that survives the restart a save causes;
  - (d) which version of the layout is this, so wave 2 can add a mode and its own keys without a migration.

  Shape: `{"version": 1, "values": {KEY: str}, "auth": {"password": "<scrypt params$salt$hash>", "session_secret": "<hex>"}}`.
- **`SERVER_URL` and `CLIENT_TOKEN` move together.** Changing `SERVER_URL` from the page clears `CLIENT_TOKEN` unless a new token is entered on the same form. A page that could point the Player at a new host while keeping the old token would hand that token to whoever runs the host (security-model.md § To the server).
- **The token can be written, never read back.** The form shows only whether a token is set.

**Decisions:**
- [DECISION: the plane-isolation guard is narrowed. One named module, `arrt_player/settings_ui/server.py`, may import `aiohttp.web` and nothing else from aiohttp, and may not reference `ClientSession`. The guard still fails any other module that imports aiohttp, and a planted violation shows it can still fail. | The norm exists to stop a quiet second channel to the server, which is outbound requests. A listening socket requests nothing. Allowing the whole aiohttp package would let a client in through the same import, which is why the carve-out names `aiohttp.web` and bans `ClientSession`. | owner can veto]
- [DECISION: the one-time setup code is printed and logged at start, an exception to "no secret ever reaches a log line". | The owner asked for the URL to be printed, and the URL has to carry the code: without it, anyone on the LAN who reached the page first would set the password. The code exists only while no password is set, is new on every start, and stops working as soon as a password is set. So a journal pasted afterwards gives away nothing still valid. The password, its hash, the session secret and the token stay out of every log, and tests cover each. | owner can veto]
- *Inferred:* the default port is 8771. It sits beside the server's 8770 and does not collide on a host running both. `UI_BIND` defaults to `0.0.0.0`, because the page is used from another machine. Both are set in `.env` only and are not on the page, since a page that can move its own port can lock itself out.
- *Inferred:* the cookie is HttpOnly, `SameSite=Strict` and signed with HMAC under the persisted secret, and lasts 30 days. A POST must carry an `Origin` header that matches the host. Failed logins are slowed with an exponential delay per process. This uses plain HTTP on the LAN, like the Arrt server's boundary (Arrt's `security-model.md` § Trust Boundary).
- *Inferred:* to recover a forgotten password, delete `auth.password` from the file by hand, which puts the Player back into the first-run flow with a new code. The deploy README documents this. No reset endpoint.

**Tradeoffs accepted:**
- Sessions are invalidated only by changing the password (a new session secret) or by expiry. That is acceptable for one owner.
- Plain HTTP means the password crosses the LAN in the clear. Revisit if the Player is ever reachable from outside the LAN.

**Decisions made mid-build:**
- Chunk 01: `_run` installs its signal handlers before resolving any config, so a SIGTERM during start-up is always handled. Settled by "shutdown is SIGTERM" (NFR).
- Chunk 01: `store.write` creates `CACHE_DIR`, because in setup mode it may be the first thing to touch it. Settled by setup mode itself.
- Chunk 01: `Referrer-Policy: same-origin`, not `no-referrer`, because under `no-referrer` a browser's form POST carries `Origin: null` and the origin check would refuse it. Settled by the origin check.
- Chunk 01: a login attempt is charged to the throttle before its password is checked, and scrypt runs on a thread. Settled by "the throttle bounds guessing" and "a wall never waits on its settings".
- Chunk 01: still owed is a check in a real browser, which the suite cannot make. It rides chunk 02's visual check.

**Level:** Medium-High. Requirements are settled and the mechanisms are all stdlib or aiohttp, which the repo already uses. To raise it: chunk 01's first test, aiohttp's test server against the setup flow, proves that the server, the store and the startup path connect.

## Scaffolding

### Dependencies

No new dependencies: `aiohttp.web` (already pinned at >=3.14), and stdlib `hashlib.scrypt`, `hmac`, `secrets`, `json` and `html`.

### Verification Strategy

- **Each chunk:** `uv run --group raster pytest`, ruff, black.
- **Beyond tests:** run `uv run python -m arrt_player` on the Mac with `CACHE_DIR` set to a scratch directory, and `SERVER_URL`, `CLIENT_TOKEN` and `TV_ADDRESS` unset. With no TV configured, nothing reaches the television. Follow the printed URL, set the password, log out, log in, save settings, and check that the process exits on SIGTERM.
- **Chunk 03:** the same flow on the Pi, under systemd.

## Build Chunks

### Chunk 01: Setup mode, the store and the login

- **Description:** The thin slice through every layer:
  - a settings file and its store;
  - `config.load` reading it beneath the environment;
  - a missing `SERVER_URL` or `CLIENT_TOKEN` starting setup mode rather than exiting 2;
  - the settings server, with first-run password, login and logout, and a placeholder settings page;
  - the printed "go to" line;
  - the plane-isolation amendment.
- **Depends on:** none
- **Artifacts consumed:** `security-model.md`, `nonfunctional-requirements.md` § Start-up, `project-preferences.md` § Enforcement
- **Deliverables:**
  - new `src/arrt_player/settings_ui/` (`store.py`, `auth.py`, `server.py`);
  - `config.load` reads the store, and raises a distinct `SetupRequired` when the server keys are missing;
  - `__main__` runs setup mode, and in normal mode starts the settings server beside the supervisor;
  - `UI_BIND` and `UI_PORT` in `config.py` and `.env.example`;
  - the guard amendment in `tests/test_plane_isolation.py`;
  - the NFR § Start-up amendment and a new security-model section on the settings page.
- **Tests:**
  - the store: round trip, mode 0600, atomic write, and refusal of an unknown version;
  - the environment beats the store;
  - setup mode on a missing server key, with exit 2 still on a missing `CACHE_DIR`;
  - the setup code: it works once, a wrong code is refused, and it dies once a password is set;
  - password hashing and verification;
  - a cookie signed under another secret is refused;
  - a POST with a foreign Origin is refused;
  - pages need a login;
  - the guard fails a planted `ClientSession` in the UI module, and a planted `import aiohttp` anywhere else;
  - no password, hash or secret appears in any log record from the flow.
- **Acceptance criteria:**
  - A Player with no `SERVER_URL` starts and prints the go-to line.
  - The page accepts the code, sets a password, logs in and out, and refuses without a login.
  - A configured Player behaves exactly as before, plus the page.
- **Done when:**
  1. Acceptance criteria met and the suite passes
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed

### Chunk 02: The settings form

- **Description:** The form for `SERVER_URL`, `CLIENT_TOKEN` (write-only) and `PLAYER_NAME`:
  - each key the environment pins is shown read-only with its source;
  - the server's name for this client is shown from the cached client document;
  - changing `SERVER_URL` clears the token unless a new one comes with it;
  - values are validated the way `config.load` validates them;
  - saving restarts the process with SIGTERM;
  - the password can be changed, which rotates the session secret.
- **Depends on:** Chunk 01
- **Artifacts consumed:** this plan's Constraints
- **Deliverables:** the form and save routes in `settings_ui/server.py`; `PLAYER_NAME` in `config.py`, the startup line and `.env.example`; the self-restart.
- **Tests:**
  - a key pinned by the environment cannot be changed from the page;
  - the token never appears in any response body;
  - a URL change without a token clears the token, and a URL change with one keeps the new token;
  - an invalid URL is refused with the reason;
  - a save signals the restart (the signal is passed in, so the test does not kill pytest);
  - changing the password invalidates old cookies.
- **Acceptance criteria:** the owner can set the server, the token and the name from a browser, and the Player comes back configured.
- **Visual change:** yes — the owner looks at the page once
- **Done when:**
  1. Acceptance criteria met and the suite passes
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed

### Chunk 03: Deploy and the record

- **Description:** The Pi path and the records:
  - `deploy/README.md`: the page, the port, and recovering a forgotten password;
  - `architecture.md`: a new task and channel;
  - `project-state.yaml`: the structural flags, since the Player now exposes a human interface on the web;
  - the change fragment;
  - checking that the unit's `Restart=always` brings a self-SIGTERM back.
- **Depends on:** Chunk 02
- **Artifacts consumed:** `deploy/README.md`, `architecture.md`
- **Deliverables:** the records above, current with the code, and the change fragment's narrative and consumer note.
- **Tests:** `tests/test_deploy_unit.py` still passes; any claim the README makes about the unit is checked there if it is checkable.
- **Acceptance criteria:** on the Pi, the page is reachable from the LAN, saving restarts the unit, and the walls come back.
- **Type:** cumulative-final
- **Done when:**
  1. Acceptance criteria met and the suite passes
  2. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved

## Early Feedback Milestone

**Milestone chunk:** 01
**What the user can do:** start a Player with no server configured, follow the printed URL, set a password and log in.

## Governance Checkpoints

**Commit & PR cadence:** commit per chunk after its review. The cumulative review after chunk 03 makes the branch ready for a PR, which is created when the owner asks.

- After chunk 01: confirm the shape (the store beneath the environment, setup mode, the guard amendment) before the form widens it.
- Wave 2 (the slideshow, served without an Arrt server) gets its own plan and branch once this one merges. A backlog item records it. Owner, 2026-11-10:
  - you choose which output or outputs the slideshow shows on;
  - Reddit is reached with a session cookie, which you copy from a browser tab using instructions shown on the page;
  - sources sit behind an interface, so another source can replace Reddit.
