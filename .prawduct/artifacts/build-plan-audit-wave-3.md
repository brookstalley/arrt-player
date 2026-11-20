---
artifact: build-plan
version: 2
scope: audit-wave-3
branch: fix/audit-wave-3
base: develop
working-branches: []
depends_on:
  - artifact: architecture
  - artifact: security-model
  - artifact: data-model
  - artifact: observability-strategy
  - artifact: operational-spec
  - artifact: samsung-tv-state-findings
  - artifact: platform-and-dependency-findings
  - artifact: project-preferences
  - artifact: build-plan-audit-wave-2
governed_by:
  - artifact: project-preferences
    dispositions:
      - "The television belongs to whoever is using it (Arrt's NFR) → conforms: Chunk 04 narrows what the Player removes and adds one read (the set's listing with each picture's date, the same request `listed_content_ids` sends today); every removal stays the gated `TvClient.remove`, and no verb is added outside `TvClient.WRITES`. Chunk 03's identity read is the existing REST read, which opens no art channel."
      - "A found problem is fixed, not filed, unless it is (orthogonal AND needs design) or medium+ → conforms: every finding the wave-2 records carried, and every wave-2 review observation left open, is a chunk item, a decision below, an operator's check, waits for the contract's 1.3.0 tag, or is dropped by name as settled (§ Dropped as already settled)."
      - "Every lint waiver says why → conforms."
      - "The TV pairing token is never tracked → conforms: no chunk touches the token file."
      - "The Player never requires the server to be reachable → conforms: Chunk 01's manifest cap keeps the last good feed past the bound; Chunk 03's `settings_url` is an annotation, absent when unknown; nothing new waits on the server."
      - "The e-paper label is legible at standing distance → inapplicable because no chunk changes the label's layout or type; Chunk 06 adds a field to two panel log lines."
      - "The manifest and its media are the only channel from the server; only pull.py opens an HTTP client → conforms: Chunk 03 reads the local address of the pull's own connection and opens nothing; Chunk 08's page reads the supervisor's own state in the process, never the server."
      - "A display device renders its own label, and the label travels as metadata → conforms."
      - "The heartbeat's instant is reported under the key the contract names → conforms."
      - "Derived artifacts are regenerated, never transported → conforms."
      - "Coverage expectations; Testing strategies → conforms: Chunk 04's double gains the set's per-picture date, which the real set returns (`platform-and-dependency-findings.md` § `upload()` reports failure on uploads that succeeded), so the double does not invent a field."
      - "The Player is stopped with SIGTERM, never SIGKILL → conforms: no chunk touches the unit."
      - "A change to the contract is a pull request and a new tag on Arrt; the installed package is never patched → conforms: Chunks 01 and 07 move the pin to released tags only; Chunk 07 waits for `contract-v1.3.0`; what the Player needs before that tag is in § Requests to the server."
      - "Everything the Player writes is its own account's alone → conforms: Chunk 05's one writer keeps the settings file's 0600 and writes under the unit's UMask."
      - "Every log line carries its event as a field → conforms: every new event is named in its chunk's report and added to the pinned lists in `tests/test_client.py` by the coordinator at the merge (§ Verification strategy)."
      - "Tests live in `tests/` → conforms."
      - "The only-channel norm's text (audit-1 S6) → unchanged from wave 2: the Player's index row becomes a pointer once Arrt's amendment lands, and that amendment is held for the owner in Arrt (its wave-2 question 1(d)). No chunk here edits the row."
      - "Delegation / Delegate verification → unset; the methodology default holds: a delegate runs only the narrowest tests covering its own change."
partition: "Four phases, then two serial chunks. Phase 1: 01 and 02 in parallel (disjoint files). Phase 2, once phase 1 is merged: 03 (after 01) and 04 (after 02) in parallel. Phase 3, once phase 2 is merged: 05 (after 03) and 06 (after 03 and 04) in parallel, on the disjoint lists each names. Phase 4 waits for `contract-v1.3.0` and for phase 3: 07, then 08 serial after it. If the tag is not cut when phase 3 is integrated, 07 and 08 move to the next plan (`dropped:` with the reason) and the plan proceeds. 09 (integration) and 10 (records, cumulative-final) are serial and last. Six delegates in phases 1–3, two in phase 4, each in its own worktree; the coordinator, or an integrator it names, merges each phase and runs the whole suite"
dropped: []
last_validated: 2026-11-20
---

# Build plan: the Player's share of the 2026-11-19 audit, wave 3

## Goals

**Response taken:** proceed. The owner asked for substantially all identified product
issues to be fixed by delegated builders under governance (2026-11-19), and the
coordinator began wave 3 under that instruction on 2026-11-20 with the Player's wave 2
merged (`aeeec7e`, PR #51). Wave 3 is what wave 2 left: its two moved chunks (the
contract pin, the settings page's address), the defect its integration found (the
heartbeat's Frame identity read once), the boundary review's warnings and observations
recorded "for the next Player plan" (R-4, R-5, O-1, O-2, O-3, O-4, O-7), the owner's
ruling on what the Player removes from a television, and the contract's unreleased
1.3.0. Every item was checked against the code at `aeeec7e`; what the code already
settles is listed in § Dropped as already settled.

**Product:** a Player that leaves a household's own pictures on its television, whatever
set it meets (the ruling); a Frame wall whose idea of which set it is on is one state, so
an outage, a reconnect to another set or a store fault cannot leave it half in one
phase and half in another (R-4, O-2); a server that knows where each Player's settings
page is and which set its Frame is now (wave 2's moved chunk 11; the read-once identity);
a Player pinned to the contract the server released, answering its tests from the
contract's own table (wave 2's moved chunk 10); one way to write a file whole (R-5); and,
once 1.3.0 is tagged, a settings page that says the Player's state in the words the
server's Players page uses (audit-5 F4).

**The owner's rulings this plan is built on:**
- **The Player removes from a television only what it recorded uploading** (2026-11-19,
  "during wave 2", recorded in Arrt's `.prawduct/owner-rulings.md`; the owner's words for
  the batch: "Other than that, yes to recommendations"). As recorded there: "**Arrt Player
  removes from a television only the pictures it recorded uploading**, in a later wave.
  Today it clears every uploaded picture it cannot account for on a set (`arrt-player`'s
  `security-model.md` § What it does to the television), so a Frame address that points at
  another television clears that set's uploads, the household's own included. Now that the
  Player's store records which set its bindings were made on, it can keep a record of its
  own uploads per set and leave everything else alone. Not built in wave 2, which ships the
  recorded behaviour unchanged; it goes in the next Arrt Player plan, with what replaces
  the orphan sweep's job of cleaning up after a failed upload. Recorded here because the
  question was asked in this repository's session; the Player's own rulings file takes a
  pointer when that plan is cut." → Chunk 04. The pointer in this repository's
  `.prawduct/owner-rulings.md` is the coordinator's edit at dispatch (§ Governance
  checkpoints).
