---
artifact: build-plan
version: 2
scope: audit-wave-2
branch: fix/audit-wave-2
base: develop
working-branches: []
depends_on:
  - artifact: architecture
  - artifact: security-model
  - artifact: nonfunctional-requirements
  - artifact: observability-strategy
  - artifact: data-model
  - artifact: samsung-tv-state-findings
  - artifact: hdmi-output-findings
  - artifact: project-preferences
governed_by:
  - artifact: project-preferences
    dispositions:
      - "The television belongs to whoever is using it (Arrt's NFR) → conforms: Chunk 03 pins the two set-reading paths the wave-1 review left unpinned and may remove one read; Chunk 06's removals from the set are write verbs, so they go through the wave-1 gate and are held while somebody watches. Nothing adds a verb outside `TvClient.WRITES`."
      - "A found problem is fixed, not filed, unless it is (orthogonal AND needs design) or medium+ → conforms: every Player-side finding left by wave 1 and every residual in the landing log is a chunk item, a recorded acceptance with its reason, an operator's check, or waits for the server's contract tag (ledger `ledger-player-wave-2.md`)."
      - "Every lint waiver says why → conforms: each new waiver follows the reasoned form."
      - "The TV pairing token is never tracked → conforms: no chunk touches the token file's location."
      - "The Player never requires the server to be reachable → conforms: Chunk 02 keeps the last good feed when the cache cannot be written; Chunk 07 deletes nothing while the client document is unreadable or absent; Chunk 05's file outputs render from the cache like any other."
      - "The e-paper label is legible at standing distance → conforms: Chunk 01 changes how a panel is found, never its type scale; the viewing distance stays the panel's `.env` value (decision below)."
      - "The manifest and its media are the only channel from the server; only pull.py opens an HTTP client → conforms: Chunk 11 reads the local address of the pull's own connection and opens nothing; Chunk 08's systemd notification is a local datagram socket, not a request to the server; Chunk 05's process test starts the Player against the suite's double."
      - "A display device renders its own label, and the label travels as metadata → conforms."
      - "The heartbeat's instant is reported under the key the contract names → conforms: Chunk 11 adds an optional key to the client heartbeat only once the contract names it."
      - "Derived artifacts are regenerated, never transported → conforms: Chunk 07's retired-wall sweep and Chunk 06's binding retirement delete what can be made again."
      - "Coverage expectations; Testing strategies (the network and the hardware doubled at their module boundary) → conforms: Chunk 05's file outputs are a real output kind the product ships, not a test double; the suite still drives the TV, panel and KMS through their doubles."
      - "The Player is stopped with SIGTERM, never SIGKILL → conforms, and is extended: Chunk 08 sets `WatchdogSignal=SIGTERM`, so a Player the watchdog ends takes the same stop path as `systemctl stop`; `KillSignal` and `TimeoutStopSec` are kept and still tested."
      - "A change to the contract is a pull request and a new tag on Arrt; the installed package is never patched → conforms: Chunks 10 and 11 wait for the server's tag; every contract need is a request in § Requests to the server's contract chunk."
      - "Everything the Player writes is its own account's alone → conforms: new files (the retired-wall stamp, the file outputs' renders) are written under the unit's UMask=0077 by the existing atomic writer."
      - "Every log line carries its event as a field → conforms, and Chunk 07 adds the wall's id to every line logged inside a wall's task."
      - "Tests live in `tests/` → conforms: Chunk 05's process test is in `tests/`; the file outputs in `src/` are product code."
      - "The only-channel norm's text (audit-1 S6) → [DECISION: Arrt's `architecture.md` § Direction holds the full text of a norm that binds the Player, its Player-side narrowing included; the Player's index row becomes a pointer once Arrt's amendment lands, as a coordinator's edit | the 2026-11-11 ruling made Arrt's Direction sections the home of the index's pointer rows, and CLAUDE.md already says Arrt's hold the norms; this is the conservative reading, not put to the owner separately | user can veto/override]"
      - "Delegation / Delegate verification → unset; the methodology default holds: a delegate runs only the narrowest tests covering its own change."
partition: "Four phases, then one serial chunk. Phase 1: 01, 02, 03, 04 fanned out to four delegates in isolated worktrees, disjoint files. Phase 2, once phase 1 is merged: 05, 06, 07 in parallel (05 and 07 after 01, 06 after 03). Phase 3, once phase 2 is merged: 08 and 09 in parallel (both after 05, for __main__.py and config.py). Phase 4 waits for the server's wave-2 contract tag and for phase 3: 10, then 11 serial after it. 12 (records, cumulative-final) is last and serial. The coordinator integrates on fix/audit-wave-2 and runs the whole suite after each phase; delegates run only their ceilings"
dropped:
  - "10: moved, not dropped (2026-11-19, the coordinator): moved to the next Player plan, after `contract-v1.2.0` exists; the tag cannot be cut until the server's wave-2 branch is reviewed and merged, and holding this branch's review for it buys nothing"
  - "11: moved, not dropped (2026-11-19, the coordinator): moved to the next Player plan, after `contract-v1.2.0` exists, with Chunk 10, since `settings_url` is a field only that tag names"
last_validated: 2026-11-19
---

# Build plan: the Player's share of the 2026-11-19 audit, wave 2

## Goals

