# Project Preferences

How code is written in Arrt Player. Every session reads this before writing code.

**Carried from Arrt on 2026-10-26**, when Arrt Player moved into a repository of
its own. Each norm below bound the Player in Arrt's repository and binds it here
unchanged; what changed is where its guard runs. The dated history behind each one
(the corrections, the sweeps, the rulings) stays in [Arrt's
`project-preferences.md`](https://github.com/brookstalley/arrt/blob/develop/.prawduct/artifacts/project-preferences.md),
and a norm that "lives in" an artifact this repository does not carry lives in
Arrt's `.prawduct/artifacts/`.

## Language & Runtime

- **Language**: Python.
- **Version**: **3.13, floor 3.12.** Raspberry Pi OS Trixie ships 3.13, and the
  IT8951 e-paper driver compiles Cython from 2023 sources targeting 3.13/3.12;
  it builds and imports on 3.13/aarch64 under uv's PEP 517 isolation (verified
  2026-08-04). 3.12 is the declared floor, not a fallback anyone expects to take.
  `platform-and-dependency-findings.md` is the record.
- **Package manager**: uv, one project, one `uv.lock`. Optional groups are
  `raster` (the text stack; installs on any modern Linux and on a Mac against
  Homebrew's libraries) and `epaper` (the panel driver; a Raspberry Pi only).

## Code Style

- **Naming**: `snake_case` functions and module-level names, `PascalCase` classes.
- **Formatting**: black, `line-length = 130`, `target-version = py312` — the
  declared floor, so formatting never uses syntax the floor cannot parse.
- **Linting**: ruff with `select = ["ALL"]` (the owner, 2026-10-05: "go big on
  ruff"), less a short ignore list in `pyproject.toml` that names each rule's
  reason. A rule is opted out of, with a reason, never forgotten into.
- **Type annotations**: every function signature in `src/` and `tools/` (ruff
  `ANN`); the test tree is exempt.
- **Imports**: strict stdlib / third-party / local grouping (ruff `I`).
- **Logging vs print**: `logging` only in `src/`; `print()` only where a
  `T201` carve-out in `pyproject.toml` `[tool.ruff.lint.per-file-ignores]` says
  why, or a per-line waiver does.

## Testing

- **Framework**: pytest, with pytest-asyncio in auto mode. The declared suite is
  `uv run --group raster pytest`; `tests/raster` needs the `raster` group and
  skips itself without it, which is why CI runs it as a job of its own.
- **Style**: descriptive test names stating the behaviour under test.
- **Coverage expectations**: happy path and likely error paths for every module
  that reads outside data (the manifest, the client document, the television's
  replies). The television and the panel are covered behind their interfaces,
  never by reaching real hardware.
- **Testing strategies**: example-based tests, and property-based tests
  (hypothesis, derandomized in `tests/conftest.py`) where a spec asks for
  invariants, as the label layout does. The contract both sides are tested
  against is the `arrt-contract` package at a released tag.
- **Test location**: `tests/`, the one directory `testpaths` collects. A
  `test_*.py` anywhere else is never collected.
- **Parallelization**: none; the suite is serial.

## Architecture Patterns

- **Error handling**: exceptions; catch specific types. A genuinely necessary
  broad catch carries `# prawduct:allow prawduct/broad-except -- reason`.
- **Async**: at the I/O boundary, synchronous core. `samsungtvws`'s async art
  client forces async at the television's edge, and the pull is async with the
  daemon; composition, layout and parsing are not coroutines.
- **File organization**: a `src/` layout package (`src/arrt_player/`), the
  hand-run tools in `tools/`, the deployment in `deploy/`.

## Tooling

- **Key libraries**:
  - `samsungtvws`, pinned to a **git SHA on a fork** (`NickWaterton/samsung-tv-ws-api`),
    not PyPI: the PyPI package ships no async art client and no event callbacks,
    and declares the same version string.
  - `omni-epd`, the e-paper driver, in the `epaper` group, with `IT8951[rpi]`
    pinned by an override because pinning the parent does not pin the child it
    resolves. Neither suite needs it: the driver is passed into `EpaperSurface`
    rather than opened by it.
  - `pycairo` + `PyGObject`/Pango, the label's typesetting, in the `raster` group.
  - `arrt-contract`, Arrt's contract package, from a git tag
    (`[tool.uv.sources]`); a new contract version is a change to that one line.
- **Dev commands**: `CLAUDE.md` carries them and is the authority.
- **`deploy/pi-freeze-2024.txt`** is a `pip freeze` from the 2024 Pi's venv: the
  record of the driver commits the wall has run since 2024, which the `epaper`
  pins reproduce. Nothing installs from it.
- **Configuration**: everything deployment-specific reads from `.env`;
  `.env.example` lists exactly the keys `config.py` reads.

## Workflow

- **Branching**: feature-branches (default: feature-branches — create a branch for medium+ work, direct commits to protected branches only for trivial fixes; set to "direct" for solo projects where committing to main is OK)
- **Protected branches**: main, develop (branches that should not receive direct commits unless branching is "direct")
- **PR creation**: wait_for_user (default: wait_for_user — only create PRs when explicitly asked; set to "automatic" to create PRs after Critic review passes)
- **PR merge**: wait_for_user (default: wait_for_user — present the PR for user review before merging; set to "automatic" to merge after CI passes and review is clean)
- **PR merge strategy**: merge commit (default: merge commit — `gh pr merge --merge`; preserves each commit's identity so a reused branch's merge-base stays correct and the review/PR gates don't re-review already-merged work; set to "squash" for one linear commit per PR, or "rebase" — with either, branches are single-use: delete after merge and never reuse, because the rewritten history strands a reused branch's merge-base)
- **Branch cleanup**: propose (default: propose — when merged branches or dead worktrees accumulate, the session start proposes removing them, each with the proof that nothing is lost, and the agent removes them on your yes; `prawduct-hook git-tidy` shows the list any time. Set to "off" to silence the proposal and keep a branch after its PR merges, except under a squash or rebase merge strategy, where deleting it is required)
- **Long-lived branches**: (unset — branches never proposed for deletion, beyond `main`, `master`, `develop`, `trunk`, `release/*`, the remote's default branch and the configured base. Name any environment branch kept level with one of those, such as `staging`, comma-separated, because a branch at the same commit as `main` otherwise reads as merged)
- **Commit attribution**: none (default: none — no `Co-Authored-By`, `Signed-off-by`, or "Generated with …" trailers on commits or PR bodies; set to "co-authored" to add a Claude `Co-Authored-By` trailer)
- **Upstream filing**: ask-user (default: ask-user — governs `/prawduct:report-bug`, which files a bug about *prawduct itself* into prawduct's own public tracker. Under ask-user you are shown the exact outbound bytes and nothing is sent without your per-report approval of them; set to "never-file" for a standing no, which the adapter refuses on mechanically and without exception; set to "always-file" for standing pre-consent, which stops the asking — and then the drafting step is the only thing between your session's words and a public issue in someone else's repository)
- **Stats contribution**: always (carried from Arrt, where the owner gave standing consent on 2026-10-05: a report carries only numbers and fixed-list values from a public allowlist, no names, paths, branches, text or identifiers)
- **Delegation**: (unset — `/prawduct:methodology delegation` states the default and anything written here overrides it. Say in prose how much this project wants fanned out to subagents and what it is worth fanning out for. `off` is a complete answer: it means no delegation at all, it is honoured without ceremony, and nothing nags a repo that has said it.)
- **Delegate verification**: (unset — what a delegate here may run to prove its own change, and what it must leave to the coordinator's integration run. In this project's own words: prawduct does not know this project's test regime and will not invent a vocabulary for it. `/prawduct:doctor` will propose a starting point from what this repo already encodes about running part of its suite.)
- **Inner-loop verification**: (unset — what to run while iterating on a change and at each chunk's Verify, before the declared suite runs at the boundary, before the work lands — or say here that this project wants the declared suite at every chunk. In this project's own words: prawduct does not know this project's test regime and will not invent a vocabulary for it. `/prawduct:doctor` will propose a starting point from the same markers, scripts and targets it finds for `Delegate verification`.)
- **Delegation approval**: ask-on-reason (default: ask-on-reason — a plan that will delegate discloses it and proceeds, asking for approval only on one of the enumerated reasons in `methodology/planning.md` "Partition: Serial or Delegated"; set to "pre-approved" once you have seen it work here, and the ask stops returning with every plan)
- **Found problems — fix or file**: **strong bias towards fixing.** Filing is the
  exception and needs one of exactly two reasons: it is **orthogonal to the
  current work AND would require design or requirements**, or it is **medium or
  larger** work. An issue is a promise to pay attention later, and a one-line fix
  costs less now, while the file is open and the cause understood, than it costs
  to write up, re-find and re-verify. A fix taken under this is still reported:
  say what you fixed and why it did not meet the filing bar. Ratified by the
  owner for Arrt on 2026-08-05 and carried here.

---

**What belongs here**: How you want code written. Conventions, tools, style preferences, workflow preferences.

**What doesn't belong here**: What to build (product-brief), system design (data-model, architecture), performance targets (nonfunctional-requirements), or deployment (operational-spec).

## Enforcement

Each preference above should be enforced by one of three mechanisms — assign the mechanism when you add the preference so it doesn't quietly become aspirational.

| Mechanism | Where it lives | What it catches | Trade-off |
|---|---|---|---|
| **Linter** | Project's configured linter (ruff, eslint, swiftlint, etc.) | Mechanical style/naming rules | Best tool when configured. If no linter, preferences in this category fall through to Critic. |
| **Test** | `tests/preferences/test_*.py` (or equivalent) | Structural rules with named exceptions (AST checks, config-presence checks) | Bakes the rule into CI; refuses to be silent. Cost: re-validate when the rule's shape changes. |
| **Critic** | `/prawduct:critic` review (Goal 3: norm departures) | Judgment-required rules (semantic naming, "appropriate" anything, what counts as a "boundary") | No false-confidence test. Cost: requires reviewer per chunk; misses violations between reviews. |

This per-preference table is the product's **norm index** (`/prawduct:methodology norms`): each row assigns a norm its **mechanism** (linter / test / Critic) and its **audit home** — `janitor` (only the deep sweep sees it) or `advisory` (a mechanical probe fires on it). A row may be a **pointer** to a `## Direction` section instead of restating the norm, and every norm carries its **why** (a whyless norm is unenforceable at its edges).

**Every populated row here *is* a homed norm** — the `norm-health-sweep-overdue` advisory reads
these rows to decide whether this product has norms worth auditing, so never leave an example or
placeholder row in the table: it would claim a norm registry that has not been ratified. Two row
shapes go in — an ordinary row naming the convention, its mechanism, its enforcement artifact, its
audit home (`janitor` or `advisory`) and its why; and a **pointer** row whose first cell reads
`norm lives in <artifact> § Direction`, whose enforcement artifact is `—`, and whose why lives in
the Direction entry it points at.

| Preference / norm | Mechanism | Enforcement artifact | Audit home | Why |
|---|---|---|---|---|
| **The television belongs to whoever is using it** — norm lives in `nonfunctional-requirements.md` § The television belongs to whoever is using it | Test | `tests/test_frame_wall.py::test_a_television_somebody_is_watching_is_left_alone` and `tests/test_frame_wall.py::test_a_republished_schedule_is_not_put_on_a_television_somebody_is_watching` (the art-mode gate); `tests/test_power_probe.py::test_press_itself_refuses_not_only_the_command_line` (the one tool that presses power refuses without a person at the set). The daemon's power gate gets its guard with Player power control, the build that first gives the daemon a power key to press | janitor | The interruption this norm exists to prevent shipped once through a deliberate decision, a review and a green suite, which is why its mechanism is a test rather than judgement. `tools/power_probe.py` is the one thing that presses power, and refuses to without `--i-am-at-the-set`. |
| **A found problem is fixed, not filed, unless it is (orthogonal AND needs design) or medium+** (§ Workflow) | Critic | — | janitor | Judgement-required by construction: "would this need design" and "is this medium+" are the calls no check can make. A filed item should say which test it met; one asserting neither is the finding. |
| black formatting, line-length 130 | Linter | `[tool.black]` in `pyproject.toml`; CI's `lint` job | janitor | Removes formatting from review entirely. |
| snake_case functions, PascalCase classes | Linter | ruff `N` (in `ALL`), `N818` ignored, `pyproject.toml` | janitor | Mechanical. `N818` is ignored because exception class names are public and already read as errors. |
| Strict stdlib / third-party / local import grouping | Linter | ruff `I` (in `ALL`), `pyproject.toml` | janitor | Mechanical; it should cost no review attention. |
| `logging` only; `print()` reserved for deliberate CLI output | Linter | ruff `T20` (in `ALL`), `pyproject.toml`, with the per-file carve-outs `pyproject.toml` lists under `[tool.ruff.lint.per-file-ignores]`, each with its reason there; anywhere else a deliberate report is waived per line as `# noqa: T201 -- <reason>` | janitor | The daemon runs unattended under systemd, where a `print()` has no level and no timestamp and a failure is invisible in the journal. |
| **Every lint waiver says why**: `# noqa: CODES -- reason`, or a `noqa` beside a prawduct pragma that carries its reason | Test | `tests/test_waivers.py::test_every_waiver_says_why` | advisory | Under `select = ["ALL"]` a per-line waiver is the record of every deliberate exception, and one without a reason reads the same whether it was argued for or copied from the line above. |
| Type-annotate every function signature in shipped code | Linter | ruff `ANN` (in `ALL`), `pyproject.toml`, excluding `tests/*` | janitor | Annotations are how a reader of a seam such as `Measure` or the television's interface knows what crosses it. |
| Catch specific exceptions; broad catch needs `# prawduct:allow prawduct/broad-except -- reason` | Linter (a catch that swallows) + Critic (that the pragma's reason holds) | ruff `BLE001` (in `ALL`), `pyproject.toml` | advisory | A swallowed exception in an unattended Player shows up as "the TV just stopped changing", with nothing in the log to say why. |
| No hardcoded deployment values in source (addresses, paths, coordinates) | Test | `tests/test_repository.py::test_no_source_file_carries_a_deployment_value` and `tests/test_repository.py::test_no_tracked_file_carries_this_machines_deployment_values` | advisory | This repository is public, and the same code runs on the Pi and on a laptop. The check knows the shapes of such values rather than the values, so it publishes nothing of its own. |
| **The TV pairing token is never tracked** | Test | `tests/test_repository.py::test_no_credential_file_is_tracked` | advisory | The token was committed to a public repository once; it is a secret, in the same class as the log-line row below. |
| Async at the I/O boundary, synchronous core (§ Architecture Patterns) | Critic | — | janitor | `samsungtvws` forces async at the television's edge only; letting it spread makes composition and layout untestable without an event loop. |
| Hardware + network access sits behind an interface | Critic | — | janitor | Both display drivers are dormant upstream; an interface keeps a frozen 2023 driver from dictating the Python version, and is what lets the suite run on a runner (`platform-and-dependency-findings.md`). |
| No secret ever reaches a log line | Test (the startup path) + Critic (everywhere else) | `tests/test_config.py::test_the_client_token_is_in_no_repr_and_no_startup_line` | advisory | The repository is public, and the journal is read over someone's shoulder during a failure, which is when logging is turned up. Beyond startup the violation is usually a whole object logged whose repr carries a token, which only a reviewer sees. |
| **The Player never requires the server to be reachable** — norm lives in `nonfunctional-requirements.md` § Direction | Test (the cache claim) + Critic (new calls) | `tests/test_pull.py::test_the_server_stopped_while_the_wall_runs_and_the_schedule_continues_from_the_cache`, `tests/test_pull.py::test_a_player_restarted_with_the_server_down_starts_from_its_cache` and `tests/test_availability.py::test_killing_curation_changes_nothing` | janitor | An unreachable server never blanks a wall: the Player renders only from its local cache. A new call to the server is a violation depending on whether rotation can proceed without it, which is judgement. |
| **The e-paper label is legible at standing distance: the panel is driven at 16 greys, and type never shrinks below the floor — content drops instead, except the facts that identify the work, which shrink rather than vanish** — norm lives in `accessibility-spec.md` § Direction | Test | `tests/test_epaper.py` (the mode is set, and a panel that stays in one bit is refused), `tests/test_label_layout.py` (the two tiers, the name ladder and the drop rule), `tests/test_label_properties.py` (both tiers as invariants over every content shape) and `tests/test_label_renderer.py` (the shrink reaches the journal) | advisory | Type sized without converting pixels to the angle a reader sees shipped once at half the legible size, through a probe, a review and a cutover. Ratified by the operator 2026-08-11. |
| **The manifest and its media, pulled over HTTP into the Player's cache, are the only channel from the server, and the Player makes only the requests the contract's `routes.json` names** | Test | `tests/test_plane_isolation.py` | strict | A violation ("just fetch the label text live") works in development and in every test, because the server is up in both, so a green suite is exactly what a violation looks like without this check. Only `pull.py` opens an HTTP client, only to the contract's routes, and nothing imports a server module — true by construction across two repositories and still asserted, because a vendored module would break it silently. |
| **A display device renders its own label, and the label travels as metadata** — norm lives in `architecture.md` § Direction | Critic | — | janitor | Output surfaces are plural: panels of different geometry, or a monitor with no e-ink drawing the label into the mat. The violation is reasoning about a device's geometry somewhere other than on that device, which has no import signature. |
| **The heartbeat's instant is reported under the key the contract names** | Test | `tests/test_heartbeat_key.py` | advisory | The heartbeat is the only channel from the Player back to the server, and neither may import the other to share a constant; each holds its own to the contract's `instant_key()`, so the two agree because each agrees with the package. |
| **Derived artifacts are regenerated, never transported** — norm lives in `data-model.md` § Direction | Critic | — | janitor | A render made for one geometry is wrong anywhere else, and regenerating it on the device is cheap: the Player composes every work for its own outputs. |
| **Coverage expectations** (§ Testing) | Critic | — | janitor | A reviewer reads the tests a change adds. |
| **Testing strategies** (§ Testing) | Critic | — | janitor | The network and the hardware are doubled at their module boundary; a reviewer checks that a new test reaches neither. |
| **Tests live in `tests/`** (§ Testing, Test location) | Test | `tests/test_repository.py::test_every_test_module_is_in_tests` | advisory | `testpaths` collects only `tests/`, so a test module anywhere else looks like a test and guards nothing. |

### Deliberately uncovered (ruled, not overlooked)

Mechanical guards this index knowingly does **not** name as norm enforcement,
recorded so that a Norm Health sweep does not rediscover them and re-raise the
question. A guard listed here is still a guard; what the ruling says is that it
is not a norm this product states, so it gets no row.

| Guard | Ruling (owner, 2026-08-06) | Reasoning |
|---|---|---|
| `tests/test_repository.py::test_no_source_or_package_directory_is_gitignored` | Not a stated norm — repository hygiene | A file excluded from version control is invisible to review, which is a real failure and a generic one, true of every repository. The guard stays because it is cheap and the failure is silent. |
| `tests/test_norm_index.py` | Not a stated norm — the index's own plumbing | It enforces a property of the index, which makes it infrastructure of the index rather than a norm in it. |


**A filled `Delegation` / `Delegate verification` / `Inner-loop verification` row states a norm, and each takes `Critic`** — a policy stated in prose is judgment-required by construction, so no linter or test can grade it; audit home `janitor`, and the why is the sentence the owner gave for it. One row covers the policy `Delegation` and `Delegate verification` state together; `Inner-loop verification` governs your own loop rather than a delegate's, so it gets its own row. `Delegation approval` is a setting like `PR creation`, not a norm. The row is written when the policy is **ratified** — the owner confirms it, and `/prawduct:doctor` proposes a starting point for all three — never shipped here, because this table ships empty.

**Rule for adding a new preference:** assign a mechanism. If the preference can be expressed as "every file/function/config matches pattern X with named exceptions" → write a test. If a linter rule already exists for it → configure the linter. If it requires understanding intent → assign to Critic. Never leave a preference unassigned.

**False-confidence guardrail:** if a generated test would pass on conforming code but couldn't reliably catch a real violation (e.g., greppy heuristics for semantic rules), prefer Critic over a weak test. A green test that doesn't actually check the rule is worse than no test.
