---
artifact: build-plan
version: 2
scope: audit-wave-1
branch: fix/audit-wave-1
base: develop
working-branches: []
depends_on:
  - artifact: architecture
  - artifact: security-model
  - artifact: nonfunctional-requirements
  - artifact: observability-strategy
  - artifact: samsung-tv-state-findings
  - artifact: hdmi-output-findings
  - artifact: project-preferences
governed_by:
  - artifact: project-preferences
    dispositions:
      - "The television belongs to whoever is using it (Arrt's NFR) → conforms, and is strengthened: Chunk 02 moves the art-mode read to one choke point in the TV seam, immediately before every verb that writes to the set, and holds the writes the measurements do not show to be safe. The norm's index row gains Chunk 02's tests (coordinator's edit)."
      - "The manifest and its media are the only channel from the server; only pull.py opens an HTTP client → conforms: no chunk adds a client. Chunk 07's Host allowlist is in settings_ui/server.py, which listens and never requests. Chunk 10's label cache is written by pull.py."
      - "The Player never requires the server to be reachable → conforms, and Chunk 10 extends it to the label (S4). Chunk 03's bounded feed hold keeps the wall's programming moving while one master fails."
      - "No secret ever reaches a log line → conforms, and Chunk 02 closes the websockets DEBUG path and the TV exception text sent in the heartbeat (audit-8 F15)."
      - "Everything the Player writes is its own account's alone → conforms: new files (the label cache) are written under UMask=0077 by the same atomic writer; Chunk 08 keeps UMask."
      - "The Player is stopped with SIGTERM, never SIGKILL → conforms: Chunk 08 must keep KillSignal and TimeoutStopSec, and tests/test_deploy_unit.py holds them."
      - "A change to the contract is a PR and tag on Arrt → conforms: wave 1 changes nothing in arrt-contract and keeps the contract-v1.0.0 pin; every contract need is filed against Arrt (ledger)."
      - "Hardware and network access sit behind an interface → conforms: Chunk 05 adds one verb to ScreenOutput (blank), doubled in tests/fakes.py."
      - "Async at the I/O boundary, synchronous core → conforms."
      - "No hardcoded deployment values in source → conforms, and Chunk 09 widens the guard (audit-8 F19)."
      - "Derived artifacts are regenerated, never transported → conforms: Chunk 04 makes a damaged composed picture regenerate."
      - "The heartbeat's instant under the contract's key → conforms: Chunk 03's ServerDouble validates the wall heartbeat (#13)."
      - "Every log line carries its event as a field; lint waivers say why; catch specific exceptions; tests live in tests/ → conforms: each new log line, waiver and catch follows the row."
      - "A display device renders its own label → conforms."
      - "The e-paper label is legible at standing distance → inapplicable because no chunk changes layout, type or the panel mode."
      - "A found problem is fixed, not filed → conforms: every audit finding not fixed here is in the ledger with the test it met (needs a ruling, a contract change, a measurement, or is medium+ and orthogonal)."
      - "Delegation / Delegate verification → unset; the methodology default holds: a delegate runs only the narrowest tests covering its own change."
partition: "01 serial first (coordinator, splits tests/fakes.py so 02 and 05 own disjoint doubles); then 02-09 fanned out to eight delegates in isolated worktrees, each owning disjoint files; then 10 and 11 serial on the integrated branch, because 10 writes files 03 and 06 own and 11 sweeps every file"
last_validated: 2026-11-19
---

# Build plan: the Player's share of the 2026-11-19 audit, wave 1

## Goals