**Response taken:** proceed. The owner asked for substantially all identified
product issues to be fixed by delegated builders under governance (2026-11-19, as
relayed by the coordinating session). Wave 1 (`build-plan-audit-wave-1.md`, merged
as #50 at f0138b0) fixed everything that needed no ruling, no contract change and no
other chunk's output. Wave 2 is what it left: the findings that needed design, the
owner's rulings of 2026-11-19 (`.prawduct/owner-rulings.md`), the contract tag the
server's wave 2 cuts, the residuals the wave-1 delegates and reviewers recorded, and
the 18 observations of the wave-1 boundary review (recovered from the evidence store,
review `rev-20261119T172527Z-139c337a`, and its two verify passes).

**Product:** a wall that keeps working through a full card or a locked store (#9,
#19); a Frame that stops collecting the images of works the household no longer
hangs (#18); a cache that stops growing with every wall and label that ever left
(#5, the label cache); a journal that says which wall each line is about (#10); a
Player that systemd restarts when it wedges (audit-8 F16a); a settings page that
holds the Frame's address and shows the server's name for the Player (audit-5 F7);
a Player that can run with no hardware at all, so a real Player can be tested
against a real server (audit-7 C1); the contract pin moved to the server's tag, so
the transport rules the Player relies on are written once, in the contract; and the
server told where the Player's settings page is (audit-5 F6c).

**The owner's rulings this plan is built on (2026-11-19, in their words in
`.prawduct/owner-rulings.md`):**
- **Not now: the Player's code owned by root with `ProtectSystem=strict`.** "That's
  just too heavy for right now. We may do that eventually, but not yet." No chunk
  changes who owns the checkout, the venv or `.env`, or how a Pi is updated. mpv goes
  on running as the service account, able to read its settings, and the records say
  this is the owner's accepted state (Chunk 12). What hardening remains that needs
  neither change is in Chunk 08: a list of unit directives that restrict the process
  without touching the filesystem layout, the devices or the update procedure, each
  checked at the Pi.
- **One label panel per Player is the recorded limit for now, and nothing may be
  designed that closes off several.** "…we shouldn't design anything that precludes
  having multiple labels, just as we don't prevent having multiple displays per
  player. In the future, there may be players that only do labels and might have
  multiple." Chunk 01 records the limit where the configuration states it, removes
  the one structural assumption of a single panel (the label worker is bound to one
  panel object, whatever output a label names), and pins both shapes the ruling
  names, two labels on one Player and a Player with labels and no display, as
  accepted or refused by name. The audit of the Player for one-label and
  display-present assumptions is § The label audit, below.
- **CI is required to merge on `develop` and `main` once the pool has a fallback.**
  The Player's CI runs on GitHub's runners and does not use the pool, so the
  condition holds here now. Turning on the protection is a coordinator's step at
  the end of the wave (§ Governance checkpoints), never a delegate's.
- **Confirmed, no work:** `arrt.local` is an example name; a guesser with two
  addresses can hold a device that has never signed in out of the settings page for
  as long as the guessing continues (the wave-1 decision stands).

**Architecture:** what must hold.
- The art-mode read stays fresh and at the seam; every write goes through
  `TvClient.WRITES`.
- A wall renders only from its cache. The pull stays the one HTTP client.
- Each wall's fault stays its own; a fault in the store, the cache or one label
  never stops another wall.
- A label is an output a Player has zero or more of, as a display is (ruling).
- The contract pin moves only to a released tag (Chunk 10).

**Constraints:** delegates run nothing under `tools/` (those drive real hardware;
Chunk 04 edits docstrings there and runs none of it), nothing that reaches a server
or a device, and never the whole suite. The integrated branch's suite runs and every
Critic review are the coordinator's.

**Decisions** (each vetoable, each stated where its chunk needs it):
- [DECISION: the settings page's address is reported to the server now, through an optional client-heartbeat field the server's contract chunk adds; no pairing code is built | audit-5 F6c. The field is additive and the Player already knows its port; a pairing code adds an unauthenticated route that `security-model.md` would have to defend, which is worth it only when somebody other than the owner sets up a Player. The conservative reading of the wave-1 recommendation, not put to the owner separately | user can veto/override]
- [DECISION: the label panel's viewing distance stays the panel's `.env` value in inches; the Player does not read the manifest's `settings.viewing_distance_m` for the label | audit-5 F7c. Arrt's `accessibility-spec.md` § deployment values makes panel diagonal and viewing distance the two `.env` values with no default, in inches "so that neither has to be remembered as feet". A label is read from closer than the wall it captions is seen from, so the wall's room distance is not the panel's. The manifest's field sizes overlay type on the wall's own screen, which this Player does not draw. The server's planner is asked not to add a label distance for the Player's sake (§ Requests) | user can veto/override]
- [DECISION: the Frame's address (`TV_ADDRESS`) moves onto the settings page; the mat's width and bottom weight, the panel's physical facts, the location and brightness stay in `.env` | audit-5 F7b. The address is the one Frame fact a household must change without SSH (a router hands the set a new lease). The mat's width is the client's by feeds-and-players ruling 7 and is a fact of the screen; the panel's facts are measurements made once at mounting; the page exists for what a household changes. Pointing the page at a different television asks that set to pair (#2), and the page says so | user can veto/override]
- [DECISION: a binding is retired when no adopted feed has named its work for 14 days; retiring removes the image from the set through the gated delete verb, then the row | #18. Absence from one feed is not retirement: a theme switch shrinks the feed legitimately, and treating that as retirement re-uploads the corpus on the way back (the issue's own warning). Fourteen days outlasts a holiday theme and a round trip between themes; a work that returns after that is uploaded again, which costs one upload | user can veto/override]
- [DECISION: a wall's cache directory is removed 7 days after the wall left this Player, measured from a stamp written when it left; nothing is removed while the client document is unreadable or absent | #5. A wall moved away and back within the week keeps its masters, pictures and Frame bindings; the Frame store's content ids matter only while the same wall may return to the same set, and the orphan path already removes images no wall on that set accounts for | user can veto/override]
- [DECISION: a configuration naming more than one label panel is refused at start (exit 2) with a message naming the one-panel limit and the 2026-11-19 ruling; a client document mapping a label to an output this Player does not hold is accepted, that label is not started, and it is said once (as today) | the ruling. A second panel that is silently not driven reads as broken hardware, and every other unusable `.env` value already stops the Player by name; the server's mapping is the server's to correct, and the client heartbeat already tells it which label outputs exist | user can veto/override]
- [DECISION: the Player ships an output kind that draws to a directory, off unless its key is set, so a real Player process can run with no screen, television or panel | audit-7 C1. Any test of a real Player against a real server has to run the Player's own entry point on a machine with no hardware; importing the Player's internals into the server's tests would couple the server to code that is not the contract. A file output is one more implementation of the existing output seam | user can veto/override]
- [DECISION: Dependabot is configured for GitHub Actions only, monthly; `uv.lock` updates stay a hand step | audit-7 M5. The workflow's actions are pinned by commit (wave 1), and Dependabot is how a pin learns its tag moved. A lock update moves the Waveshare pin and the contract's git source, and the learnings rule on bumping a dependency asks for its call sites to be exercised first, which a bot does not do | user can veto/override]
- [DECISION: the watchdog is petted only when the Player has made progress (the supervisor finished a cycle, the slideshow advanced, or in setup mode the page's task still running on a loop that turns, which is weaker than the page answering: see § Decisions made mid-build), never merely because the event loop is alive; `WatchdogSignal=SIGTERM` | audit-8 F16a. A loop that is alive while every worker is stuck is the wedge the finding names; SIGTERM keeps the norm's stop path, so the set's art channel is closed rather than abandoned | user can veto/override]

**Ruled out for now:** root-owned code and venv, `ExecStart` without a sync,
`ProtectSystem=strict`, mpv under its own account (audit-8 F2b; the owner,
2026-11-19). A pairing code (audit-5 F6d), by the decision above. Several label
panels or Frames actually driven (#23, audit-3 M4): recorded as the limit; Chunk 01
only keeps the path open.

**Not in this wave, and why:** #12 (failures that never reach the server's view)
needs contract fields and is not an audit finding; #31 (Arrt mode on mpv, moving
media) is a feature at `stage:requirements` and Chunk 10 moves the pin without it;
#2 waits for the operator's check (§ Operator's checklist, item 4); the Player's page
status line in the server's state words (audit-5 F4) waits for the server to settle
one state vocabulary, which is requested for the contract (§ Requests, R7) and taken
in Chunk 10 if the tag carries it.

**Tradeoffs accepted:**
- A label document cached under a read-only cache can stay older than the server's
  last answer (wave-1 Chunk 10's residual). Accepted: its hold runs from its own,
  older stamp, so it is never held longer than a fresh one would be, and a read-only
  cache also stops every wall's manifest write, which the wall heartbeat already
  reports.
- `FakeTv` stays importable from both `tests/fakes.py` and `tests/fake_tv.py`
  (review O-8). The reviewer offered "or keep it and say so"; the re-export's waiver
  already says why, and pointing six importers at `fake_tv` edits files four chunks
  own.
- The multi-major machinery (`pull.HIGHER_MAJOR_SECONDS`, `Wall._programmes`) is
  kept: it is readiness for the next manifest major, stated where it is defined.
- The pair test of a real Player against a real server lives in Arrt (§ The test of
  a real Player against a real server), so a change to the Player is tested against
  the server only when the server moves its pinned Player commit. The release
  runbook says to move it (Chunk 12).

**Decisions made mid-build:** (appended by the coordinator from the chunk reports;
the first three recorded at the boundary review, rev-20261120T002012Z-647a4891, R-6.)
- [DECISION: a Frame binding is used only on the television it was made on, known by the set's `device.duid`, not its address. The store moves to schema 4 (`tv_identity`, and `tv_binding_aside` for another set's bindings); a store whose recorded identity is not the connected set's sends no removal; a store from before schema 4 takes the first set it reads as its own; a different set starts afresh (every work uploaded, and the set's upload category cleared of what the Player cannot account for, the household's own pictures included, as a first run already does), while the old set's bindings are set aside for its return; a set that will not say which it is gets no stored binding for `IDENTITY_PATIENCE_SECONDS` (15 minutes), after which the wall shows art from a store held in memory and removes nothing; and an operator whose Frame address ever moved to another set deletes that wall's store before upgrading (runbook step 6a) | built in Chunk 09's commit 72a96b3, because a set's content ids are its own and nothing tied a binding to its set: a wall pointed at another television selected that set's pictures by the first set's ids, and could retire, or keep as accounted for, ids naming the household's pictures; the reasoning is in `architecture.md` decision 14, `state/store.py`'s docstring and `security-model.md` § What it does to the television, and `architecture.md` decision 14 leaves open for the owner whether the Player should remove only what it recorded uploading | user can veto/override]
- [DECISION: setup mode pets the watchdog every `SETUP_PET_SECONDS` (30 s) while the page's task runs on a loop that turns (`notify.Watchdog.while_running`), not when a request is answered | setup mode serves nothing until somebody opens the page, so "the page answered" could be hours apart on a healthy Player and the watchdog would kill it; a wedged setup mode is a loop that has stopped turning, which stops these pets too. A page handler that hangs while the loop turns is not caught, and `deploy/README.md` says so | user can veto/override]
- [DECISION: `ProtectClock=` is left out of the unit's hardening, although Chunk 08 listed it | it implies `DeviceAllow=char-rtc`, and one `DeviceAllow=` turns the device policy into an allowlist that would refuse the display card, the render node, SPI, GPIO and the DDC bus at the next start; the unit's comment beside the hardening says so | user can veto/override]

**Level:** Medium. Every chunk has a red test buildable against the doubles today.
What stays unmeasured is the hardware's answer (§ Operator's checklist) and the
server's tag's exact contents (Chunks 10, 11). What would raise it: the server's
contract chunk answering § Requests, and the operator's items 1, 2 and 10.

## Commit subjects, and how a chunk is attributed

`prawduct-hook plan status` and the boundary review count a chunk as built only by
a **first-parent** commit on `fix/audit-wave-2` whose subject carries **this plan's
scope** and **the chunk**: `<type>(audit-wave-2): <what> (Chunk NN)`
(`plugin/lib/plan_status.py`, `_carries_scope` and `_CHUNK_COMMIT_RE`; a path the
chunk lists gives no credit). In wave 1 the merge commits read
`Merge audit/NN: … (chunk NN)`: they named the chunk but not the scope, so only the
two chunks a review named with `--chunks` were certified. Therefore:

- **The coordinator's merge of a delegate branch is the commit that counts**, because
  it is the first-parent commit: `git merge --no-ff audit2/NN -m "merge(audit-wave-2): <what> (Chunk NN)"`.
- A delegate's own commits use `fix(audit-wave-2): <what> (Chunk NN)` (they sit on
  the second parent and do not count, but a fast-forward or a squash would make them
  count, and they read the same).
- A boundary or fix-round commit on the integration branch names every chunk it
  serves: `fix(audit-wave-2): <what> (Chunk 03) (Chunk 07)`. Never `(chunk 03 asked)`:
  the id must be followed directly by `)`.
- A review may also name its chunks with `critic-begin --chunks 03,07`.

Every brief quotes this section.

## The label audit (the owner's ruling of 2026-11-19)

Read on f0138b0, each assumption of exactly one label, or of a display being present,
with what Chunk 01 does about it:

| Where | What it assumes | Wave 2 |
|---|---|---|
| `src/arrt_player/config.py` `PANEL_OUTPUT = "epd-0"`, `PanelSettings.epd_device` (one value), module docstring "one label output, named `epd-0`" | one panel per Player | Kept as the recorded limit, stated as the ruling's ("by ruling, 2026-11-19, not by design"). A value naming more than one panel is refused by name (decision above). |
| `src/arrt_player/__main__.py` `label_worker=partial(run_label, settings, panel)` | every label, whatever output it names, draws on the one panel | **Structural; removed.** The label worker finds its panel by the label's output among the Player's panels, and a label naming an output with no panel is refused by name, never drawn on another panel. |
| `__main__.py` `label_outputs=lambda: [panel.report()]` and `panel.close()` at shutdown | one panel | Reports and closes each panel the Player holds. |
| `client.Supervisor._reconcile_labels` | keyed by output; refuses an output not held, and a second label on one output, each said once | Already per output. Tests exist for both refusals (`tests/test_client.py`); Chunk 01 adds the shapes below. |
| `pull.LabelPull`, `CACHE_DIR/.labels/<id>.json` | per label id | Confirmed per id (`pull.py`, `LabelPull.__init__`). Chunk 01's cleanup removes by id, never "the" file. |
| `label_renderer.LabelRenderer` | one per assignment | Per assignment already. |
| `heartbeat.Health.has_label_surface`, `label_surface_working` (wall heartbeat, required by `heartbeat.v1`) | one label surface per wall, from the one-wall daemon | Contract fields; the Frame writes `False`/`None`. Not a Player change: request R8 asks the contract to state them as the wall display's own, superseded by the client heartbeat's `label_outputs`. |
| `client.client_heartbeat` | `label_outputs` is a list; `outputs` may be empty | Already describes both shapes. |
| `config._frame` and `load` | a label panel needs no Frame and no screen | Already (`tests/test_config.py` loads `EPD_DEVICE` without `TV_ADDRESS`). Chunk 01 pins the running shape: a Player with no display output drives its label. |
| Settings page | names no label output | Nothing to change. |
| `FRAME_OUTPUT = "frame"` (one Frame, #23) | one Frame per Player | Out of the ruling's scope (it is about labels; HDMI screens are already several). Listed in Chunk 01's report, not changed. |

**The contract:** it already describes several labels on one Player
(`client.v1.labels[]`, `client-heartbeat.v1.label_outputs[]`, label names unique per
client) and a label-only Player (`outputs` may be empty, `walls` may be empty).
Nothing is missing from the schemas; request R9 asks for fixtures of both shapes, so
both repositories are held to them.

## Verification strategy

Each chunk proves itself with a test that is red on `develop` at f0138b0 (or on the
phase's integrated head, for a chunk of phase 2 or later) and green after. The report
shows the red run, as the failing output or as the re-break that turns the test red
again. Learnings: "When you write a check, prove it can fail". Each delegate's
ceiling is the run below, over the files its chunk names and nothing wider:

    testament run --json -- <its test paths>        # else: uv run --group raster pytest <its test paths>
    uv run ruff check <its paths> && uv run black --check <its paths>

Never the whole suite, never anything under `tools/`. The coordinator runs the whole
suite (`testament run --json`) on the integrated head after each phase and after
Chunk 12.

**Every brief carries these, from wave 1's lessons:**
1. **Attribution:** § Commit subjects.
2. **Implicit guarantees:** a chunk that replaces or consolidates a mechanism lists,
   under its acceptance, what the old mechanism guaranteed without anyone writing it
   down, each with a test. Each chunk below names the ones the plan could see; the
   delegate adds any it finds.
3. **Sibling instances:** a chunk that fixes one instance of a shape greps its files
   and their neighbours for the rest, and fixes or lists each in its report.
4. **Shared values:** a value the server and the Player both state gets one named
   owner, and the other side quotes it with a test that fails if they diverge
   (Chunk 10 for the pairing labels and the bottom weight; Chunk 11 for the
   settings address's wire name).
5. **Rewritten tests:** a chunk that changes an existing assertion lists it in its
   report with the old assertion and the reason; the ones the plan foresees are named
   in the chunk.

**Every report returns:** the red-then-green evidence; the records owed (each sentence
in a `.prawduct/artifacts/` file, `deploy/README.md` or the runbook that the change
falsifies, with its replacement), for Chunk 12; the test ids a norm-index row should
gain (for the coordinator); and any file outside the chunk's boundary that it needed
and did not touch.

**Files no chunk edits:** `tests/conftest.py`, `tests/test_wall.py`,
`tests/test_majors.py`, `src/arrt_player/wall.py`, `src/arrt_player/tv/client.py`,
`.prawduct/project-state.yaml`, `.prawduct/owner-rulings.md`,
`.prawduct/artifacts/project-preferences.md` (the coordinator's), and everything under
`tools/` except Chunk 04's docstring edits. A chunk that cannot pass without one of
them stops and reports.

## The test of a real Player against a real server (audit-7 C1)

The server's planner decides where this lives; its plan
(Arrt's `build-plan-audit-wave-2.md`) did not exist when this was drafted.
**Proposal: it lives in Arrt's curation suite, on the pool**, because only the
server's own fixtures can seed a wall, a work with a real master, a client and its
token without the network, and because it then runs on every server change. It runs
the Player as a **process** from the Player's own lock (a checkout at a pinned
commit, `uv sync --frozen`), never installed into the server's environment, so
neither project's dependencies constrain the other's; it points that process at the
server's real uvicorn with Chunk 05's file outputs and no television or panel, and
asserts both sides: one client poll, one pull and adopt drawn to the file output, a
client heartbeat and a wall heartbeat recorded by the server, a label poll answered
and its decision written, a 304 round, and a refused token. **The assumption it rests
on:** the server's curation suite may start a subprocess and read a pinned Player
commit from one file the server owns and bumps at each Player release. The Player's
half is Chunk 05: the output kind, its documented keys, and a process-level test of
the same steps against `tests/server_double.py`, so the invocation is proven here
before the server's test quotes it. If the server's planner places the test in this
repository instead, Chunk 05's process test is its frame and only the server boot
differs.

## Requests to the server's contract chunk

What the Player relies on that no schema or vector pins, and what the Player needs at
the server's wave-2 tag. Each is a request to the server's contract chunk; Chunk 10
takes whatever the tag carries.

- **R1. A machine-readable response table** (audit-1 M8, audit-3 § Contract
  conformance): for each route in `routes.json`, the statuses it answers and what each
  means to a Player: `200` with an ETag, `304`, `400` for a body the schema refuses,
  `401`, `403`, `404`, `413` (the 256 KiB request-body cap from the server's wave 1),
  `5xx`, the error body's shape, and a stated size bound for each response document
  the Player reads (the manifest has none today: wave-1 Chunk 03's residual). With a
  column for the Player's expected reaction: a master `404` skips the work until the
  feed changes; a master `5xx` or timeout holds the feed (bounded at 10 minutes); a
  heartbeat `4xx` is not resent until the report changes, a `5xx` is retried; `401`
  is refused and said once; `403` on a wall is "not this client's".
- **R2. A conditional-GET vector:** a sequence of (document bytes, `If-None-Match`
  sent) → (status, ETag) cases pinning that the ETag is a function of the document's
  bytes, that `304` is answered only when `If-None-Match` equals the ETag of the
  document that would be sent (an identical document), and that a changed document
  never answers `304`. The server's route tests assert it; the Player's double replays
  it.
- **R3. Contract text** in `player-contract.md` § Transport: a `404` for a master the
  feed names means the server does not hold it and will not until the feed changes;
  the client document's ETag changes if and only if its content does (since wave 1 the
  Player re-matches its own outputs, so it relies on this only for the document).
- **R4. A must-refuse fixture for each schema constraint** the three Player parsers
  restate by hand (audit-3 § Contract conformance): every `required`, `enum`,
  `pattern`, `minimum`/`exclusiveMinimum`, `minItems`/`maxItems` in `manifest.v2`,
  `client.v1` and `label.v1`.
- **R5. `client-heartbeat.v1` gains an optional `settings_url`** (a minor): an absolute
  `http` or `https` URL of the Player's own settings page, as the Player can be reached
  on the network it reaches the server from; absent when the page is bound to loopback
  or the Player cannot tell. The server's Players row links to it.
- **R6. The mat's bottom weight as a named contract default** (audit-1 M3: 1.15, now
  `config.DEFAULT_MAT_BOTTOM_WEIGHT` here and `adequacy.BOTTOM_WEIGHT` there), and the
  stale `_about` of `vectors/mat-geometry.json` (audit-1 M4).
- **R7. A vocabulary file in the package, owned by the contract:** the words the two
  products must say alike. Now: the pairing field labels, "Server address" and "Token"
  (the Player's page and the server's token dialog crossed in wave 1, audit-5 F6a).
  Later, when the server settles one state vocabulary: the Player's state words for
  its page's status line (audit-5 F4). Both sides quote it in a test.
- **R8. The wall heartbeat's superseded fields** (`television_showing_art`,
  `television_reachable`, `has_label_surface`, `label_surface_working`): keep them
  required within major 1 (the Player goes on writing them; retiring a required field
  in a minor would invalidate the server's own fixtures), and state in the schema text
  that they describe the wall's own display, superseded by `display_state` and by the
  client heartbeat's `label_outputs`, so nothing reads `has_label_surface` as "the
  Player's one label".
- **R9. Fixtures for the ruling's two shapes:** a valid `client-heartbeat.v1` with
  `outputs: []` and two `label_outputs`; a valid `client.v1` with `walls: []` and two
  `labels` on two outputs; and an invalid `client-heartbeat.v1` with two label outputs
  sharing a name, if the server's validation refuses it (the schema states the rule
  in prose only).
- **R10. A cross-tag compatibility test** on the server (audit-1 M2, audit-7 M6): every
  valid fixture at the tag a released Player pins still validates at HEAD.
- **R11. No label viewing distance for the Player's sake** (decision above): the
  panel's distance stays the panel's.

## Build Chunks

### Chunk 01: Labels are outputs a Player has zero or more of, and retirement is said one way

- **Description:** closes the owner's ruling of 2026-11-19 on labels (with audit-3 M4,
  ledger row 15, as the recorded limit), and review observations O-1, O-4, O-7 and
  O-14 with wave-1 Chunk 10's residual (cached files of unmapped labels).
  - **The ruling:** § The label audit. The label worker finds its panel by the label's
    output; the Player reports and closes every panel it holds; the one-panel limit is
    stated where the configuration defines the panel, as the ruling's; a value naming
    more than one panel is refused (Goals). Nothing is built that drives a second
    panel.
  - **O-7:** a wall's worker is told it is retired through `WallStop.retired`, a
    label's renderer through a second `asyncio.Event`. One stop type carries the reason
    for both, and the "called once more, already stopped and retired" convention is
    stated once, where the type is.
  - **O-4:** the once-more call for a retired label logs `label.renderer_started` for
    a label that captions nothing. A retired renderer's last call says what it does
    (blanks), not that it started.
  - **O-1:** `run_label` gives `LabelPull` the injected clock's monotonic but builds
    `LabelRenderer` without it, so the offline hold's monotonic is the process's, and
    nothing tests the wiring.
  - **O-14 and the residual:** a retired label's cached document
    (`CACHE_DIR/.labels/<id>.json`) is removed when the label is retired, and files for
    label ids the current client document does not map are removed at reconcile, once
    a document has been read (never while it is unreadable or absent). The path is
    learned through `LabelPull`, or through one helper both use, never re-derived by
    hand.
- **Depends on:** none (phase 1).
- **Deliverables:** `src/arrt_player/client.py`, `src/arrt_player/label_renderer.py`,
  `src/arrt_player/__main__.py` (label wiring only: `label_panel`, `run_label`, the
  supervisor's label arguments, the panel's close), `src/arrt_player/config.py` (the
  panel settings and their constants only), `tests/test_client.py`,
  `tests/test_label_renderer.py`, `tests/test_main.py`, `tests/test_config.py`,
  new `tests/test_label_outputs.py`.
- **Must not touch:** `src/arrt_player/pull.py` (read `LABEL_CACHE_DIRNAME` and
  `LabelPull`; if a public path helper is needed there, report it for the coordinator),
  `tests/test_pull.py`.
- **Tests (red on f0138b0, green after):**
  1. `EPD_DEVICE` naming two panels is refused by `load()` with a message naming the
     limit and the ruling (today it is taken as one driver name and fails later as
     `panel.unavailable`).
  2. A Player with no display output and one panel, given a client document with no
     walls and one label on its panel, draws that label and reports `outputs: []` with
     the label output in its client heartbeat (a guarding test: say whether it was
     green on f0138b0).
  3. A client document with two labels on two outputs, one of which the Player holds:
     the held one is drawn on its own panel, the other is said once by name and never
     drawn on the held panel (the second half is red against a worker bound to one
     panel, when driven through `__main__`'s wiring with two panels).
  4. O-1: `run_label` with an injected clock: the renderer's hold runs on that clock's
     monotonic.
  5. O-4: a label retired while its renderer waits to restart: its last call logs no
     `label.renderer_started`.
  6. O-14: a label document cached through a real `LabelPull` fetch is gone after the
     label is retired; a stale file for an unmapped id is gone after the next
     reconcile; nothing is removed while the document is unreadable.
- **Implicit guarantees to keep, each with a test (O-7 consolidates a mechanism):** a
  renderer stopped because its label now captions another wall is not retired and
  does not blank; a retired renderer blanks once; the once-more call after a crash
  arrives with stop set and retired; `stop_all` retires nothing; a new renderer waits
  for every renderer still stopping before it draws; a wall worker's existing
  signature `(settings, wall, output, stop)` still works for workers that ignore the
  reason; the restart ladder's timings are unchanged.
- **Sibling instances:** grep `__main__.py` and `client.py` for other single objects
  bound into a worker for every assignment (the Frame's `frame_tv`, the identity);
  list each in the report, change only labels (#23 is outside the ruling).
- **Verification ceiling:** `tests/test_client.py tests/test_label_renderer.py tests/test_main.py tests/test_config.py tests/test_label_outputs.py tests/test_pull.py tests/test_label_rule.py`, plus ruff and black on the owned files.
- **Model:** Opus-class, high effort. The ruling is a design constraint and O-7 is a
  consolidation with five implicit guarantees.

### Chunk 02: The pull outlives a cache it cannot write, and production runs the cycle the tests run

- **Description:** closes #9 (audit-3 M8, ledger row 20) and review observation O-6.
  - **#9:** an `OSError` from `_write_atomically` while adopting a feed (the manifest
    or its ETag) ends the wall's pull, and the supervisor restarts it after 5–300 s.
    The pull now keeps the last good feed, says the fault once per episode (a new
    event; name it in the report), writes no ETag for a feed it could not write, and
    tries again on the next poll under the existing backoff. The same holds for the
    client document's and a label document's writes (sibling instances in
    `ClientPull` and `LabelPull`).
  - **O-6:** `Pull.cycle` is a composition only the tests run since wave 1 put the
    heartbeat on its own loop. Make the tests drive what production runs: `run`'s loops
    and `cycle` call the same two bodies (one manifest poll, one heartbeat forward), and
    a test proves `run` uses them.
- **Depends on:** none (phase 1).
- **Deliverables:** `src/arrt_player/pull.py`, `tests/test_pull.py`.
- **Must not touch:** `src/arrt_player/client.py`, `tests/server_double.py`.
- **Tests (red on f0138b0, green after):**
  1. The manifest write raises `OSError(errno.ENOSPC)` once: the pull task is still
     running, the last good feed is still the cached one, no ETag file was written,
     and the next poll writes the feed.
  2. The fault said once across three failing polls, and its end said once.
  3. The same for `ClientPull`'s document write and `LabelPull`'s cache write.
  4. O-6: `Pull.run` drives the manifest body and the heartbeat body that `cycle`
     drives (patch one, observe both callers).
- **Implicit guarantees to keep, each with a test:** an ETag is never stored without
  the document it describes (wave-1 C2); a feed is adopted only after every master is
  answered for (the bounded hold); the heartbeat loop's cadence is independent of the
  manifest backoff.
- **Rewritten tests:** if any of the 97 `cycle` call sites in `tests/test_pull.py`
  changes its assertion, list each with the old assertion and why.
- **Verification ceiling:** `tests/test_pull.py tests/test_heartbeat_wiring.py tests/test_player_contract.py tests/test_plane_isolation.py tests/test_availability.py`, plus ruff and black.
- **Model:** Opus-class, medium effort.

### Chunk 03: The Frame: a store fault has a line, the set's news is pinned, and its events say "frame"

- **Description:** closes #19 (ledger row 21), the observation the wave-1 review left
  open (verify pass `rev-20261119T180115Z-725f4812` O-1; landing log "Observation left
  as recorded"), and the `journal_name="daemon"` residue (audit-3 § Multi-wall model,
  ledger row 39).
  - **#19:** a `sqlite3` error under the Frame wall's tick is not classified. Draw the
    line: a fault that may clear (`database is locked`, `busy`) is said at WARNING and
    the wall goes on at its next tick with the existing backoff shape; a fault it
    cannot outlive (disk full, read-only, I/O error, a malformed file) is one ERROR per
    episode naming the store operation and the class of fault, and the wall
    heartbeat's `last_error` names it, so the server sees it. The restart ladder stays
    the last resort. The builder records the classification table under Decisions made
    mid-build.
  - **The set's news:** (a) a `get_artmode` "no" read after an announcement
    (`_take_the_sets_news` → `_read_art_mode`) begins `rotation.wall_not_ours` even when
    the gate was never asked, and an announced picture ends it; neither is pinned.
    Pin both as intended or change them, and say which. (b) After the set's news ends a
    still-wall episode, the next ignored selection costs one more `get_artmode` read.
    Remove the read or pin it with a count, and say which.
  - **`journal_name`:** the Frame wall's `<journal_name>.*` events read `daemon.*`, the
    one-wall daemon's name. They become `frame.*`. Process-level `daemon.*` events
    (`daemon.misconfigured`, `daemon.setup_mode`, `daemon.crashed`, `daemon.stopped`
    and any other not emitted through a wall's `journal_name`) keep their names.
- **Depends on:** none (phase 1).
- **Deliverables:** `src/arrt_player/displays/frame.py`, `src/arrt_player/state/store.py`,
  `tests/test_frame_wall.py`, `tests/test_bindings.py`, `tests/test_selection_observers.py`.
- **Must not touch:** `src/arrt_player/tv/client.py` and `tests/fake_tv.py` beyond an
  armed behaviour (no changed default), `src/arrt_player/wall.py`.
- **Tests (red on f0138b0, green after):**
  1. The store raises `sqlite3.OperationalError("database is locked")` once from a
     write the tick makes: the wall goes on, one WARNING, no worker crash.
  2. The store raises `OperationalError("disk I/O error")` on every tick: one ERROR per
     episode with the operation named, and the next wall heartbeat's `last_error` names
     it.
  3. The two news paths of (a), each pinned by event and count.
  4. (b): the count of `get_artmode` reads over the named sequence, before and after.
  5. The Frame wall's start event is `frame.started` (today `daemon.started`).
- **Implicit guarantees to keep, each with a test:** the number of reads sent to the
  set on every existing path in `tests/test_frame_wall.py` (wave 1's hold refactor
  changed it once unseen); no write leaves the seam ungated; a held write is not
  recorded as failed.
- **Records owed include** every reader of the renamed events: grep the whole repo
  (no `--include`) for `daemon.` and list each hit as renamed, kept (process-level) or
  history. Arrt's runbook line about an *older* revision's `daemon.state_too_new` keeps
  the old name, because it describes what the older revision says.
- **Verification ceiling:** `tests/test_frame_wall.py tests/test_bindings.py tests/test_selection_observers.py tests/test_samsung_client.py tests/test_display_state.py tests/test_main.py`, plus ruff and black.
- **Model:** Opus-class, high effort.

### Chunk 04: Repository hygiene: actions watched, tools run frozen, one leak scanner, "theme" in the journal

- **Description:** closes audit-7 M5's second half (ledger row 79), the "left as is"
  usage lines of wave-1 Chunk 11 (the tools part), review observations O-5/O-11, O-9
  and O-12, and the owner's ruling 1 of 2026-11-19 ("theme, not playlist, wherever a
  person reads") where the Player's journal says it.
  - **Dependabot:** new `.github/dependabot.yml` for `github-actions`, monthly (Goals);
    a test holds it.
  - **Tools:** every usage line in a tool's docstring that is run on the Pi says
    `uv run --frozen …` (the checkout's venv is the deployed one); a line run only on a
    Mac may stay and says so. A test over `tools/*.py` docstrings holds it.
  - **O-9:** `tests/test_repository.py` has two leak scanners over two file listings.
    Fold `_DEPLOYMENT_PATHS` into the working-tree scan and retire the older test,
    proving with a seeded string that every shape the old one caught is still caught
    (learnings: "replace a badly-shaped guard in the same commit").
  - **O-12:** `tests/workflow_scan.py`'s opening describes one pattern; it holds four.
  - **O-5/O-11:** the doubled article in `tests/test_availability.py`'s docstring; grep
    for the second one the review counted and fix both.
  - **Ruling 1:** the journal line in `src/arrt_player/manifest.py` that says "adopted
    the feed for playlist …" says "theme". Grep `src/` for any other person-read
    "playlist" (log messages, page text); the contract's field names stay.
- **Depends on:** none (phase 1).
- **Deliverables:** new `.github/dependabot.yml`, `tests/test_ci_scope.py`,
  `tests/test_repository.py`, `tests/workflow_scan.py`, `tests/test_availability.py`
  (docstring only), `tests/test_tools.py`, `src/arrt_player/manifest.py` (wording
  only), `tests/test_manifest.py`, and docstrings only in `tools/power_probe.py`,
  `tools/tv_api_check.py`, `tools/tv_delete.py`, `tools/panel_check.py`,
  `tools/label_preview.py`.
- **Must not touch:** any code under `tools/` beyond docstrings; nothing under `tools/`
  is run.
- **Tests (red on f0138b0, green after):** the Dependabot test (no file today); the
  tools' usage-line test (red on `tools/power_probe.py`); the folded scanner catches a
  seeded `_DEPLOYMENT_PATHS` value through the working-tree scan; the journal line
  says "theme".
- **Rewritten tests:** retiring `test_no_source_file_carries_a_deployment_value` is a
  rewritten test: the report names it, its old assertion, the test that now carries
  each of its shapes, and the seeded proof.
- **Verification ceiling:** `tests/test_ci_scope.py tests/test_repository.py tests/test_assert_tests_ran.py tests/test_tools.py tests/test_manifest.py tests/test_availability.py`, plus ruff and black.
- **Model:** Sonnet-class, high effort.

### Chunk 05: A Player that runs with no hardware, proven as a process against the double

- **Description:** closes the Player's half of audit-7 C1 (ledger row 76; audit-1 M8's
  "nothing runs the two together"), by the file-output decision in Goals.
  - **The output:** one `.env` key names a directory, and the Player then lists a
    screen output there (kind `framebuffer`, connected, a size from a second key with
    a stated default) that draws each render it shows to a file and records a blank.
    A second key gives a label output that writes each drawing the label rule decides
    (caption, card or blank, with its wall and work) as JSON, needing no typesetter.
    Names, file layout and keys are the builder's, documented in `.env.example` under
    one heading the server's test can quote. Off unless set. Composed beside
    `client_outputs` in `__main__.py`; `client.py` is not touched.
  - **The process test:** `python -m arrt_player` started as a subprocess (the suite's
    own interpreter) against `tests/server_double.py` on a local port, with the file
    outputs, a cache directory and no television or panel. It asserts: the client
    document is polled; the wall's feed is pulled, adopted and drawn to the file
    output; a client heartbeat listing the file outputs and a wall heartbeat reach the
    double and validate; a label mapped to the file label output is polled and its
    decision written; the second manifest poll is a `304`; a process given an unknown
    token says so in the journal and starts no wall; SIGTERM ends the process with
    exit 0 inside the unit's stop window. Bounded to about 30 s; it reaches nothing
    outside the machine.
- **Depends on:** Chunk 01 merged (both edit `__main__.py` and `config.py`). Phase 2.
- **Deliverables:** `src/arrt_player/__main__.py`, `src/arrt_player/config.py`,
  `.env.example`, new `src/arrt_player/displays/file_output.py`,
  new `src/arrt_player/panel/file_surface.py`, new `tests/test_running_without_hardware.py`,
  `tests/test_main.py`, `tests/test_config.py`.
- **Must not touch:** `src/arrt_player/client.py`, `src/arrt_player/pull.py`,
  `tests/server_double.py` (if the double cannot serve a subprocess as it is, report
  the change needed; the coordinator assigns it).
- **Tests (red today, green after):** the process test above (red: the keys are
  unknown and nothing is drawn); the file output's unit tests (draw, refresh, blank);
  the label file surface writes the decided drawing; `load()` refuses the size key at
  a non-positive or malformed value by name.
- **Implicit guarantees to keep:** with neither key set, the outputs a Player reports
  and drives are exactly today's (a test over `client_outputs` plus the composition);
  the KMS output is still chosen for an `hdmi-*` connector.
- **Verification ceiling:** `tests/test_running_without_hardware.py tests/test_main.py tests/test_config.py tests/test_screen.py tests/test_client.py tests/test_plane_isolation.py`, plus ruff and black.
- **Model:** Opus-class, high effort. It sets the invocation the server's pair test
  will quote.

### Chunk 06: The Frame lets go of works nobody hangs any more

- **Description:** closes #18 (ledger row 23), by the binding-retirement decision in
  Goals.
  - Each binding records when an adopted feed last named its work (a store migration,
    forward only, as `state/store.py` already migrates).
  - At reconciliation, a binding unnamed for 14 days is retired: its image is removed
    from the set through the gated delete (held while somebody watches, like every
    write), its removal is confirmed by listing, and the row is deleted. A binding for
    the work on the wall now is never retired.
  - **The persisted format:** the questions the new column must answer, settled before
    its shape: "which bindings has no adopted feed named since when?"; "what did the
    Player take off the set, and when?" (answered by the journal, not the store);
    "can an older Player open this store?" (the report states what the previous
    release does with a migrated store, and the rollback step it needs, for Chunk 12).
- **Depends on:** Chunk 03 merged (same files). Phase 2.
- **Deliverables:** `src/arrt_player/displays/frame.py`, `src/arrt_player/state/store.py`,
  `tests/test_frame_wall.py`, `tests/test_bindings.py`.
- **Tests (red on the phase-1 head, green after):**
  1. A work absent from every adopted feed for 15 simulated days: its image is gone
     from `FakeTv` and its row from the store (today both stay).
  2. A theme switch away and back within 13 days uploads nothing again.
  3. A retirement due while the set shows a programme is held and done later.
  4. A store at the previous schema migrates forward and keeps every binding.
- **Implicit guarantees to keep, each with a test:** the orphan path is unchanged (an
  image the store cannot account for is removed as today); `accounted_content_ids`
  still accounts every binding not retired; the reads sent to the set per pass are
  unchanged when nothing is due.
- **Verification ceiling:** `tests/test_frame_wall.py tests/test_bindings.py tests/test_samsung_client.py tests/test_display_state.py`, plus ruff and black.
- **Model:** Opus-class, high effort.

### Chunk 07: Each wall's own: its events name it, and its directory goes when it does

- **Description:** closes #10 (outline W2-8) and #5 (ledger row 25).
  - **#10:** `pull.*` and `screen.draw_*` events carry no `wall_id`, and `logs` drops
    `taskName`. A context variable set where the supervisor starts a wall's task (and a
    label's, with its label id) is stamped onto every record by a filter, as
    `work_context` stamps the work id; `asyncio.to_thread` carries it.
  - **#5:** by the decision in Goals. A wall that leaves this Player gets a stamp in its
    directory; at reconcile, a directory whose stamp is older than 7 days and whose
    wall the current document does not place here is removed; a wall that returns has
    its stamp removed; a directory with no stamp that no wall claims is stamped at
    first sight.
- **Depends on:** Chunk 01 merged (`client.py`). Phase 2.
- **Deliverables:** `src/arrt_player/logs.py`, `src/arrt_player/client.py`,
  `tests/test_logs.py`, `tests/test_client.py`.
- **Tests (red on the phase-1 head, green after):**
  1. A `pull.unreachable` line from wall A's task carries `wall_id: "A"`; a line from a
     drawing thread of wall B carries `"B"`.
  2. A wall retired 8 days ago: its directory is gone; 6 days: kept; returned: kept and
     unstamped.
  3. With the server down and no readable client document, nothing is removed.
- **Implicit guarantees to keep, each with a test:** an explicit `wall_id` passed in
  `extra` is never overwritten; a process-level line carries no `wall_id`; a wall moved
  between two outputs of this Player is not "left" and keeps its directory; `stop_all`
  stamps nothing; nothing outside `CACHE_DIR` is removed, a symlink is never followed,
  and only names `config.PLAIN_COMPONENT` admits and `config.CACHE_DIR_NAMES` does not
  hold are candidates.
- **Sibling instances:** grep `src/` for `extra=` dicts that pass `wall_id` by hand and
  list which the filter now makes redundant (keep them; list only).
- **Verification ceiling:** `tests/test_logs.py tests/test_client.py tests/test_main.py tests/test_pull.py`, plus ruff and black.
- **Model:** Opus-class, high effort.

### Chunk 08: The unit's watchdog, and the hardening that needs no change of ownership

- **Description:** closes audit-8 F16a (ledger row 54), and what remains of audit-8 F2
  under the owner's "not now" (ledger row 46).
  - **Watchdog:** `Type=notify`, `NotifyAccess=main`, `WatchdogSec=` and
    `WatchdogSignal=SIGTERM`. A new module sends `READY=1` and `WATCHDOG=1` over
    `$NOTIFY_SOCKET` (a datagram socket; no new dependency) and does nothing when the
    variable is absent. `READY=1` is sent in each of the three modes (Arrt, slideshow,
    setup); the pet follows progress (Goals). `WatchdogSec` is set above the worst
    stall the plan knows of (operator's item 12) with the reasoning in the unit's
    comment.
  - **Hardening with no change of ownership or update procedure:** `LockPersonality`,
    `RestrictNamespaces`, `RestrictSUIDSGID`, `RestrictRealtime`, `ProtectClock`,
    `ProtectHostname`, `ProtectKernelLogs`, `ProtectControlGroups`,
    `SystemCallArchitectures=native`, and an empty `CapabilityBoundingSet`. Nothing
    that touches the filesystem layout, `/sys`, the devices or module loading
    (`ProtectSystem` stays `full`; no `PrivateDevices`, `DevicePolicy`,
    `ProtectKernelModules`, `ProtectKernelTunables` or `MemoryDenyWriteExecute`), each
    excluded one named with its reason in the unit's comments. If verify-api shows a
    listed directive could stop the screen, the panel, the DDC bus or mpv, drop it and
    say why.
- **Foreign API:** systemd's notification protocol (`sd_notify(3)`) and unit
  directives (`systemd.service(5)`, `systemd.exec(5)`).
- **Depends on:** Chunk 05 merged (`__main__.py`). Phase 3.
- **Deliverables:** new `src/arrt_player/notify.py`, `src/arrt_player/__main__.py`,
  `deploy/display.service`, `tests/test_deploy_unit.py`, new `tests/test_notify.py`.
- **Must not touch:** `KillSignal`, `TimeoutStopSec`, `UMask`, `SupplementaryGroups`,
  `StartLimitIntervalSec`, `ExecStart`, `User`, `WorkingDirectory`, the memory bounds.
- **Done when:**
  0. verify-api: read `man sd_notify`, `man systemd.service` (`Type=notify`,
     `WatchdogSec`, `NotifyAccess`, `WatchdogSignal`, `TimeoutStartSec`) and
     `man systemd.exec` for each hardening directive; quote the lines relied on.
  1. The tests below are red on the phase-2 head and green.
- **Tests:** a datagram socket in a temp directory as `NOTIFY_SOCKET` receives
  `READY=1` in each mode and `WATCHDOG=1` only after progress; with a stalled
  supervisor no pet arrives; without the variable nothing is sent; the unit carries the
  directives and keeps the kept ones.
- **Implicit guarantees of `Type=simple`, each with a test or a runbook check:** a
  start never fails for want of a message (now it does after `TimeoutStartSec`, so
  every mode must send `READY=1`: tested); `Restart=always` still restarts after a
  watchdog stop (operator's item 13); a SIGTERM from the watchdog takes the orderly
  stop path that closes the art channel (tested through the existing signal handler).
- **Verification ceiling:** `tests/test_notify.py tests/test_deploy_unit.py tests/test_main.py tests/test_slideshow.py`, plus ruff and black.
- **Model:** Opus-class, high effort. A wrong directive or a missing `READY=1` dims a
  wall only at the next deploy.
- **Built as `NotifyAccess=all`, not `main` (2026-11-19, Chunk 08's delegate):**
  `ExecStart` is `uv run`, so the unit's main process is uv and the Player's Python is
  its child; under `main` systemd would drop the Player's `READY=1` and fail each start
  at `TimeoutStartSec`. Every process in the cgroup is the Player's own, and mpv's
  environment carries no `NOTIFY_SOCKET` (`deploy/display.service`'s comment;
  `tests/test_deploy_unit.py`).

### Chunk 09: The settings page: the Frame's address, the server's name, a save that says what it saved

- **Description:** closes audit-5 F7b and F7a (ledger rows 69, 68), #46, review
  observations O-3 and O-10, and the second verify pass's O-3.
  - **F7b:** `TV_ADDRESS` becomes a key the page manages (Goals). The page says that
    pointing it at another television asks that set to pair. If the settings file's
    layout must change for it, the version moves and the report states what the
    previous release does with the new file (wave-1 Chunk 08 found an older Player
    refuses a newer layout with exit 2) and the rollback step needed, for Chunk 12.
  - **F7a:** once the Player has a cached client document, the page shows the server's
    name for this Player read-only, and says where to rename it (the server's Players
    screen); before pairing it stays editable.
  - **#46:** the save button reads as the whole page's; saving slideshow fields while
    the mode is Arrt says the Player stays in Arrt mode; the Slideshow section says
    plainly when no cookie is stored.
  - **O-3:** `CACHE_DIR_NAMES`'s comment says "every name this Player keeps directly
    under CACHE_DIR" and omits `.labels` and `.client.etag`. Say what the set is for (the
    names a wall id may not take; dotted names are excluded by `PLAIN_COMPONENT`), and
    add a test that every file and directory name the Player keeps at the cache root is
    either in the set or not a possible wall id, derived from the constants, not copied.
  - **O-10:** one constant for `UI_ALLOWED_HOSTS` and one host-normalising and IP-parsing
    helper in `src/arrt_player/config.py`, used by `src/arrt_player/settings_ui/server.py`.
  - **Second verify O-3:** `RefusedHosts`' "said once more when quiet" happens only when
    a later refusal arrives. Make the class's docstring relational and test the
    `_others` window and its forget; report the observability row's wording for Chunk 12.
- **Depends on:** Chunk 05 merged (`config.py`, `.env.example`). Phase 3.
- **Deliverables:** `src/arrt_player/settings_ui/server.py`,
  `src/arrt_player/settings_ui/store.py`, `src/arrt_player/config.py`, `.env.example`,
  `tests/test_settings_ui.py`, `tests/test_settings_ui_style.py`, `tests/test_config.py`.
- **Must not touch:** `src/arrt_player/settings_ui/auth.py` (the throttle stands as
  ruled), `src/arrt_player/client.py` (read `player_name`, do not change it).
- **Tests (red on the phase-2 head, green after):** `TV_ADDRESS` saved on the page is
  what the next `load()` gives the Frame; the name field is read-only with a cached
  client document naming the Player, editable without one; a save of slideshow fields
  in Arrt mode says the mode did not change; the cache-root names test; the `_others`
  window test.
- **Implicit guarantees to keep, each with a test:** a value in `.env` still wins over
  the page where it does today, and the page shows it pinned; `CLIENT_TOKEN` and
  `REDDIT_SESSION` are never shown back; a file the previous release wrote is still
  read.
- **Visual change:** yes. The coordinator adds an operator-verification entry: the
  page's Frame field and its pairing note, the read-only name, and #46's sentences, read
  at the Pi.
- **Verification ceiling:** `tests/test_settings_ui.py tests/test_settings_ui_style.py tests/test_config.py tests/test_main.py`, plus ruff and black.
- **Model:** Opus-class, high effort.

### Chunk 10: The contract pin moves to the server's wave-2 tag

> **Moved, 2026-11-19 (the coordinator): moved to the next Player plan, after
> `contract-v1.2.0` exists** (frontmatter `dropped:`, as its Depends on allows).
> The requirement stands and nothing of it is built on this branch: the pin is still
> the one wave 1 left. The next plan carries it with Chunk 11 and one defect found in
> integration: the heartbeat's `FrameIdentity` reads the set's identity once and never
> again, so a different set answering at the same address without a restart is
> reported under the first set's identity while the wall's store follows the new one
> (`client.FrameIdentity.refresh`; the wall reads on every connection,
> `displays/frame.py` `_know_the_set`).

- **Description:** closes audit-3 § Contract skew and § Contract conformance, audit-7
  M6, audit-1 M1 and M8 (ledger rows 36, 37, 38, 80, 87, 88, 90), the Player's half of
  the shared pairing labels (row 64, lesson 4), and the manifest's size bound if the
  tag states one (row 47's residual).
  - **What the pin gains at each tag.** At `contract-v1.1.0` (cut): heartbeat minor 5
    (`capabilities.media`, video). The Player writes minor 4, which stays valid; nothing
    in `src/` changes, and the suite's double validates against the newer schema. At a
    `contract-v1.1.1` patch (if cut, R6's `_about`): nothing to change. At the server's
    wave-2 minor (`contract-v1.2.0` or whatever it is named): whatever of R1–R9 it
    carries.
  - **What must change, per request the tag answers:** R1/R2: `tests/server_double.py`
    answers from the contract's table and vector instead of its hand-copied rules, and a
    test parametrised over the table's rows replays each row against the double and
    against the Pull's reaction (pull.py changes only where a row disagrees with what it
    does; each disagreement is reported). R4: each new must-refuse fixture is refused by
    the matching parser, with a rule in `_REFUSED_BECAUSE`. R6: a test holds
    `config.DEFAULT_MAT_BOTTOM_WEIGHT` to the contract's named default. R7: a test holds
    the page's field labels to the vocabulary file (and its status words, if present).
    R8: nothing; the Player keeps writing the fields. R9: the client parser and the
    client-heartbeat builder are run over the new fixtures. A stated manifest bound
    (R1): the pull caps the manifest read at it.
  - `uv.lock` moves `arrt-contract` only.
- **Depends on:** the server's wave-2 contract tag, and phase 3 merged
  (`tests/test_settings_ui.py`, `tests/test_config.py`). Phase 4. **If the tag is not
  cut when phase 3 is integrated**, the coordinator either waits, or drops this chunk and
  Chunk 11 (`dropped:` with the reason) to a wave 3 and proceeds to Chunk 12.
- **Deliverables:** `pyproject.toml`, `uv.lock`, `tests/server_double.py`,
  `tests/test_player_contract.py`, `tests/test_pull.py`, `src/arrt_player/pull.py`
  (only where a row disagrees, or for the manifest cap), and, only for new must-refuse
  fixtures, `src/arrt_player/manifest.py`, `src/arrt_player/client.py`,
  `src/arrt_player/label_rule.py`.
- **Tests (red at the new tag before the change, green after):** the table replay; the
  conditional-GET vector replay; each new must-refuse fixture; the bottom-weight and
  vocabulary quotes (in `tests/test_config.py` and `tests/test_settings_ui.py`, which this
  chunk may extend by those tests only).
- **Acceptance:** `uv lock` changes no resolved version but `arrt-contract`'s (the diff
  shown); the double has no transport rule of its own left (grep its docstring's list
  against the table, each item ticked off).
- **Rewritten tests:** every assertion of the double's hand-copied behaviour that the
  table replaces is listed with its old form and the row that now holds it.
- **Verification ceiling:** `tests/test_player_contract.py tests/test_pull.py tests/test_client.py tests/test_manifest.py tests/test_label_rule.py tests/test_heartbeat.py tests/test_config.py tests/test_settings_ui.py tests/test_plane_isolation.py tests/test_corpus_contract.py`, plus ruff and black. No `uv lock --upgrade`.
- **Model:** Opus-class, high effort. A row that disagrees with the Pull is a judgment.

### Chunk 11: The Player tells the server where its settings page is

> **Moved, 2026-11-19 (the coordinator): moved to the next Player plan, after
> `contract-v1.2.0` exists**, with Chunk 10. Nothing of it is built on this branch:
> the client heartbeat carries no `settings_url`.

- **Description:** closes audit-5 F6c (ledger row 66), by the decision in Goals, once the
  tag carries R5's `settings_url`.
  - The client heartbeat carries the page's address: the first name in
    `UI_ALLOWED_HOSTS` if one is listed, else the local address of the pull's own
    connection to the server, with `UI_PORT`, as `http://…/`. Absent when `UI_BIND` is
    loopback. A change of address alone sends the heartbeat again.
  - The wire name is the contract's; the value is the Player's. The double validates it
    against the schema, so a misspelt key fails here.
- **Foreign API:** aiohttp's connection socket address (`ClientResponse.connection`,
  `transport.get_extra_info("sockname")`).
- **Depends on:** Chunk 10 merged. Phase 4.
- **Deliverables:** `src/arrt_player/client.py`, `src/arrt_player/pull.py`
  (`ClientPull` only), `src/arrt_player/__main__.py` (wiring only), `tests/test_client.py`,
  `tests/test_pull.py`.
- **Done when:**
  0. verify-api: read aiohttp's `client_reqrep.py` and `connector.py` for how a
     response's connection and transport are reached and when they are released; record
     where the address can be read before the connection returns to the pool.
  1. The tests below are red at Chunk 10's head and green.
- **Tests:** the heartbeat carries the listed name when `UI_ALLOWED_HOSTS` names one,
  the connection's local address otherwise, and no key with `UI_BIND=127.0.0.1`; an
  address change alone resends; the document validates against `client-heartbeat.v1`.
- **Implicit guarantees to keep:** the client heartbeat is not sent more often than
  today when nothing changes; slideshow and setup modes send nothing.
- **Verification ceiling:** `tests/test_client.py tests/test_pull.py tests/test_player_contract.py tests/test_main.py`, plus ruff and black.
- **Model:** Opus-class, medium effort.

### Chunk 12: The records

- **Description:** applies every chunk's records owed to the Player's
  `.prawduct/artifacts/`, `deploy/README.md` and the runbook, and settles the records
  observations of the wave-1 review.
  - Each chunk's records-owed list, ticked off item by item against its report
    (learnings: "treat the list as a checklist").
  - The owner's two rulings where the records describe them: `security-model.md` § The
    deploy unit says mpv runs as the service account and can read the settings, as the
    owner's accepted state (2026-11-19, "not now"); `architecture.md` and
    `data-model.md` say one label panel per Player by ruling, a label being an output a
    Player has zero or more of.
  - `deploy/README.md`: the unfrozen `uv run` at the tool line (the usage lines' fix in
    Chunk 04); "asks it before every selection" (O-13) now that every write is gated;
    the file outputs' heading if Chunk 05 asks; the watchdog and the new directives
    (Chunk 08).
  - The runbook: steps renumbered so they read 7, 7a, 7b, 7c (O-18); the checks
    Chunks 06, 08 and 09 ask for (the store migration's way back, `READY=1` and the
    watchdog, the settings file's version); the release step that moves the server's
    pinned Player commit (§ The test of a real Player against a real server).
  - `data-model.md`'s `.client.etag` row says the ETag is sent only while the document
    parses (O-17); the label cache's lifecycle (O-14); the binding's retirement (#18);
    the retired-wall stamp (#5).
  - `.prawduct/changes/audit-wave-1.md` (unreleased): step 7b checks `*_SECONDS` only,
    and `deploy/README.md` § Two things that can stop an upgrade covers the rest (O-15);
    selection needs art mode on, the other writes art mode or dark (O-16, folded in
    because the file is open).
- **Type:** cumulative-final
- **Depends on:** Chunks 01–11 merged, or 10 and 11 dropped. Serial, last.
- **Deliverables:** `.prawduct/artifacts/architecture.md`, `.prawduct/artifacts/data-model.md`,
  `.prawduct/artifacts/security-model.md`, `.prawduct/artifacts/observability-strategy.md`,
  `.prawduct/artifacts/nonfunctional-requirements.md`, `.prawduct/artifacts/operational-spec.md`,
  `.prawduct/artifacts/samsung-tv-state-findings.md`, `.prawduct/artifacts/hdmi-output-findings.md`,
  `deploy/README.md`, `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`,
  `.prawduct/changes/audit-wave-1.md`.
- **Acceptance:** each records-owed item ticked off against its chunk's report; every
  renamed event (Chunk 03) found by a whole-repo grep and classified;
  `tests/test_artifact_citations.py` and `tests/test_norm_index.py` pass.
- **Verification ceiling:** `tests/test_artifact_citations.py tests/test_norm_index.py tests/test_repository.py tests/test_deploy_unit.py`, plus ruff and black. The coordinator runs the whole suite after it.
- **Model:** Sonnet-class, high effort.

## Operator's checklist (the measurements no agent may take)

Each needs the owner at the set, the Pi or a monitor. Each names what to do, what to
look for, and which code decision its answer changes, so the measurement can be acted
on in one step. Tools named here are run by the owner, never by a delegate.

1. **The power report while a programme shows** (the most important). With the set
   showing a programme, read `PowerState` from the set's REST description on port 8002
   (`tools/power_probe.py` without `--click`). Look for `on` or `standby`. If
   `standby`, the wave-1 gate reads a lit set as dark and admits every write but
   selection during television: the gate in `src/arrt_player/tv/client.py` must change
   to hold every write whenever art mode is off (a follow-up chunk). If `on`, nothing
   changes.
2. **Whether upload, delete, the slideshow switch or brightness disturb a programme**
   (ledger rows 3, 31). With a programme showing and the Player stopped, run
   `tools/tv_api_check.py` once and `tools/tv_delete.py` on its own image, and watch the
   picture and sound. If none disturbs it, the S1 hold can be relaxed for those verbs
   (uploads stop waiting for art mode; a follow-up decision); if any does, the hold
   stays as built.
3. **An upload in flight when the set leaves art mode.** Start an upload of a 4K image
   from art mode and switch to a programme during it. Look for the upload's content id
   in the set's list afterwards and for no selection. If the set aborts it, the Frame's
   retry must treat it as held, not failed.
4. **The pairing prompt (#2).** Keep a copy of the token file, empty it, put a programme
   on, start the Player. Look for a pairing prompt over the programme. If it appears:
   build "the Player never pairs" (refuse to connect on an empty token file and pair by
   a hand-run step), which changes the first-install runbook. If not, close #2 with the
   observation. Restore the token file after.
5. **DDC brightness on a monitor switched to another input** (row 32). Switch an HDMI
   monitor the Player drives to another input and wait one brightness interval. Look
   for a brightness change on the other input and for `screen.brightness_*` events. If
   writes reach it, gate DDC writes on the monitor's input source being this one.
6. **Peak memory with several walls composing at once** (row 33). With two or more walls
   changing feeds at once, read `systemctl show -p MemoryPeak display.service` and
   watch for throttling against `MemoryHigh`. If the peak nears 60% of RAM, serialise
   composition across walls.
7. **A 7680 px q95 master over the deployed Wi-Fi** (row 34). Hang a theme with such a
   master and read the pull's journal lines for that work. Look for `pull.media_failing`
   from 30 s of silence. If it appears, the master's `sock_read` timeout becomes a
   setting or longer.
8. **ext4 after power loss** (row 35). Read `findmnt -no OPTIONS /` for the cache's
   filesystem (`data=ordered`, `auto_da_alloc`). No code decision: wave 1's fsync and
   repair hold either way; the answer is recorded in `platform-and-dependency-findings.md`.
9. **The Pi's facts** (audit-8 § Could not determine, row 60). `stat -c '%a %U'` on the
   checkout's `.env` (expect `600 tvpi`), the `UI_BIND` in use, whether `SERVER_URL` is
   https, and `free -h`. If `.env` is wider than 600, the runbook gains a `chmod`; RAM
   under 8 GB makes item 6 urgent.
10. **The hardened unit, under systemd** (wave-1 Chunk 08's runbook steps 7b, 7c, 9–11,
    14a; and Chunk 08 of this wave). After installing the unit: the cgroup's
    `memory.max` is set; the screen, the label (SPI, GPIO), the DDC bus (i2c) and mpv
    all draw; `.env` names no path under `/home`, `/root` or `/run/user`;
    `systemd-analyze security display.service` lists the new directives. Any directive
    that stops a device is reverted by name in `deploy/display.service`.
11. **A retired HDMI wall's black frame** (wave-1 Chunk 05's checks). Unassign a wall from
    an HDMI screen. Look for black, not the text console, on the vc4 driver; that the
    4K black frame (about 33 MB) allocates; and that a screen unplugged when retired
    comes back black. If the console shows or the allocation fails, the blank changes
    to one buffer allocated once, or to disabling the plane with the console accepted.
12. **The loop stall at the first pass after a start** (wave-1 Chunk 04's integrity
    decode runs on the loop thread). With many composed pictures cached, restart the
    Player and read the gap between the first journal lines of each wall and the
    label's first draw. If it exceeds a few seconds, the decode moves to a thread; the
    number sets Chunk 08's `WatchdogSec` floor.
13. **The watchdog** (after Chunk 08 deploys). Check that the unit reaches `active` in
    Arrt mode, slideshow mode and setup mode; then `kill -STOP` the Player's main
    process and look for a watchdog stop and a restart within `WatchdogSec` plus
    `RestartSec`, with the art channel closed. If a mode never reaches `active`, that
    mode's `READY=1` is missing.

## Governance checkpoints

- **Dispatch:** worktrees `arrt-player/.claude/worktrees/audit2-NN` on branches
  `audit2/NN`, cut from `fix/audit-wave-2` at the plan commit (phase 1) or at the phase's
  integrated head (later phases), each with `.prawduct/.delegate-brief.md`.
  `mk_player_brief.py` names wave 1's plan and must be pointed at this one.
- **Commit and merge:** § Commit subjects. The coordinator merges each branch with
  `merge(audit-wave-2): … (Chunk NN)` and reviews each chunk on the integrated head.
- **Reviews:** Chunk 01 and Chunk 03 get a `final` review before phase 2 is dispatched,
  because 05, 06 and 07 build on them. Chunk 12's `cumulative` makes the branch PR-ready.
- **The heavy run:** the whole suite after each phase and after Chunk 12.
- **At the end of the wave (the coordinator, a repository setting, after the PR
  merges):** branch protection on `develop` and `main` requiring the checks
  **"The suite"**, **"The typesetting"** and **"Lint and format"** (the job names in
  `.github/workflows/suites.yml`, as reported on #50). The owner's condition (a pool
  fallback) does not bind here, because the Player's CI does not use the pool.
- **Coordinator's edits:** the norm-index rows' test ids from each report; the
  only-channel row as a pointer after Arrt's amendment (Goals); the change fragment
  `.prawduct/changes/audit-wave-2.md`; an operator-verification entry for Chunk 09.
- **Owner checks that are not tests:** § Operator's checklist.