- **One label panel per Player is the recorded limit, and nothing may close off several
  panels or a label-only Player** (2026-11-19, this repository's `.prawduct/owner-rulings.md`).
  No chunk changes the label's wiring. Chunk 07's writer check holds the contract's cap of
  32 label outputs, which a Player with several panels does not approach; Chunk 08's page
  speaks of the Player, never of "the" panel.

**Architecture:** what must hold.
- Every write to the set goes through `TvClient.WRITES` and its gate; a removal is sent
  only from a store whose recorded identity is the connected set's (wave 2), and now only
  for a picture the store records this Player putting on that set (Chunk 04).
- A wall renders only from its cache. The pull stays the one HTTP client.
- Each wall's fault stays its own.
- The contract pin moves only to a released tag (Chunks 01, 07).
- A persisted format changes forward only, with the previous release's way back stated
  (Chunk 04's store schema 5).

**Constraints:** delegates run nothing under `tools/` (Chunk 06 edits a comment in one
tool and runs none of it), nothing that reaches a server, a television or a panel, and
never the whole suite. The integrated branch's suite runs and every Critic review are the
coordinator's. No chunk number in a shipped comment or docstring (Chunk 06 adds the check).

**Decisions** (each vetoable, each stated where its chunk needs it):
- [DECISION: on the first run of the new Player, the pictures it takes as its own are exactly those its store recorded: every binding with a content id, current or set aside, under the set it was made on. Everything else the set lists is left there, an older Player's unrecorded copies included, and the journal says once per set how many listed pictures it leaves alone and why | the ruling. The one way to remove an older Player's unrecorded copies is a last sweep by the old rule, which is the removal the ruling ends, and it would take the household's pictures added since the last sweep with them. **The cost, stated:** the old sweep ran at every feed adoption, so what it leaves behind is what an older Player put up and never recorded since its last sweep: a copy whose upload was misreported and whose read-back failed, a copy uploaded while the store could not write (O-7), the theme a set that would not say which it was received per outage (wave 2's recorded limit), and every picture under a store the operator deleted (runbook step 6a). **What a household sees:** some Arrt pictures in the set's own uploaded-pictures list that nothing removes, which they can delete there; the operator can remove them with `tools/tv_delete.py`. The owner is told before the deploy (§ Governance checkpoints) | user can veto/override]
- [DECISION: the replacement for the orphan sweep's cleanup after a failed upload is an upload record written before the upload is sent: the set it is sent to (or "not yet known", while the set will not say), and the marker the Player stamps on it (`image_date`, already sent, `src/arrt_player/tv/samsung.py` `SamsungTv._send_upload`). A picture the set lists is the Player's when its id is one the Player recorded on that set and, where a marker was recorded, its `image_date` is that marker; an upload whose id was never learned is resolved by the first listing after it, by marker | the set echoes the marker in its listing (measured once, `platform-and-dependency-findings.md`, the upload whose library returned None "carried the exact `image_date` this request sent"); an id alone means nothing across sets (wave 2), and an id with its marker names one upload. A household picture stamped in the same second as an upload of the Player's on the same set is the residue | user can veto/override]
- [DECISION: an upload whose record cannot be written is not sent | review O-7: today a store that cannot write sends one copy per pass that nothing records, which under the ruling nothing would ever remove. The wall shows the works it has already put on the set, and `store.failed` already says why the theme stops filling | user can veto/override]
- [DECISION: uploads made while the set will not say which it is are recorded on disk with no set, and removed once a set says and its listing shows each one's id with its marker | wave 2's leaving-the-store-in-memory reconciliation (R-2/R-3) relied on those copies being unaccounted for; under the ruling they must be recorded to be removable at all, and recording them in the device's store is what lets an outage keep them. Wave 2's recorded limit (a set that never says gains one theme of copies per outage) becomes: removed once the set ever says | user can veto/override]
- [DECISION: the wall's running time with a feed, and when each work left the feed, are counted and saved on the device's store whichever store the wall's bindings come from | R-4 item 2: today `adopted` records a leaving at the store in memory's count, which starts at 0, and `_save_feed_time` writes it to the device's store and clears leavings that were the device store's. The running time is the wall's, not the set's, and a retirement reads it only from a store that records the set | user can veto/override]
- [DECISION: the client heartbeat's Frame identity is read again once per heartbeat interval (one REST GET a minute, which opens no art channel and presses no key), the last value is kept through a failed read, and a changed value is sent at once and said once | the read-once defect wave 2's integration found (`client.py` `FrameIdentity.refresh`). Taking the identity from the Frame wall's worker instead saves that read only while a wall runs and couples the worker to the heartbeat; a Frame with no wall still needs the client's own read | user can veto/override]
- [DECISION: carried from wave 2, unchanged: the settings page's address is reported in `settings_url` (the first name in `UI_ALLOWED_HOSTS` if one is listed, else the local address of the pull's own connection, with `UI_PORT`), absent when `UI_BIND` is loopback; no pairing code is built | `build-plan-audit-wave-2.md` Goals; `contract-v1.2.0` carries the field | user can veto/override]
- [DECISION: the shared writer keeps every writer's present behaviour, its temp-file names included, and adds no directory `fsync` | R-5 asks for one helper so a durability change reaches every writer at once; making that change in the same chunk would turn a consolidation into a change of behaviour on the SD card. Whether ext4 on the Pi needs it is the operator's item 8 (wave 2's checklist) | user can veto/override]
- [DECISION: of the 28 manifest fixtures 1.3.0 marks `player_must_refuse`, the Player asks the server, before the tag, to mark three `false`: `generated-at-malformed`, `generated-at-not-text` and `work-media-content-type-unknown`, and enforces `staging-repeated` itself | measured at `aeeec7e`: the reader adopts those four and refuses the other 24. The Player does not read `generated_at` or `content_type` (it decodes the bytes and checks their hash), and refusing a feed over a field it never acts on freezes a wall's programming on a writer's slip; an enum a later minor may widen must be tolerated by a reader, or that minor is not additive for it. A repeated staging id is a fault in a field the reader reads. If the tag keeps the split, Chunk 07 enforces all four, which costs only a feed the server's own schema check already refuses | user can veto/override]
- [DECISION: carried from wave 2's Chunk 07, never entered as a decision there: a server outage still counts towards a departed wall's 7 days while the last read client document is held | it removes only walls that document does not place here; counting only while the server confirms would need `ClientPull.fetch` to tell a 304 from a failure. Recorded here so the boundary review can find it | user can veto/override]

**Ruled out for now:** root-owned code and `ProtectSystem=strict` (the owner, "not now",
2026-11-19); a pairing code (wave 2's decision); several label panels or Frames driven
(#23; the label ruling keeps the path open only).

**Not in this wave, and why:** #2 waits for the operator's check (wave 2's checklist,
item 4); #12 and #31 are features, not audit findings; the only-channel norm's pointer row
waits for Arrt's amendment, held for the owner in Arrt; a `file` kind for label outputs
(wave 2 Chunk 05's note) is not requested, because the contract's one kind, `epaper`,
describes what the file label draws for.

**Tradeoffs accepted:**
- The household's pictures stay, and so do an older Player's unrecorded copies (the first
  decision above).
- The marker is second-resolution (the protocol's), so two uploads in one second on one
  set could not be told apart; the Player uploads one image at a time.
- A set that never says which it is still receives one theme per outage; those copies are
  now recorded and removed once it says, not left for good.
- `settings_url` names the address the pull reached the server from, which on a Pi with
  two interfaces may not be the one a person's phone reaches; `UI_ALLOWED_HOSTS` overrides it.

**Decisions made mid-build:** (appended by the coordinator from the chunk reports)

**Level:** Medium. Every chunk in phases 1–3 has a red test buildable against the doubles
today. What stays unmeasured is the set's answer (§ Operator's checklist: whether every
firmware echoes the marker; what the set's own phone uploads stamp), and 1.3.0's contents
until the tag (Chunks 07, 08). What would raise it: the operator's items 14 and 15, and the
server's answer to § Requests.

## Commit subjects, and how a chunk is attributed

As in wave 2 (`build-plan-audit-wave-2.md` § Commit subjects), with this plan's scope:
`prawduct-hook plan status` and the boundary review count a chunk as built only by a
**first-parent** commit on `fix/audit-wave-3` whose subject carries the scope and the
chunk, `<type>(audit-wave-3): <what> (Chunk NN)`.

- **The coordinator's merge of a delegate branch is the commit that counts:**
  `git merge --no-ff audit3/NN -m "merge(audit-wave-3): <what> (Chunk NN)"`.
- A delegate's own commits use `fix(audit-wave-3): <what> (Chunk NN)` (or `test(…)`,
  `refactor(…)`), and carry no attribution trailer of any kind.
- A fix-round commit names every chunk it serves: `fix(audit-wave-3): <what> (Chunk 03) (Chunk 04)`.
  The id is followed directly by `)`.

Every brief quotes this section.

## Verification strategy

Each chunk proves itself with a test that is red at `aeeec7e` (or at the phase's
integrated head, for phase 2 onward) and green after, shown as the failing output or as
the re-break that turns it red again (learnings: "When you write a check, prove it can
fail"). A guarding test that was already green says so. Each delegate's ceiling is the
run its chunk names, and nothing wider:

    testament run --json=summary -- <its test paths>    # else: uv run --group raster pytest <its test paths>
    uv run ruff check <its paths> && uv run black --check <its paths>

Never the whole suite, never anything under `tools/`. The coordinator runs the whole suite
(`testament run --json`) on the integrated head after each phase and after Chunk 10.

**Every brief carries these, from waves 1 and 2:**
1. **Attribution:** § Commit subjects.
2. **Implicit guarantees:** a chunk that replaces or consolidates a mechanism lists what
   the old one guaranteed without anyone writing it down, each with a test. Each chunk
   names the ones the plan could see; the delegate adds any it finds.
3. **Sibling instances:** a chunk that fixes one instance of a shape greps its files and
   their neighbours for the rest, and fixes or lists each.
4. **Shared values:** a value the server and the Player both state has one owner, the
   contract, and the Player quotes it with a test that fails if they diverge.
5. **Rewritten tests:** an existing assertion that changes is listed with its old form and
   why.
6. **New events:** a chunk that adds an event names it and its scope (a wall's, a label's,
   one that names its wall, the process's) in its report. The pinned lists in
   `tests/test_client.py` (`WALL_EVENTS`, `LABEL_EVENTS`, `NAMES_ITS_WALL`,
   `PROCESS_EVENTS`) are the coordinator's at each merge, because two chunks of one phase
   can add events (wave 2: Chunk 08 was red on the merged head for this).
7. **No chunk numbers** in a comment or docstring the change ships (Chunk 06's check
   refuses them from phase 3 on).

**Every report returns:** the red-then-green evidence; the records owed (each sentence in
a `.prawduct/artifacts/` file, `deploy/README.md` or the runbook that the change falsifies,
with its replacement), for Chunk 10; the norm-index test ids it adds; new events (item 6);
and any file outside its boundary that it needed and did not touch.

**Files no chunk edits:** `tests/conftest.py`, `tests/test_wall.py`, `tests/test_majors.py`,
`src/arrt_player/wall.py`, `.prawduct/project-state.yaml`, `.prawduct/owner-rulings.md`,
`.prawduct/artifacts/project-preferences.md` (the coordinator's), and everything under
`tools/` except Chunk 06's comment edit. A chunk that cannot pass without one of them stops
and reports.

## Requests to the server (for its wave-3 planner, through the coordinator)

What the Player needs from `contract-v1.3.0` before it is cut. None blocks phases 1–3.

- **Q1. Three fixtures `player_must_refuse: false`:** `manifest.v2/invalid/generated-at-malformed.json`,
  `generated-at-not-text.json` and `work-media-content-type-unknown.json` (decision above;
  Arrt's `player-contract.md` § What a Player does at 1.3.0 asks the Player's coordinator to
  confirm the split). The other 25 the Player enforces, 24 of them already.
- **Q2. A word for a refused token on a Player's own page**, if the server wants one: the
  five `player_states` are the server's judgement, and a Player whose token the server
  refuses (`401`) is a state the Player sees and the server cannot attribute to a Player.
  Chunk 08 keeps its own sentence for it if the tag carries none.
- **Q3. The real-Player pin:** Arrt's `arrt/tests/system/player-ref.txt` moves to this
  branch's merged head once it merges, and `player_e2e` runs once on the pool against it
  (the coordinator's, in Arrt).

## Build Chunks

### Chunk 01: The pin moves to `contract-v1.2.0`, and the double answers from the contract's table

- **Description:** closes wave 2's moved Chunk 10 (audit-3 § Contract skew and § Contract
  conformance, audit-7 M6, audit-1 M1 and M8; wave-2 ledger rows 36, 37, 38, 47, 64, 80, 87,
  88, 90; L11).
  - The pin moves from `contract-v1.0.0` to `contract-v1.2.0` (skipping 1.1.0, whose
    heartbeat minor 5 the Player does not write; it goes on writing minor 4, which stays
    valid).
  - `tests/server_double.py` answers each route from `arrt_contract.transport()` rather
    than its hand-copied rules, and a new test replays every row and every
    `conditional_get.cases` entry against the double and against the Player's reaction
    (the Pull, `ClientPull`, `LabelPull`). Where a row's reaction disagrees with the
    Player's, the Player changes, and each disagreement is reported.
  - The manifest read is capped at its route's `max_bytes` (16 MiB), keeping the last good
    feed past it, as the client and label documents already are at theirs (1 MiB).
  - Shared values quoted against the package, each by a test: the manifest cap and
    `pull.DOCUMENT_MAX_BYTES` against `transport()`'s `max_bytes`;
    `config.DEFAULT_MAT_BOTTOM_WEIGHT` against `vectors/mat-geometry.json`'s
    `defaults.bottom_weight`; `settings_ui.server.PAIRING_LABELS` against `vocabulary()`'s
    `pairing`.
  - The parsers run over `released()`'s valid fixtures of the majors they read, so the
    Player is held to what an older server sent.
- **Found at HEAD (`aeeec7e`):** `pyproject.toml` pins `contract-v1.0.0` in
  `[tool.uv.sources]`, and `uv.lock` resolves it to `c5e2d5e`: the pin is two releases
  behind, not at 1.2.0. `tests/server_double.py` states eight transport rules in its
  docstring and imports no `transport()`. `src/arrt_player/pull.py` reads the manifest
  whole (`Pull._walk`, `response.read()`), and the client and label documents through
  `_read_capped` at `DOCUMENT_MAX_BYTES`, 1 MiB, which equals 1.2.0's `max_bytes` for both.
  `tests/test_player_contract.py`'s `_REFUSED_BECAUSE` has no entry for 1.2.0's
  `media-url-empty.json`, so the test that every must-refuse fixture has a stated reason
  goes red at the new pin; measured with the package at Arrt's HEAD, the reader already
  refuses it ("work w-dali-1 carries no media with a sha256 and a url"), and
  `parse_client_document` already refuses 1.2.0's one new invalid client document
  (`several-labels-one-without-an-output.json`). `config.DEFAULT_MAT_BOTTOM_WEIGHT` is
  1.15, as 1.2.0's vector default is; `PAIRING_LABELS` is "Server address" and "Token", as
  1.2.0's `pairing` is. **One disagreement, read and not run:** a client heartbeat the
  server refuses with a `4xx` is sent again on every supervisor cycle
  (`src/arrt_player/client.py` `Supervisor._report_outputs` leaves `_reported_outputs`
  and `_reported_at` unchanged on a refusal, so the report stays due), where 1.2.0's `400`
  rows say "send again when the report changes". The wall heartbeat already holds a `4xx`
  report (`Pull._heard`).
- **Depends on:** none (phase 1).
- **Deliverables:** `pyproject.toml`, `uv.lock` (`arrt-contract` only), `tests/server_double.py`,
  `tests/test_player_contract.py`, new `tests/test_transport_table.py`, `tests/test_pull.py`,
  `src/arrt_player/pull.py` (the manifest cap, and only where a row disagrees),
  `src/arrt_player/client.py` (`Supervisor._report_outputs` only, for the `4xx` row),
  `tests/test_client.py` (that row's test only).
- **Acceptance:**
  1. A server that answers a manifest past 16 MiB: the wall goes on showing its cached
     feed, the journal says once that the document ran past the bound, and the next poll
     asks again (red: today the whole body is read and adopted).
  2. A server that refuses the client heartbeat with `400`: it receives that report once,
     and again only when an output changes or is plugged in (red, if the reading above
     holds; the delegate says which).
  3. Every row of `transport()` and every conditional-GET case, replayed: the double
     answers it and the Player reacts as its `player` column says.
  4. The three quotes and the `released()` run are green, and each fails when its value
     is changed by one.
  5. `uv lock` moves no resolved version but `arrt-contract`'s (the diff shown).
- **Implicit guarantees to keep, each with a test:** an ETag is never stored without the
  document it describes; a feed is adopted only once every master is answered for; a `304`
  keeps the cached document; the double still validates both heartbeats against their
  schemas, so a Player writing a bad one still fails here.
- **Rewritten tests:** every assertion of the double's hand-copied behaviour that a row now
  holds is listed with its old form and the row.
- **Verification ceiling:** `tests/test_player_contract.py tests/test_transport_table.py tests/test_pull.py tests/test_client.py tests/test_manifest.py tests/test_label_rule.py tests/test_heartbeat.py tests/test_heartbeat_wiring.py tests/test_plane_isolation.py tests/test_corpus_contract.py tests/test_compose.py tests/test_running_without_hardware.py`, plus ruff and black. No `uv lock --upgrade`.
- **Model:** Opus-class, high effort. A row that disagrees with the Pull is a judgment, and
  the double is the frame every other chunk's tests stand on.

### Chunk 02: One model of which set the wall is on

- **Description:** closes the wave-2 boundary review's R-4 (its item 2 and the class) and
  observations O-1 and O-2 (`rev-20261120T002012Z-647a4891`; R-4's item 2 confirmed open by
  its verify pass, `rev-20261120T004725Z-231f51d0`, O-1).
  - **R-4:** the wall's phase with respect to the set (not yet known on this connection;
    identified; withholding its identity, within patience; carrying on from the store in
    memory) becomes one state with its transitions in one place, from which `_on`,
    `_scratch`, `_state`, the patience and its two episodes are read, not rebuilt. Every
    combination the code reaches today that nothing intends is either unreachable by
    construction or pinned by a test.
  - **R-4 item 2:** by the feed-time decision in Goals.
  - **O-1:** `_may_remove` defends what its docstring says, with each clause reachable or
    removed (learnings: "When a mutation survives, find why").
  - **O-2:** a refused selection whose reconnect reaches a different set looks the work's
    binding up again in the store now in use, rather than judging the old set's id
    against the new set's list.
- **Found at HEAD (`aeeec7e`):** in `src/arrt_player/displays/frame.py`, `FrameDisplay`
  rebuilds the phase from `_on`, `_scratch`, `_store`/`_state`, `_unidentified_since`,
  `_identity_retry`, `_unidentified_reason`, the `_unidentified` and
  `_going_on_unidentified` episodes, `_ended_unidentified` and `_left_on_the_wall`, moved by
  `_know_the_set`, `_go_on_unidentified`, `_forget_the_set`, `_the_connection_may_have_gone`,
  `_leave_the_scratch_store`, `_connected`, `went_away`, `answering` and `close`. R-4's items
  1 and 3 were fixed at `82fdea3` (`_forget_the_set` restarts the patience and ends the
  episode; `_know_the_set` says the change and owes its cleanup before `forget_the_wall`,
  pinned by `tests/test_bindings.py::TestASetThatWillNotSayWhichItIs::test_a_store_fault_after_the_set_changed_loses_neither_the_line_nor_the_cleanup`). Item 2 is open: `adopted` takes `_left_at[...]` from
  `self._state.feed_seconds`, the store in memory's count while it is in use;
  `_count_feed_time` and `_save_feed_time` act on `self._state`; `_feed_time_saved_at` is
  taken from the device's store at construction and compared with whichever store `_state`
  is. O-1: `_may_remove` is `self._on is not None and self._state.tv_identity == self._on`,
  and `_on` is set only after `belongs_to` recorded that identity and the store in memory
  was left, so the second clause cannot be false. O-2: `_rebind_or_reraise` calls
  `_connected()`, which may meet another set, then tests the old set's `refused` id
  against the new set's `listed_content_ids()`.
- **Depends on:** none (phase 1).
- **Deliverables:** `src/arrt_player/displays/frame.py`, `src/arrt_player/state/store.py`
  (only if the feed-time decision needs a store method), `tests/test_frame_wall.py`,
  `tests/test_bindings.py`, `tests/test_selection_observers.py`.
- **Must not touch:** `src/arrt_player/tv/client.py`, `src/arrt_player/tv/samsung.py`,
  `tests/fake_tv.py` beyond an armed behaviour (no changed default).
- **Acceptance:**
  1. A wall that ran from the store in memory for an hour and then met its set: a work that
     left the feed during that hour retires 14 days of running time after it left, not
     after the hour's count is lost or misplaced (red today: the leaving is recorded at the
     store in memory's count and written to the device's store).
  2. A refused selection whose reconnect reaches another set: the work is shown there
     from that set's own binding when it has one, or uploaded once when it has none; no
     `binding.orphaned` line names an id the new set never refused, and the wall does not
     back off (red today: one of the two).
  3. Every reachable combination of the phase's parts that R-4 lists, driven by a sequence
     of outages, refusals and identity reads, is one of the four phases (a test over the
     sequences, or a property; the delegate says which).
  4. `_may_remove`: each clause it keeps fails a test when deleted.
- **Implicit guarantees to keep, each with a test (a consolidation):** the reads and writes
  each scenario of a pass sends, in order, are unchanged
  (`tests/test_frame_wall.py::test_what_reaches_the_set_in_each_pass_is_unchanged`, whose
  scenarios are not rewritten); an outage restarts the patience; leaving the store in
  memory owes a reconciliation; the picture on the wall is never removed before another is
  selected; no binding is used on a connection before the set has said which it is; a
  store fault after `belongs_to` loses neither the line nor the cleanup; the store in
  memory removes nothing.
- **Verification ceiling:** `tests/test_frame_wall.py tests/test_bindings.py tests/test_selection_observers.py tests/test_samsung_client.py tests/test_display_state.py tests/test_main.py`, plus ruff and black.
- **Model:** Opus-class, high effort. A state-model rewrite under nine call sites, with a
  household's television on the other end of every transition.

### Chunk 03: The client heartbeat says where the page is, and which set the Frame is now

- **Description:** closes wave 2's moved Chunk 11 (audit-5 F6c; wave-2 ledger row 66) and the
  read-once identity wave 2's integration found (wave-2 plan, Chunk 10's note).
  - **`settings_url`:** by the carried decision in Goals. A change of address alone sends
    the heartbeat again. The value matches the schema's pattern (no user information, query
    or fragment; an IPv6 literal in brackets), which the double checks.
  - **The identity:** by the identity decision in Goals. A different set at the Frame's
    address is reported within one heartbeat interval, and the change is said once
    (a new event; name it).
- **Foreign API:** aiohttp's connection socket address (`ClientResponse.connection`,
  `transport.get_extra_info("sockname")`).
- **Found at HEAD (`aeeec7e`):** `src/arrt_player/client.py` `FrameIdentity.refresh` returns
  at once once `value` is set, so the first identity read is kept for the life of the
  process; `__main__.py` builds one `FrameIdentity` at start and `Supervisor._report_outputs`
  calls `refresh` every cycle. `client_heartbeat` writes `reported_at`, `outputs` and
  `label_outputs` only; no `settings_url` appears anywhere under `src/`.
  `.prawduct/artifacts/architecture.md` records the read-once identity as a known limit in
  its failure table.
- **Depends on:** Chunk 01 merged (`pull.py`, `client.py`, and the pin that names
  `settings_url`). Phase 2.
- **Deliverables:** `src/arrt_player/client.py`, `src/arrt_player/pull.py` (`ClientPull`
  only), `src/arrt_player/__main__.py` (wiring only), `tests/test_client.py` (not the pinned
  event lists, item 6), `tests/test_pull.py`, `tests/test_main.py` (the wiring's test only).
- **Done when:**
  0. verify-api: read aiohttp's `client_reqrep.py` and `connector.py` for how a response's
     connection and transport are reached and when they are released; record where the
     address can be read before the connection returns to the pool.
  1. The acceptance below is red at Chunk 01's head and green.
- **Acceptance:**
  1. The server's view of the Player carries a `settings_url` naming the first
     `UI_ALLOWED_HOSTS` entry when one is listed, the pull's local address otherwise, and
     no key with `UI_BIND=127.0.0.1`; each validates against `client-heartbeat.v1`.
  2. The Player's local address changes and nothing else: the server hears a new report.
  3. The set answering at the Frame's address is replaced, without a restart: the server's
     next report after one interval carries the new set's identity (red today: the first
     identity for the life of the process); a failed read keeps the last identity.
- **Implicit guarantees to keep:** the client heartbeat is sent no more often than today
  when nothing changes; slideshow and setup modes send nothing; an identity never read is
  absent, never empty.
- **Verification ceiling:** `tests/test_client.py tests/test_pull.py tests/test_player_contract.py tests/test_main.py tests/test_running_without_hardware.py`, plus ruff and black.
- **Model:** Opus-class, medium effort. A foreign API to read first; the rest is small.

### Chunk 04: The Player removes from a television only what it recorded uploading

- **Description:** builds the owner's ruling (Goals, quoted) by the four decisions it rests
  on, and closes review observation O-7.
  - **What is the Player's on a set:** a record of every upload, written before it is sent,
    by the second decision in Goals; the bindings an older store holds are taken as
    recorded, under the set they were made on (first decision).
  - **What is removed:** of what the set lists, only what the record says this Player put
    on that set, and of that only what no wanted binding names, or what is due to retire.
    The picture on the wall is still never asked for before another is selected (wave 2).
    A re-rendered work's previous image, a copy whose read-back failed, an orphaned
    binding's id the set lists again and the uploads of the store in memory are each
    still removed, because each is recorded.
  - **The record is bounded:** an entry leaves once the set's listing no longer shows it,
    and an upload whose id was never learned is resolved by the first listing after it.
  - **What the household is told:** the settings page's sentences for a new Frame address
    say the Player leaves the other set's own pictures alone; the journal says once per set
    how many listed pictures it leaves alone (a new event; name it).
- **The persisted format** (store schema 5; learnings: a persisted format is a lock-in
  decision): the questions it must answer, settled before its shape:
  1. Which pictures on set X did this Player put there, by content id and marker, that the
     set may still list?
  2. Which uploads were sent and never learned their id, with the marker each carried, and
     on which set, or on none known yet?
  3. Which of those has the set's listing since shown gone?
  4. What did an older Player's store record, carried forward: every binding with a
     content id, current or set aside, under its set's identity, and none from a store with
     no identity until it adopts one?
  5. Can the previous release open a schema-5 store? (It refuses it, as it refuses 4 from
     3: the report states the rollback value, which is the previous *release*'s schema at
     cut time, and what that release then does to the set, which is the old sweep.)
- **Found at HEAD (`aeeec7e`):** `src/arrt_player/displays/frame.py`
  `FrameDisplay._remove_orphans` asks the set to remove `listed - accounted_content_ids()`
  less the pictures it knows are on the wall, where `DisplayState.accounted_content_ids`
  (`src/arrt_player/state/store.py`) is every binding's content id: so on a set met with no
  record every uploaded picture is removed, the household's included, and
  `.prawduct/artifacts/security-model.md` § What it does to the television records that as
  the design. The store is schema 4 and records no upload apart from its binding.
  `FrameDisplay._upload` writes the binding only after `TvClient.upload` returns; a
  `StoreFault` from `record_upload` leaves a landed copy unrecorded, and the next pass uploads
  again (O-7). `src/arrt_player/tv/samsung.py` `SamsungTv._send_upload` stamps a marker as
  `image_date` and `_attribute` matches it against the listing, raising `TvUploadFailed`
  when the read-back fails, though the image may have landed; today only the orphan sweep
  removes such a copy. `TvClient.listed_content_ids` returns ids only, and `tests/fake_tv.py`
  lists ids with no date. `src/arrt_player/settings_ui/server.py` `_PICTURES_AT_A_NEW_ADDRESS`
  and the Frame field's note say the Player "removes the uploaded pictures there that it
  did not put there".
- **Depends on:** Chunk 02 merged (`frame.py`, `store.py`). Phase 2.
- **Deliverables:** `src/arrt_player/displays/frame.py`, `src/arrt_player/state/store.py`,
  `src/arrt_player/tv/client.py` (one read that returns each listed picture with its date,
  classified as a read), `src/arrt_player/tv/samsung.py`, `tests/fake_tv.py` (the date on
  each listed picture, as the set gives it), `src/arrt_player/settings_ui/server.py` (the two
  sentences only), `tests/test_frame_wall.py`, `tests/test_bindings.py`,
  `tests/test_samsung_client.py`, `tests/test_settings_ui.py` (those sentences only),
  `tests/test_display_state.py`.
- **Must not touch:** `src/arrt_player/client.py`, `src/arrt_player/pull.py` (Chunk 03's).
- **Acceptance:**
  1. A household picture in the set's uploaded list, on a set the Player meets with no
     record of it: still there after the Player's first reconciliation (red today: removed).
  2. The same picture on a set the Player has a store for: still there (red today).
  3. An upload the library misreports and whose read-back fails, which landed: gone from
     the set at the next reconciliation (a guarding test today, through the orphan sweep;
     say whether it was green).
  4. A store that refuses writes: the set gains no picture across three passes (red today:
     one per pass).
  5. A theme uploaded while the set would not say, then an outage, then the set says: its
     copies are gone from the set, the picture on the wall last (red today: the outage
     loses them, wave 2's recorded limit).
  6. A store at schema 4 migrates forward: every retirement and re-render removal it would
     have made it still makes, and it removes nothing it did not record.
  7. The settings page's sentence for a new address no longer says the Player removes
     pictures it did not put there.
- **Implicit guarantees of the orphan sweep, each with a test or named as given up:** a
  re-rendered work's previous image leaves the set (kept); a failed-but-landed upload is
  cleaned (kept, by marker); an orphaned id the set lists again is removed (kept for rows
  this release records; given up for orphaned rows an older Player cleared the id of);
  leaving the store in memory removes its uploads (kept, by marker); the category holds no
  Arrt picture nothing accounts for (**given up**, by the first decision); images of an
  earlier generation of this product are cleared (**given up**); the reads and writes each
  pass sends are unchanged when nothing is due, the new read being the same request as
  `listed_content_ids` (`tests/test_frame_wall.py::test_what_reaches_the_set_in_each_pass_is_unchanged`);
  a removal is sent only from a store whose identity is the set's; the picture on the wall
  is never asked for before another is selected.
- **Visual change:** yes, the settings page's two sentences (§ Operator's checklist, item 16).
- **Verification ceiling:** `tests/test_frame_wall.py tests/test_bindings.py tests/test_samsung_client.py tests/test_display_state.py tests/test_selection_observers.py tests/test_settings_ui.py tests/test_tv_delete.py tests/test_main.py`, plus ruff and black.
- **Model:** Opus-class, high effort. A persisted format, a household's pictures on the
  other end, and every guarantee of a mechanism it replaces.

### Chunk 05: One way to write a file whole

- **Description:** closes review R-5 (and its verify pass's O-2), wave 2 Chunk 05's listed
  `slideshow.save_listing` leak, and review O-3.
  - **R-5:** one helper in a new small module writes a file by temp-and-rename with an
    `fsync` before the rename and the temp file removed on any exit; every hand-written
    copy calls it, each keeping its own temp name and the settings file's mode, by the
    shared-writer decision in Goals.
  - **The slideshow's listing:** written through the helper, so a failed write leaves no
    `.tmp`.
  - **O-3:** a public `logs.current_work_id()` replaces `file_output.bound_work_id`'s read
    through `WorkCorrelationFilter`, and the file label's missing caption binding raises,
    as a missing drawing binding already does.
- **Found at HEAD (`aeeec7e`):** seven temp-and-rename writers under `src/`:
  `src/arrt_player/pull.py` `_write_atomically`; `src/arrt_player/client.py`
  `WallCaches._write_stamp` (fixed at `8d90654` by a third copy, which the verify pass
  named); `src/arrt_player/heartbeat.py` `write_atomically` (a temp name with no leading
  dot); `src/arrt_player/settings_ui/store.py` (0600 by `os.open` and `chmod`, no cleanup
  on failure); `src/arrt_player/displays/file_output.py` `write_atomically` (a near copy of
  the pull's); `src/arrt_player/programmes/slideshow.py` `save_listing` (no cleanup: leaves
  its `.tmp` on failure); `src/arrt_player/compose.py` (a `.composing` name, `fsync` after
  the save, cleanup in `finally`). The two streaming writers (`pull.Pull._cache_media`,
  `src/arrt_player/sources/fetch.py`) write as they read and stay as they are. `file_output.bound_work_id`
  builds a `LogRecord` and runs `logs.WorkCorrelationFilter().filter` on it.
- **Depends on:** Chunk 03 merged (`pull.py`, `client.py`). Phase 3.
- **Deliverables:** new `src/arrt_player/safe_write.py` (name the builder's),
  `src/arrt_player/pull.py`, `src/arrt_player/client.py` (`WallCaches._write_stamp` only),
  `src/arrt_player/heartbeat.py`, `src/arrt_player/settings_ui/store.py`,
  `src/arrt_player/displays/file_output.py`, `src/arrt_player/programmes/slideshow.py`,
  `src/arrt_player/compose.py`, `src/arrt_player/logs.py`, new `tests/test_safe_writes.py`,
  `tests/test_logs.py`, `tests/test_slideshow.py`.
- **Must not touch:** every other test file (Chunk 06's, in the same phase); existing tests
  of the writers are run, not edited.
- **Acceptance:**
  1. A slideshow whose listing write fails leaves no `.tmp` beside the listing (red today).
  2. A grep test: no `.replace(` of a temp file under `src/` outside the helper and the two
     streaming writers (red today: seven).
  3. The helper's own test: the file is whole or untouched after a failure at each step;
     the temp file is gone; the data is `fsync`ed before the rename (observed by patching,
     as `tests/test_client.py::test_a_stamp_reaches_the_disk_before_it_is_renamed_into_place` does).
  4. O-3: a file screen drawing outside any work's context records no work id, and a file
     label drawing outside a caption binding raises by name.
- **Implicit guarantees to keep, each with a test:** the settings file is 0600 even over a
  stale temp file; each writer's temp name is unchanged, so no directory's tidy
  (`Pull._cached_render_names`, the schedule's tidy, `WallCaches`) begins treating it
  differently; the heartbeat file the pull forwards is read only whole; a composed picture
  that cannot be saved leaves nothing under its final name.
- **Verification ceiling:** `tests/test_safe_writes.py tests/test_pull.py tests/test_client.py tests/test_heartbeat.py tests/test_heartbeat_wiring.py tests/test_settings_ui.py tests/test_running_without_hardware.py tests/test_slideshow.py tests/test_compose.py tests/test_logs.py tests/test_media_cache.py tests/test_plane_isolation.py`, plus ruff and black.
- **Model:** Sonnet-class, high effort. Mechanical across seven sites, with the implicit
  guarantees named.

### Chunk 06: Shipped prose names no chunk, and two panel lines name their output

- **Description:** closes review O-4, and two log fields wave 2's Chunk 07 left as
  "could add".
  - **O-4:** the working-tree scan in `tests/test_repository.py` refuses `[Cc]hunk \d{2}`
    in `src/`, `tools/` and `tests/`, shown red on a seeded line; every present instance is
    reworded to say what it means without the number.
  - **The fields:** `panel.close_failed` and `label.close_abandoned` carry `output`, so a
    Player with several panels says which.
- **Found at HEAD (`aeeec7e`):** 25 lines match `[Cc]hunk \d{2}` under `src/`, `tools/` and
  `tests/`, in `src/arrt_player/sources/reddit.py`, `src/arrt_player/sources/resolvers/redgifs.py`,
  `tools/power_probe.py`, `tests/test_label_rule.py`, `tests/test_plane_isolation.py`,
  `tests/test_label_renderer.py`, `tests/test_power_probe.py`, `tests/test_settings_ui.py`,
  `tests/test_client.py`, `tests/test_notify.py`, `tests/fixtures/reddit/README.md`,
  `tests/test_main.py` and `tests/test_bindings.py`; no check refuses them.
  `src/arrt_player/panel/epaper.py` logs `panel.close_failed` and
  `src/arrt_player/label_renderer.py` logs `label.close_abandoned`, neither with `output`.
- **Depends on:** Chunks 03 and 04 merged (their test files). Phase 3.
- **Deliverables:** `tests/test_repository.py`, the comment and docstring lines in the
  thirteen files above (wording only), `src/arrt_player/panel/epaper.py` and
  `src/arrt_player/label_renderer.py` (the two log calls only), `tests/test_epaper.py`.
- **Must not touch:** anything Chunk 05 owns; nothing under `tools/` is run.
- **Acceptance:** the scan red on a seeded `# Chunk 99` in each of the three trees and on
  the present instances, green after; each panel line carries its output in a captured
  journal line.
- **Verification ceiling:** `tests/test_repository.py tests/test_epaper.py tests/test_label_renderer.py`, plus the collection of every edited test file (`--co -q`) and ruff and black on the edited files.
- **Model:** Sonnet-class, medium effort. Mechanical.

### Chunk 07: The pin moves to `contract-v1.3.0`

- **Description:** takes 1.3.0 as Arrt's `player-contract.md` § What a Player does at 1.3.0
  describes it: the 170 invalid fixtures, the client heartbeat's two semantic rules, the
  corrected wall-heartbeat row, and the cap of 32 outputs and 32 label outputs.
  - Every new invalid fixture the Player's tests read gets its reason: a manifest the reader
    refuses, by the reader's own words in `_REFUSED_BECAUSE`; a client document, refused
    whole; a label document, refused or tolerated with why (a set of each, together equal
    to the index's invalid label rows).
  - The four manifest fixtures by the 1.3.0 decision in Goals: the reader refuses
    `staging-repeated`, and whichever of the other three the tag still marks.
  - The client heartbeat fixtures are judged by the schema and `semantic_errors(…, schema=…)`
    together, and what the Player writes is held to both: no two outputs, nor two label
    outputs, of one name, and at most `MAX_OUTPUTS_PER_REPORT` of each, over every
    configuration `client_outputs` and the panels can make.
  - The double answers the corrected `not_an_object` row (`400`, the error shape).
- **Found at HEAD (`aeeec7e`), measured against Arrt's unreleased 1.3.0 (`develop`):**
  the reader refuses 24 of the 28 new `player_must_refuse` manifest fixtures and adopts
  `work-media-content-type-unknown`, `staging-repeated`, `generated-at-not-text` and
  `generated-at-malformed`; the 40 others it adopts, as their index rows allow.
  `parse_client_document` refuses all 20 new invalid client documents. Of 14 new invalid
  label documents the reader refuses 10 and reads `schema-minor-not-a-whole-number`,
  `schema-minor-negative`, `display-state-with-an-unknown-key` and
  `display-state-missing-work-id`. `tests/test_player_contract.py`
  `test_the_client_heartbeat_schema_judges_its_fixtures_as_the_index_says` judges by the
  schema alone, so the four fixtures that pass the schema and break a semantic rule turn it
  red at the new pin.
- **Waits for:** the `contract-v1.3.0` tag (not cut; Arrt's contract package says 1.3.0 on its
  `develop`), the owner's ratification of the cap that tag carries, and the server's answer to
  § Requests, Q1. Then phase 3 merged.
- **Depends on:** Chunk 01 merged (`tests/test_player_contract.py`, `tests/server_double.py`,
  and the new `tests/test_transport_table.py`). Phase 4.
- **Deliverables:** `pyproject.toml`, `uv.lock` (`arrt-contract` only),
  `tests/test_player_contract.py`, `tests/server_double.py`, Chunk 01's new `tests/test_transport_table.py`,
  `src/arrt_player/manifest.py` (only the fixtures the decision enforces), `tests/test_manifest.py`.
- **Acceptance:** a feed that stages one work twice is refused and the wall keeps its last
  good feed (red at `aeeec7e`); every invalid fixture at the tag has a stated reason, and
  an unexplained one fails by name; the writer test fails on a seeded duplicate name and on
  a seeded 33rd output.
- **Verification ceiling:** `tests/test_player_contract.py tests/test_transport_table.py tests/test_manifest.py tests/test_pull.py tests/test_client.py tests/test_label_rule.py tests/test_corpus_contract.py`, plus ruff and black.
- **Model:** Sonnet-class, high effort. Mostly mechanical once the four are decided.

### Chunk 08: The settings page says the Player's state in the server's words

- **Description:** closes audit-5 F4 for the Player's page (wave-2 ledger row 72): the
  page's status line ("Running · pulling from …") becomes the Player's state in
  `vocabulary()`'s `player_states` words, as far as the Player can know it of itself (no
  token yet; waiting for its first report; reporting; not heard from since a date, from
  the last report the server accepted; report unreadable, from a `400`), and each screen it
  lists says its state in `display_states`' word. A state the vocabulary has no word for
  (the server refusing the token) keeps the page's own sentence (§ Requests, Q2). Each word
  is quoted from the package, with a test.
- **Found at HEAD (`aeeec7e`):** `src/arrt_player/settings_ui/server.py` writes "Running"
  and "pulling from" the server's address, "Setup mode" before pairing, and each screen as
  "(no screen connected)" or "(a screen at W×H)" (`_output_words`); no word is quoted from
  the package. The page has no account of the client heartbeat's last answer.
- **Waits for:** the `contract-v1.3.0` tag (`player_states` is 1.3.0's), then Chunk 07.
- **Depends on:** Chunks 04 and 07 merged (`src/arrt_player/settings_ui/server.py`, the pin). Phase 4.
- **Deliverables:** `src/arrt_player/settings_ui/server.py`, `tests/test_settings_ui.py`,
  `tests/test_settings_ui_style.py`, `src/arrt_player/client.py` (an accessor for the last
  report's answer only), `src/arrt_player/__main__.py` (wiring only).
- **Acceptance:** with a token whose report the double accepted, the page reads
  "Reporting"; with the double refusing the report as unreadable, "Report unreadable";
  before pairing, "No token yet", shown as a step, not a fault; a screen with nothing on
  its connector reads "No screen". Each red at `aeeec7e`.
- **Visual change:** yes (§ Operator's checklist, item 17).
- **Verification ceiling:** `tests/test_settings_ui.py tests/test_settings_ui_style.py tests/test_client.py tests/test_main.py tests/test_player_contract.py`, plus ruff and black.
- **Model:** Opus-class, medium effort. Which state the Player can honestly claim of itself
  is a judgment the vocabulary's `means` constrains.

### Chunk 09: Integration

- **Type:** code
- **Description:** the coordinator's, or an integrator's it names, after every code chunk is
  merged (07 and 08 merged or dropped): the interactions no single chunk could see, each
  checked on the integrated head and fixed there with a commit naming the chunks it serves.
  - 04's new read counted in 02's pins of what each pass sends.
  - 03's `settings_url` and identity validated by 01's double, and 01's table replay
    re-run over 03's heartbeat.
  - 05's writer under 01's manifest cap and 03's `ClientPull` changes.
  - 06's scan over every chunk's additions.
  - The pinned event lists in `tests/test_client.py` against every report's new events.
  - The real-Player pair test: Arrt's `player_e2e` against this branch's head (§ Requests,
    Q3), run in Arrt by its coordinator.
- **Depends on:** Chunks 01–06 merged, and 07–08 merged or dropped.
- **Deliverables:** fix-ups only, in the files the interaction names.
- **Acceptance:** the whole suite green on the pool on the integrated head, none missing;
  `prawduct-hook plan status` names 01–08 built or dropped with reasons.
- **Verification ceiling:** the whole suite (the coordinator's run).
- **Model:** Opus-class, medium effort.

### Chunk 10: The records

- **Description:** applies every chunk's records owed to `.prawduct/artifacts/`,
  `deploy/README.md` and the runbook, ticked off item by item against each report
  (learnings: "treat the list as a checklist"), and these known ones:
  - The ruling where the records describe removal: `security-model.md` § What it does to
    the television (the "deletes every user upload it cannot account for" paragraph and
    the "clears the category" sentence), `architecture.md` decision 14 (its open question
    answered) and its failure table's different-set row, `data-model.md` (schema 5 and the
    upload record), `samsung-tv-state-findings.md` § What is still owed, item 9 (the
    household's picture on the wall is no longer removed on a set met afresh); the runbook's
    step 6a (deleting a store now leaves that set's pictures for good, by the first
    decision) and the way back (the rollback value at the previous release's schema, and
    that the previous release sweeps by the old rule).
  - The read-once identity row of `architecture.md`'s failure table, retired.
  - `operational-spec.md` § Continuous integration: "The suite", "The typesetting" and
    "Lint and format" are required on `develop` and `main` (set 2026-11-20).
  - `observability-strategy.md`: every new event, in its inventory between
    `**The event inventory.**` and `**Destinations and retention.**`.
  - The change fragment, new `.prawduct/changes/audit-wave-3.md`, with `closes:` derived from the plan.
- **Type:** cumulative-final
- **Depends on:** Chunk 09. Serial, last.
- **Deliverables:** `.prawduct/artifacts/architecture.md`, `.prawduct/artifacts/data-model.md`,
  `.prawduct/artifacts/security-model.md`, `.prawduct/artifacts/observability-strategy.md`,
  `.prawduct/artifacts/operational-spec.md`, `.prawduct/artifacts/samsung-tv-state-findings.md`,
  `deploy/README.md`, `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`,
  new `.prawduct/changes/audit-wave-3.md`.
- **Acceptance:** each records-owed item ticked off against its report; a whole-repo grep
  (no `--include`) for "cannot account for", "clears the" and "once and keeps it" finds no
  sentence the ruling or Chunk 03 falsified (learnings: "When you retire, amend … grep the
  whole repo"); `tests/test_artifact_citations.py` and `tests/test_norm_index.py` pass.
- **Verification ceiling:** `tests/test_artifact_citations.py tests/test_norm_index.py tests/test_repository.py tests/test_client.py`, plus ruff and black. The coordinator runs the whole suite after it.
- **Model:** Sonnet-class, high effort.

## Operator's checklist (the measurements no agent may take)

**Items 1–13 of `build-plan-audit-wave-2.md` § Operator's checklist stand unchanged**: none
has been run, because no revision has been deployed since. They are not copied here. New:

14. **Whether the set echoes the marker in its listing** (Chunk 04 rests on it; measured
    once, on one upload). With the Player stopped, upload one image with
    `tools/tv_api_check.py` and read the set's listing: the new entry's `image_date` should
    be the date the request sent. If it is absent or rewritten, the upload record falls back
    to content ids alone and an upload whose read-back failed is never cleaned (a follow-up
    decision).
15. **What the set stamps on a picture the household uploads from the phone.** Upload one
    from the set's phone app and read its `image_date`. If it is a phone's photo date or the
    set's clock to the second, a collision with the Player's marker is a same-second upload;
    if it is empty or fixed, nothing changes.
16. **The settings page's sentences for a new Frame address** (Chunk 04), and wave 2's page
    check, which was never queued: open the page from a phone; the Frame field shows pinned
    when `.env` sets `TV_ADDRESS`, editable otherwise; saving a new address says before and
    after that the set there will ask to pair, and now that the other set's own pictures
    are left alone; with a cached client document the Player's name is read-only and says
    to rename it on the server's Players screen; a slideshow field saved in Arrt mode says
    the Player stays in Arrt mode. Only with the owner at the set and a copy of the token
    file kept: save a new address, confirm the token file is gone, the journal shows
    `settings_page.pairing_forgotten` and the set shows a pairing prompt; then restore both.
    If a sentence is missed or misread, the wording changes, or `TV_ADDRESS` goes back to
    `.env` only.
17. **The page's state words** (Chunk 08): the status line beside the server's Players page
    for the same Player, in each state the Player can reach. The two should say the same.
18. **The first run after the upgrade** (the first decision): before the deploy, count the
    set's uploaded pictures (`tools/tv_api_check.py`); after the Player's first
    reconciliation, count again and read the journal's count of pictures left alone. Remove
    any Arrt leftovers by hand if the household wants them gone.
19. **The settings link on the server's Players page** (Chunk 03): it opens this Player's
    page from a phone on the household network. If it names an address the phone cannot
    reach, list the page's name in `UI_ALLOWED_HOSTS`.

## Governance checkpoints

- **Dispatch:** worktrees `arrt-player/.claude/worktrees/audit3-NN` on branches `audit3/NN`,
  cut from `fix/audit-wave-3` at the plan commit (phase 1) or at the phase's integrated head,
  each with `.prawduct/.delegate-brief.md`. The wave-2 brief writer names wave 2's plan,
  scope and branch prefix and must be pointed at this one.
- **Coordinator's edits:** at dispatch, the pointer to the "during wave 2" ruling in this
  repository's `.prawduct/owner-rulings.md`; the norm-index rows' test ids from each report;
  the pinned event lists at each merge (§ Verification strategy, item 6); § Requests relayed
  to the server's wave-3 planner before 1.3.0 is cut.
- **Reviews:** Chunks 02 and 04 each get a `final` review before the next phase is
  dispatched, because 04 builds on 02 and 06 on 04. Chunk 10's `cumulative` makes the
  branch PR-ready.
- **The heavy run:** the whole suite after each phase and after Chunk 10.
- **Before any deploy:** the owner is told the first decision's cost (an older Player's
  unrecorded copies stay on the set) and item 18's check.
- **Owner checks that are not tests:** § Operator's checklist.

## Dropped as already settled

Each was carried to this plan by a record; the code or the records at `aeeec7e` settle it.

- **R-4 items 1 and 3** (an outage left the patience running; a store fault after a change
  of set lost the cleanup): fixed at `82fdea3`, confirmed by the verify pass
  (`rev-20261120T004725Z-231f51d0`, O-1) and pinned by
  `tests/test_bindings.py::TestASetThatWillNotSayWhichItIs::test_a_store_fault_after_the_set_changed_loses_neither_the_line_nor_the_cleanup`. Item 2 and the class are Chunk 02.
- **R-5's drifted instance** (`WallCaches._write_stamp` with no `fsync` and a temp left on
  failure): fixed at `8d90654`, `tests/test_client.py::test_a_stamp_reaches_the_disk_before_it_is_renamed_into_place`.
  The class is Chunk 05.
- **R-6** (mid-build decisions with no vetoable entry): `build-plan-audit-wave-2.md`
  § Goals, under its "Decisions made mid-build", holds all seven.
- **O-5** (deploy README's store-finding glob): `deploy/README.md` § The television's
  identity… now finds the stores with `find` run as the service account, as runbook step 6a does.
- **O-6** (runbook 19b pointing at 6a's find): 19b now carries its own `grep` and `find`.
- **`config.RETIRED_SETTINGS` saying "Settings › Clients"** and **`tools/power_probe.py`
  saying `TV_ADDRESS` lives only in `.env`** (wave 2 Chunk 09's flags): both name the
  settings page and the Players screen now (the `--host` help and the missing-address error
  in the tool).
- **Branch protection** (wave 2's end-of-wave step): set on 2026-11-20, `develop` and `main`
  requiring "The suite", "The typesetting" and "Lint and format" (read from GitHub's
  protection API). Only its record is owed, in Chunk 10.
- **The 1.2.0 fixtures' reader changes:** none needed; both new must-refuse fixtures are
  already refused (Chunk 01's Found at HEAD). Only the stated reason is owed.
- **audit-3 M1** (a refused wall heartbeat posted every poll): `Pull._heard` holds a `4xx`
  report until it changes. Its client-heartbeat sibling is Chunk 01's acceptance 2.
- **The setup code printed as a plain line among the JSON journal lines** (Arrt's chunk 08
  observation): deliberate, wave 1's decision that the setup link stays in the journal and
  out of the structured records (`__main__.py` `run_settings_page`'s comment).
- **`label.cache_forgotten` without `label_id`** (wave 2 Chunk 07's "could add"): it carries
  the file's `path`, which is `<label id>.json`. The two panel lines are Chunk 06.