**Response taken:** proceed. The owner asked for substantially all identified
product issues to be fixed by delegated builders under governance (2026-11-19, as
relayed by the coordinating session). This plan is wave 1: every Player fix that
needs no owner ruling, no change to `arrt-contract`, and no other chunk's output.
Everything else is in the ledger and the wave-2 outline (gitignored, beside the
audit reports in Arrt's `.ux-walk/audit-2026-11-19/`).

**Product:** the Player keeps each wall right and the household's television its
own. The findings this wave closes are of four kinds: the television touched while
somebody watches it (C1, S1, S7); a wall that stops following its programming
after a damaged file, a late connector or one bad master (C2, S2, S3, S6); a
screen or label that says the wrong thing after an unassignment or a restart (S4,
S5, M3, M6); and the settings page, the unit and CI (audit-8 F1, F2 part, F13,
F14, F15, F19, F22; audit-7 M4, M5).

**Architecture:** what must hold.
- The art-mode read stays fresh, never cached from announcements, and now sits in
  the seam itself, so a verb added later inherits it.
- A wall renders only from its cache. The pull stays the one HTTP client.
- The contract pin stays `contract-v1.0.0`.
- Each wall's fault stays its own.

**Constraints:** the delegates run nothing under `tools/` (those drive real
hardware), nothing that reaches a server or a device, and never the whole suite.
The integrated branch's suite run and every Critic review are the coordinator's.

**Decisions** (each vetoable, each stated where its chunk needs it):
- [DECISION: tests/fakes.py is split before fan-out (Chunk 01): FakeTv moves to new tests/fake_tv.py, re-exported from tests/fakes.py, so Chunk 02 owns the television double and Chunk 05 owns the screen double | Chunks 02 and 05 both have to change a double in that one file (S7 and S5), and the partition gives a file one owner; the move changes no behaviour and keeps all 18 importers unchanged | user can veto/override]
- [DECISION: the verbs that write to the set (upload, delete, brightness, the native slideshow switch) are sent only when get_artmode says on, or when it says off and PowerState says standby (the dark state); they are held when the set is lit and not in art mode, or when power cannot be read. select_image needs art mode on, as today | `samsung-tv-state-findings.md` § What works in each state measures all of them working in the dark state and art mode, and records upload, delete and slideshow as "not tried" in the Television state, where brightness was only read. Holding them there costs uploads during an evening of television, which the wall catches up on in art mode | user can veto/override]
- [DECISION: a feed whose masters fail transiently is held for at most 10 minutes, then adopted with the failing works skipped, as a 404 is today. The failing masters go on being retried on later polls until they arrive. One route is to write no ETag while any master is missing, so the next poll is a 200 and retries them | today one master that will not download freezes the wall's schedule, scenes and staging indefinitely (S3). The hold exists so a feed is never adopted half-fetched by accident; a bound keeps that for transient blips and ends the freeze. This changes what `architecture.md` and `nonfunctional-requirements.md` say the pull never does, and Chunk 11 rewrites them | user can veto/override]
- [DECISION: a master's download is streamed to a temporary file under a running hash and stopped once it passes the byte count the manifest declares for it. A master stopped that way is skipped like a hash mismatch. The client document and the label document are read with a 1 MiB cap | #7 and audit-8 F13: a master is read whole into memory. The manifest already states each master's `bytes`, so the cap needs no invented constant | user can veto/override]
- [DECISION: an HDMI wall that is unassigned or moved goes dark by drawing a black frame through its own connector, and a Player that is merely stopping leaves its screens as they are | S5. A black frame is what the console shows before a first draw, which the screen already reports as `dark`; disabling the CRTC would hand the connector back to the text console. Shutdown mirrors the labels, which are "stopped, never retired" (`client.Supervisor.stop_all`) | user can veto/override]
- [DECISION: the settings page's setup link stays in the journal, and the record that says otherwise is corrected (Chunk 11) | audit-8 F15. The link is the only way a headless Pi's operator can find the code. It is valid only until a password is set and changes on every start, and the journal is readable only by the groups that already administer the Pi | user can veto/override]
- [DECISION: the login throttle is kept per source address, under a global ceiling well above one person's typing | audit-8 F1: with one global throttle, an attacker locks the owner out. Once the Host allowlist closes DNS rebinding, a guesser must be on the LAN with an address of its own, and the owner's address is never the one being slowed | user can veto/override]
- [DECISION: Player CI stays on GitHub's runners in this wave; it pins uv, Python and every action by commit. Moving it to the testament pool waits for the owner (wave-2 question 4) | the Player's CI is about 70 s; the pool would need a TESTAMENT_KEY secret in a second public repo, which audit-8 F6 names as the risk | user can veto/override]
- [DECISION: the shared `.prawduct/artifacts/` records are rewritten once, in Chunk 11, from each chunk's "records owed" list, not by each chunk | Chunks 02, 03, 05, 07 and 08 each falsify a sentence in the same few artifacts (architecture, NFR, security-model, observability-strategy), and a file gets one owner | user can veto/override]
- [DECISION: the "plane" wording sweep (#14) runs here, as the last serial chunk, rather than in wave 2 | it touches about 57 sites across src/, which collide with every other chunk, but it needs no ruling, and running it after the merges costs one cheap serial chunk instead of a second wave of the same files | user can veto/override]

**Tradeoffs accepted:**
- Uploads wait while somebody watches television (the S1 decision).
- The unit is hardened only as far as the operator's procedure allows unchanged.
  Root-owned code and `ProtectSystem=strict` wait for the owner (wave-2 question 1).
- Chunk 08's unit can be checked here only by reading it. Whether it runs on the Pi
  is the operator's check at the next deploy, written into the runbook.

**Decisions made mid-build:** *(appended while building)*

**Level:** Medium-High. Every chunk is backed by a red test that can be built
against the doubles today. What stays unmeasured: whether the held verbs actually
disturb a set showing a programme, whether the hardened unit starts on the Pi, and
ext4's behaviour after power loss. None of these can be measured without the owner at the device, and each is a ledger row.
What would raise the level: the owner's hand test at the set (S1), and the
operator running Chunk 08's runbook check on the Pi.

## Verification strategy

Each chunk proves itself with a test that is red on `develop` at c3cdab2 and
green after. Its report includes the red run, either as the failing output or as
the re-break that turns the test red again. Learnings: "When you write a check,
prove it can fail". Each delegate's ceiling is the run below, over the files its
chunk names and nothing wider:

    testament run --json -- <its test paths>        # else: uv run --group raster pytest <its test paths>
    uv run ruff check <its paths> && uv run black --check <its paths>

The coordinator runs the whole suite (`testament run --json`) once on the
integrated branch, after Chunk 09 merges and again after Chunk 11.

**Every report returns:** the red-then-green evidence; the records owed (each
sentence in a `.prawduct/artifacts/` file or `deploy/README.md` that the change
falsifies, with its replacement); and any file outside the chunk's boundary that
it needed and did not touch.

**Files no chunk edits:** `tests/conftest.py`, `tests/test_majors.py`,
`tests/test_player_contract.py` (Chunk 03 may, see there), `tests/test_availability.py`,
`tests/test_wall.py`, `src/arrt_player/wall.py`, `src/arrt_player/manifest.py`,
and everything under `tools/`. A chunk that cannot pass without one of them
stops and reports.

## Build Chunks

### Chunk 01: PL-0, the television double gets a file of its own

- **Description:** move `FakeTv` and `FOREIGN_IMAGE` out of `tests/fakes.py` into new `tests/fake_tv.py`, re-exported from `tests/fakes.py` so that no importer changes.
- **Type:** trivial
- **Trivial because:** moves one class and one constant between test modules and re-exports them; no test, assertion or behaviour changes.
- **Depends on:** none. Done by the coordinator, or one delegate, before the fan-out; Chunks 02 and 05 branch from it.
- **Owns:** `tests/fakes.py`, new `tests/fake_tv.py`.
- **Acceptance:** the collected test count is unchanged, and `grep -n "class FakeTv" tests/` names only `tests/fake_tv.py`. Verification: `tests/test_frame_wall.py`, `tests/test_client.py`, `tests/test_bindings.py`, `tests/test_samsung_client.py`, `tests/test_selection_observers.py`, plus ruff and black.
- **Model:** Sonnet-class, low effort.

### Chunk 02: PL-1, the television seam holds the household's rule

- **Description:** closes audit-3 C1, S1 and S7, and audit-8 F15 (the TV parts).
  - **C1:** the art-mode read sits at one choke point in the TV seam, immediately before every verb that writes to the set. An upload of up to 60 s, or a rebind, can no longer be followed by a `select_image` nobody re-checked.
  - **S1:** the non-select writes are gated by the S1 decision in Goals.
  - **S7:** the double gains time and state.
  - **F15:** the `websockets` logger is floored, and the television's exception text is scrubbed of its token before it reaches `last_error` and so the heartbeat.
- **Design:**
  - The choke point lives in `src/arrt_player/tv/`, and nothing outside that package can reach a write verb past it.
  - One shape that meets this: `TvClient` declares its writes as concrete template methods that read the gate, then call an abstract `_send_*` that `SamsungTv` and `FakeTv` implement.
  - Every write verb is declared in one symbol, so a test can range over it.
  - A refused write raises a new exception exported from `src/arrt_player/tv/__init__.py`.
  - `FrameDisplay` answers that exception per verb:
    - a selection is held off, as `is_ours` holds today;
    - reconciliation stays owed;
    - brightness is retried at the next interval;
    - the slideshow switch stays unmarked;
    - an upload is not recorded as failed.
  - `is_ours` may stay as the cheap early exit.
  - The builder chooses the shape and records the choice under Decisions made mid-build.
- **Depends on:** Chunk 01.
- **Critic mode:** final
- **Owns:** `src/arrt_player/displays/frame.py`, `src/arrt_player/tv/__init__.py`, `src/arrt_player/tv/client.py`, `src/arrt_player/tv/samsung.py`, `src/arrt_player/logs.py`, new `tests/fake_tv.py` (created by Chunk 01), `tests/test_frame_wall.py`, `tests/test_samsung_client.py`, `tests/test_selection_observers.py`, `tests/test_logs.py`.
- **Must not touch:** `src/arrt_player/programmes/schedule.py`, `src/arrt_player/__main__.py`, `tests/fakes.py`. FakeTv's new behaviour is armed, never a changed default, except that `show` with art mode off now takes the screen (art mode turns on and `took_the_screen` is counted), which is what the set does. Every other test file that imports FakeTv must pass unedited.
- **Tests (red on today's code, green after):**
  1. FakeTv armed to turn art mode off during `upload`. One tick of a fresh wall gives `selected == []` (the audit's probe gives `['MY-F0001']`).
  2. The same, on the rebind path: the set refuses a selection, the work is re-uploaded, and art mode goes off during the re-upload. No second `select_image` is sent.
  3. Art mode off with power on, over 3 ticks with a pending upload, owed reconciliation, a due brightness change and the slideshow switch unmarked: 0 uploads, 0 removals, `brightness == []`, `slideshow_disabled == 0` (today: 3 uploads, 1 brightness write).
  4. The same with power `standby`: the uploads and the brightness write go ahead.
  5. Parametrised over the seam's declared write verbs: each, called with art mode off and power on, raises and leaves the double unchanged. A second test fails when an abstract method of `TvClient` is classified as neither a read nor a write. Learnings: "parametrise the test over that symbol".
  6. FakeTv itself: `show` with art mode off takes the screen.
  7. `logs.configure(logging.DEBUG)` leaves the `websockets` logger at WARNING or above.
  8. A library failure whose text carries `token=<secret>` produces a `TvUnavailable` without the secret in it.
- **Acceptance:** tests 1, 2, 3, 5 and 8 are shown red on c3cdab2, and the household tests already in `tests/test_frame_wall.py` stay green. The report names the new test ids, which go into the norm index row (lines for the coordinator), and the records owed: Player `security-model.md` "Other verbs are not behind the art-mode gate", and the architecture failure table's "Television in use" row.
- **Verification ceiling:** `tests/test_frame_wall.py tests/test_samsung_client.py tests/test_selection_observers.py tests/test_logs.py tests/test_bindings.py tests/test_client.py tests/test_majors.py tests/test_availability.py tests/test_player_contract.py tests/test_display_state.py`, plus ruff and black on the owned files.
- **Model:** Opus-class, high effort. This is the product's highest-ranked household rule, and the shape of the seam is a judgment call.

### Chunk 03: PL-2a, the pull: ETags, the feed hold, heartbeats and big masters

- **Description:** closes audit-3 C2, S3, M1, and M8's #6, #7 and #13; and audit-8 F13 (the Player's reads).
  - **C2:** `Pull._cached_etag` and `ClientPull._cached_etag` send `If-None-Match` only when the cached document parses with the reader's own parser: the watcher's for the manifest, `parse_client_document` for the client document.
  - **S3, heartbeat half:** the wall heartbeat is forwarded every poll interval while the server answers, whatever the manifest backoff is doing.
  - **S3, hold half:** the bounded hold in Goals.
  - **M1:** a 4xx refusal of a heartbeat marks that report as seen, so it is posted again only when it changes, as the log line already says. A 5xx or a transport failure is retried.
  - **#6:** `label_id` is checked as a plain path component before it goes into a URL.
  - **#7 and F13:** masters are streamed and capped (Goals), and the client and label reads are capped.
  - **#13:** `ServerDouble` validates the wall heartbeat against the contract's schema through `arrt_contract`.
- **Depends on:** none.
- **Owns:** `src/arrt_player/pull.py`, `src/arrt_player/heartbeat.py` (no change expected), `tests/test_pull.py`, `tests/server_double.py`, `tests/test_heartbeat.py`, `tests/test_heartbeat_wiring.py`, and `tests/test_player_contract.py` only where #13's validation makes one of its posts invalid.
- **Must not touch:** `src/arrt_player/client.py` (`parse_client_document` is imported, not changed), `src/arrt_player/label_renderer.py`, `tests/test_client.py`.
- **Tests (red today, green after):**
  1. C2, manifest: a cached `manifest.json` truncated to half beside its ETag. A restarted `Pull.cycle` adopts the server's feed (today the file stays at half and is not adopted).
  2. C2, client document: an unreadable `.client.json` beside its ETag. `ClientPull.fetch` returns the server's document (today None, because the server answers 304).
  3. S3: one master answers 500. The heartbeat changes and reaches the double within one poll interval, not after the backoff.
  4. S3: one master keeps failing past the hold. The feed is adopted, the other works and the scene are shown, and the failing master is fetched once it answers again.
  5. M1: the double answers 400 to a heartbeat. Two cycles over an unchanged file post once (today twice).
  6. #6: a label id of "../x" is refused by name, and no request is made.
  7. #7: a master whose body runs past its declared `bytes` is stopped and skipped, and no file of that size is written.
  8. #13: an invalid wall heartbeat posted to the double is refused.
- **Acceptance:** each red run is shown. The report lists the records owed: architecture § What each component must never do, its failure table rows for transient masters and the heartbeat, NFR § When the server is down, and the observability events added (`pull.media_failing` text).
- **Verification ceiling:** `tests/test_pull.py tests/test_heartbeat.py tests/test_heartbeat_wiring.py tests/test_player_contract.py tests/test_client.py tests/test_majors.py tests/test_plane_isolation.py tests/test_availability.py`, plus ruff and black.
- **Model:** Opus-class, high effort. The hold and the cadence are design work against an invariant.

### Chunk 04: PL-2b, composed pictures survive power loss and transient reads

- **Description:** closes audit-3 S6 and M7.
  - **S6, durability:** `compose.compose` fsyncs the staged file before the rename.
  - **S6, repair:** the schedule treats a composed picture that is empty or not a readable JPEG as owed. It removes the picture and composes again. The check runs at most once per path per process (or per file stamp), never on every pass.
  - **M7:** an `OSError` that carries an errno while the master is being read is retryable after `COMPOSE_RETRY_SECONDS`. A master that decodes badly stays `Uncomposable` until the feed changes.
- **Depends on:** none.
- **Owns:** `src/arrt_player/compose.py`, `src/arrt_player/programmes/schedule.py`, `tests/test_compose.py`, `tests/test_schedule.py`.
- **Tests (red today, green after):**
  1. A zero-length file at the work's `composed_path`. Stepping the schedule composes it again, and the result is non-empty and decodes (today it is never recomposed).
  2. The staged file is fsynced before it is renamed. Record the order of `os.fsync` and `Path.replace` on that path.
  3. A composer whose master read raises `OSError(errno.EIO)` once: the work is composed after the retry wait (today it is `Uncomposable` for the feed).
  4. A master that is not an image stays refused after the wait.
- **Verification ceiling:** `tests/test_compose.py tests/test_schedule.py tests/test_wall.py tests/test_frame_wall.py tests/test_screen.py`, plus ruff and black.
- **Model:** Sonnet-class, high effort.

### Chunk 05: PL-3a, outputs follow connectors, and a retired screen goes dark

- **Description:** closes audit-3 S2 and S5.
  - **S2:** the supervisor keeps the last client document, and reconciles against it whenever the outputs it lists have changed since the last reconcile, not only when the server sends a new document.
  - **S5:** a wall worker learns whether it was stopped because its wall was retired (unassigned, or moved off this output) or because the Player is stopping. On retirement an HDMI wall blanks its output, by the S5 decision in Goals.
  - **Shape:** `ScreenOutput` gains `blank()`. `KmsOutput.blank` presents a black frame through the existing `present`. `RecordingOutput` in `tests/fakes.py` records it.
  - The worker signature stays callable as it is today: an optional or defaulted parameter, or a stop event that carries the reason. Tests this chunk does not own then pass unedited.
- **Depends on:** Chunk 01.
- **Owns:** `src/arrt_player/client.py`, `src/arrt_player/displays/screen.py`, `src/arrt_player/kms.py`, `src/arrt_player/__main__.py`, `tests/fakes.py`, `tests/test_client.py`, `tests/test_screen.py`, `tests/test_kms.py`, `tests/test_main.py`.
- **Must not touch:** `src/arrt_player/displays/frame.py`, `src/arrt_player/wall.py`, and the new tests/fake_tv.py.
- **Tests (red today, green after):**
  1. S2: reconcile a document with no outputs. Then let the outputs list `hdmi-a-1` while the link answers 304 for 3 cycles: the wall is running (today `running == {}`).
  2. S5: unassign a running screen wall. The output records one `blank` (today none).
  3. S5: move the wall to another output. The old output is blanked.
  4. S5: `stop_all` blanks nothing.
  5. `KmsOutput.blank` presents an all-black frame of the connector's mode, through a `Device` double.
- **Acceptance:** each red run is shown. The report lists the records owed: the architecture failure table (an output that appears; unassignment) and `hdmi-output-findings.md` if it describes what a stopped wall shows.
- **Verification ceiling:** `tests/test_client.py tests/test_screen.py tests/test_kms.py tests/test_main.py tests/test_pull.py tests/test_player_contract.py tests/test_screen_brightness.py tests/test_majors.py tests/test_slideshow.py`, plus ruff and black.
- **Model:** Opus-class, high effort.

### Chunk 06: PL-3b, the label's hold keeps one clock, and shutdown waits for a draw

- **Description:** closes the second half of audit-3 M3, and M6.
  - **M3:** the offline hold measures its 30 minutes on a monotonic clock. `LabelRenderer` takes an optional `monotonic` callable defaulting to `time.monotonic`, so `__main__` does not change. The first half of M3, the server's `since` against the Pi's clock, is the contract's accepted behaviour and stays.
  - **M6:** `LabelPanel.close` does not power the panel down under a draw still in flight. It waits for the draw, bounded by the label budget. Past the budget it logs once and leaves the panel. A thread parked in C cannot be stopped, and the unit's stop window already covers that case.
- **Depends on:** none.
- **Owns:** `src/arrt_player/label_renderer.py`, `src/arrt_player/label_rule.py` (no change expected), `tests/test_label_renderer.py`, `tests/test_label_rule.py`.
- **Must not touch:** `tests/fakes.py`. FakeSurface already has `draw_takes_seconds`, `blocks`, `entered` and `left`; a missing arming is a subclass in this chunk's test file. Also `src/arrt_player/pull.py` and `src/arrt_player/__main__.py`.
- **Tests (red today, green after):**
  1. The server goes unreachable. The wall clock jumps 31 minutes ahead while 1 monotonic minute passes, and the caption is still held (today it blanks).
  2. 31 monotonic minutes pass while the wall clock steps back, and the panel blanks.
  3. A draw is in flight past the budget when `close()` runs. The surface's `close` is not called while the draw is between `entered` and `left` (today it is).
- **Verification ceiling:** `tests/test_label_renderer.py tests/test_label_rule.py tests/test_client.py`, plus ruff and black.
- **Model:** Sonnet-class, high effort.

### Chunk 07: PL-4a, the settings page answers only to its own names, and speaks the server's words

- **Description:** closes audit-8 F1; audit-3 M2 and M8's #8; and audit-5 F16 and the Player's half of F6 (the field labels and the word "client").
  - **F1, Host:** a Host allowlist, as Arrt's arrt/src/arrt/http/hosts.py has. Allowed: an IP literal, `localhost`, this host's own name and `<name>.local`, and the names in an optional `.env` key (`UI_ALLOWED_HOSTS`). Anything else is refused before any page. The Origin check accepts a listed name or the request's own address.
  - **F1, login:** the throttle is per source address (Goals).
  - **M2:** every interval and timeout read through `_float` is refused by name at zero, below zero, NaN or infinity: `MANIFEST_POLL_SECONDS`, every `TV_*_SECONDS`, `BRIGHTNESS_INTERVAL_SECONDS`, and any other `*_SECONDS` key. `LATITUDE` and `LONGITUDE` must be finite and in range.
  - **#8:** a wall id equal to any name the cache directory reserves (`token_file`, the client document, the settings file and the rest) raises `WallIdUnusable`. The reserved set is derived from the constants that name those files, not copied.
  - **F16:** screens show *HDMI 1* for `hdmi-a-1`, by the server's rule (Arrt's static core/outputs.js, `screenName`), and *Samsung Frame* for the Frame.
  - **F6:** the fields read *Server address* and *Token*, and no visible string on the page says "client". Hints may still name `SERVER_URL` and `CLIENT_TOKEN` for `.env`.
- **Foreign API:** aiohttp `Request.host` and `Request.remote`.
- **Depends on:** none.
- **Owns:** `src/arrt_player/settings_ui/server.py`, `src/arrt_player/settings_ui/auth.py`, `src/arrt_player/settings_ui/store.py`, `src/arrt_player/config.py`, `.env.example`, `tests/test_settings_ui.py`, `tests/test_settings_ui_style.py`, `tests/test_config.py`.
- **Done when:**
  0. verify-api: read aiohttp's `web_request.py` for what `host` and `remote` return behind no proxy, and record it in the chunk report.
  1. The tests below are red on c3cdab2 and green.
- **Tests:**
  1. A GET with `Host: rebound.example` is refused (today 200).
  2. A POST whose Origin and Host both name an unlisted name is refused (today it passes).
  3. Wrong passwords from one address do not delay a correct one from another.
  4. Parametrised over the interval keys, each refused at `0`, `-1`, `nan` and `inf`.
  5. Parametrised over the reserved names, each refused as a wall id.
  6. The page says "HDMI 1" and never `hdmi-a-1`.
  7. The page says "Token", and no visible text says "client".
- **Records owed:** Player `security-model.md` (the page's boundary), `observability-strategy.md` (the setup link goes to the journal: Goals), and `.env.example`'s new key.
- **Verification ceiling:** `tests/test_settings_ui.py tests/test_settings_ui_style.py tests/test_config.py tests/test_main.py tests/test_client.py tests/test_plane_isolation.py`, plus ruff and black.
- **Model:** Opus-class, high effort.

### Chunk 08: PL-4b, the unit: frozen, bounded, partly sandboxed, and the way back told truly

- **Description:** closes audit-8 F14 and F13 (the unit's half), the parts of F2 that leave the operator's procedure unchanged, and F22 (the Pi's uv calls); and audit-3 M5.
  - **F14:** `ExecStart` runs uv frozen and without the dev group, keeping the optional groups the Pi needs (`epaper`, `raster`).
  - **F13:** `MemoryMax=` as a percentage of the Pi's RAM, with `MemoryHigh=` below it.
  - **F2, in this wave:** `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=full`, `ProtectHome=yes`, and `RestrictAddressFamilies=` limited to what the Player and mpv use.
  - **F2, mpv:** mpv is started with a cleaned environment: an allowlist of the variables it needs, so no token or cookie reaches it.
  - **F22:** every `uv sync` in `deploy/README.md` and the runbook is frozen.
  - **M5:** the runbook's "Nothing on the Pi migrates" is replaced with what migrates (the settings file's layout v2, which an older Player refuses with exit 2; the Frame store's forward migration), and "Going back" carries the steps that `deploy/README.md` holds.
  - The runbook gains the operator's check that the hardened unit starts and draws.
- **Foreign API:** systemd unit directives; the uv CLI.
- **Depends on:** none.
- **Owns:** `deploy/display.service`, `deploy/README.md`, `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`, `src/arrt_player/mpv.py`, `tests/test_deploy_unit.py`, `tests/test_mpv.py`.
- **Must not touch:** `KillSignal`, `TimeoutStopSec`, `UMask`, `SupplementaryGroups`, or `StartLimitIntervalSec`, beyond keeping them. The devices behind those groups stay reachable: nothing here sets `PrivateDevices`, `DevicePolicy` or `ProtectKernelModules`.
- **Done when:**
  0. verify-api: read `man systemd.exec` and `man systemd.resource-control` for each directive added, and `uv run --help` for the flags. Quote the lines relied on in the report.
  1. The tests below are red on c3cdab2 and green.
- **Tests:**
  1. `tests/test_deploy_unit.py` asserts:
     - `ExecStart` carries the frozen flag and excludes dev;
     - `MemoryMax` is set;
     - `NoNewPrivileges=yes`;
     - the kept directives are unchanged.
  2. mpv's `Popen` is given an environment without a sentinel secret planted in the Player's (today it inherits everything).
- **Records owed:** Player `security-model.md` § The deploy unit, and the architecture failure table if an OOM kill becomes a named row.
- **Verification ceiling:** `tests/test_deploy_unit.py tests/test_mpv.py tests/test_slideshow.py`, plus ruff and black.
- **Model:** Opus-class, high effort. A wrong directive dims a wall only at the next deploy.

### Chunk 09: PL-4c, CI is pinned, and the hygiene guard sees the shapes that leaked

- **Description:** closes audit-7 M4 and M5 (the Player's workflow) and audit-8 F22 (CI uv calls and the Waveshare pin), and F19 (the Player's guard).
  - **Pins:** `.github/workflows/suites.yml` pins `setup-uv`'s uv `version:` to the server's 0.11.32 and the Python it tests on, set in the workflow, not by a `.python-version` file, which would also move the Pi's interpreter. The Python comes from `platform-and-dependency-findings.md`'s record of the Pi.
  - **Actions:** every `uses:` is pinned to a commit SHA, with its tag in a comment.
  - **uv in CI:** every uv call in CI is `--locked`.
  - **Waveshare:** `pyproject.toml` pins `waveshare-epd` by an override at the commit the lock already records (`2587516a…`), and the re-locked `uv.lock` changes no resolved version.
  - **F19:** `tests/test_repository.py` matches CGNAT 100.64/10, IPv6 ULA and link-local, 169.254/16, `.local` and `.lan` host names, and `user@host`. It scans tracked documents, `deploy/` and `.prawduct/` as well as source. Any real hit is fixed in this chunk, or, where it is in a file another chunk owns, reported.
- **Foreign API:** GitHub Actions refs.
- **Depends on:** none.
- **Owns:** `.github/workflows/suites.yml`, `.github/scripts/assert_tests_ran.py`, `pyproject.toml`, `uv.lock`, `tests/test_ci_scope.py`, `tests/test_repository.py`, `tests/workflow_scan.py`.
- **Done when:**
  0. verify-api: resolve each action tag to its SHA with `gh api`, and record the mapping in the report.
  1. The tests below are red on c3cdab2 and green.
- **Tests:**
  1. Every `uses:` names a 40-hex SHA, `setup-uv` names a version, and every `uv run` and `uv sync` in CI is `--locked`.
  2. Seeded strings of each new shape are matched by the guard, and a planted one fails it.
  3. The scanned set includes a known `.prawduct/` file.
- **Verification ceiling:** `tests/test_ci_scope.py tests/test_repository.py tests/test_assert_tests_ran.py`, plus ruff and black. No `uv lock --upgrade`.
- **Model:** Sonnet-class, high effort.

### Chunk 10: The label captions from the cache across a restart

- **Description:** closes audit-3 S4.
  - `LabelPull` writes each accepted label document atomically under the cache, named by the label id that Chunk 03 now checks, and exposes `cached()`, as `ClientPull` does.
  - `LabelRenderer` seeds its document from `cached()` before its first poll. A restart, or a renderer the supervisor restarted, during a server outage then shows the last caption under the rule's offline hold rather than blank.
  - How long the hold runs after a restart is the builder's call against M3's monotonic rule. Record it under Decisions made mid-build.
- **Depends on:** Chunks 03 and 06 merged. Serial on the integrated branch.
- **Owns:** `src/arrt_player/pull.py` (`LabelPull` only), `src/arrt_player/label_renderer.py`, `tests/test_label_renderer.py`, `tests/test_pull.py` (label tests only).
- **Tests (red today, green after):** a renderer built with a link whose server is unreachable, and a cached document, draws that document's caption on its first tick (today `drawing_for(None)` gives blank).
- **Verification ceiling:** `tests/test_label_renderer.py tests/test_pull.py tests/test_main.py`, plus ruff and black.
- **Model:** Opus-class, medium effort.

### Chunk 11: The wording sweep and the records

- **Description:** closes #14 (audit-3 M8). The "display plane", "this plane" and "curation plane" wording is swept from `src/`, from `pyproject.toml`'s description and from the runbook, including the operator-facing "display plane cannot start" in `__main__.main`. This chunk then applies every chunk's records owed to the Player's `.prawduct/artifacts/` and to `deploy/README.md`.
- **Type:** cumulative-final
- **Depends on:** Chunks 01-10 merged. Serial on the integrated branch.
- **Owns:** comment and string wording anywhere under `src/`; `pyproject.toml` (description only); `.prawduct/artifacts/architecture.md`, `.prawduct/artifacts/nonfunctional-requirements.md`, `.prawduct/artifacts/security-model.md`, `.prawduct/artifacts/observability-strategy.md`, `.prawduct/artifacts/hdmi-output-findings.md`; and wording only in `deploy/README.md` and `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`.
- **Acceptance:**
  - Count before and after: `grep -rniE "display plane|this plane|curation plane|the plane" src/ pyproject.toml` is empty after (57 hits at c3cdab2, by the audit's count).
  - Any test asserting the old operator message is updated with the message.
  - Each records-owed item is ticked off against its chunk's report (learnings: "treat the list as a checklist").
  - `tests/test_artifact_citations.py` passes.
- **Verification ceiling:** the test files whose strings changed, `tests/test_artifact_citations.py`, `tests/test_norm_index.py`, plus ruff and black. The coordinator runs the whole suite after it.
- **Model:** Sonnet-class, high effort.

## Governance checkpoints

- **Commit and PR cadence:**
  - Each delegate commits on its own worktree branch, with subjects `fix(audit-wave-1): … (Chunk NN)`.
  - The coordinator merges each delegate's branch into `fix/audit-wave-1` and reviews each chunk there.
  - Chunk 02 gets a `final` review before Chunk 05 merges, because both touch the Frame worker's neighbourhood.
  - Chunk 11's cumulative review makes the branch PR-ready.
- **The heavy run:** the whole suite runs on the integrated head after Chunk 09 and after Chunk 11.
- **Owner checks that are not tests:**
  - At the set, whether the held verbs matter (ledger).
  - At the Pi, whether the hardened unit starts and draws (runbook, Chunk 08).
