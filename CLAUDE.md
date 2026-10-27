@AGENTS.md

# Arrt Player

The player at each wall. It pulls its walls' programming from an **Arrt** server
([brookstalley/arrt](https://github.com/brookstalley/arrt)) into a local cache and
drives the screens and the e-paper label from that cache alone. It moved out of
Arrt's repository on 2026-10-26, with its history.

**This repo is public.** The operator's deployment is recorded in their private
homelab repo. Don't put network addresses, hostnames or usernames here;
`tests/test_repository.py` checks the shapes of such values.

## The contract with the server

The only ground the two share is Arrt's **`arrt-contract`** package, which this
project takes at a released git tag (`[tool.uv.sources]` in `pyproject.toml`).
Read the specification through the package, never from a copy of your own:

```sh
uv run python -c "import arrt_contract; print(arrt_contract.specification().read_text())"
```

`arrt_contract.routes()`, `schema(name)`, `validator(name)` and `fixtures()`
give the routes, schemas and fixtures the suite is held to. **A change to the
contract is a pull request on Arrt and a new tag there**, which this repo then
takes by changing the tag. Never patch the installed package. A field the Player
needs is filed against Arrt.

**Where artifacts live.** The Player's own findings, the deployment record and the
Pi runbook are here (`.prawduct/`, `deploy/`). An artifact that code or a norm
cites and that is not under `.prawduct/artifacts/` here (`player-contract.md`,
`accessibility-spec.md`, `labels-and-surfaces.md` and the rest) is Arrt's, in its
`.prawduct/artifacts/`.

Six names exist in both repositories: `architecture.md`, `data-model.md`,
`security-model.md`, `nonfunctional-requirements.md`, `operational-spec.md` and
`observability-strategy.md`. Arrt's hold the norms (each `§ Direction`, and
§ The television belongs to whoever is using it); the ones here describe what the
Player's code does and point at Arrt's for the rule. **Cite Arrt's as "Arrt's
`<name>.md`"**, so a bare name means this repo's copy;
`tests/test_artifact_citations.py` checks the part of that it can reach.

## Dev commands

| | |
|---|---|
| Test | `uv run --group raster pytest` |
| Lint | `uv run ruff check .` |
| Format | `uv run black .` |

**`--group raster` because the recorded evidence carries it.** The label is
typeset with Pango through PyGObject, an optional group. Without it
`tests/raster` skips itself and `pytest` collects a strictly smaller suite than
the one `test_command` records, and the part left out is the product's most
important accessibility surface. CI runs it as its own `typesetting` job.

**On a Mac, build the venv on Homebrew's Python**, or the typesetter cannot load
GLib. uv picks its own managed interpreter by default, which does not search
Homebrew's library directory, and `tests/raster` then fails at import with
`Failed to load shared library 'libgobject-2.0.0.dylib'`. Measured 2026-10-26 on
a fresh clone: the same suite passes once the venv is built on Homebrew's 3.13.

```sh
uv venv --python /opt/homebrew/opt/python@3.13/bin/python3.13 && uv sync --group raster
```

**A plain `uv sync` uninstalls the optional groups.** uv treats anything outside
the default groups as extraneous, so after a bare `uv sync` the typesetter skips
itself and the suite stays green while it shrinks. Sync with the group the run
needs: `uv sync --group raster`. The panel driver group (`--group epaper`,
`omni_epd`) installs on a Raspberry Pi and nowhere else; nothing in the suite
needs it, because the driver is passed into `EpaperSurface` rather than opened
by it.

**`uv run` always.** pytest, ruff and black live in a `[dependency-groups] dev`
group that only uv installs; a bare `pytest` is command-not-found or a
system-Python run that resolves different dependencies.

## The television

**Nothing in the suite reaches a television or a panel.** It drives a double
behind the TV interface, which is why it runs on a GitHub runner. The hardware is
exercised by `tools/tv_api_check.py`, `tools/tv_delete.py`, `tools/panel_check.py`
and `tools/power_probe.py`, all run by hand.

**`tools/power_probe.py` presses power on the real television, and it is the only
thing in this repo that does.** It refuses to send anything without
`--i-am-at-the-set`, because one measurement is taken from the state that
Arrt's `nonfunctional-requirements.md` § The television belongs to whoever is using it
forbids shipped code to press at, which is legitimate only with somebody watching
the set. Stop the daemon first. Its docstring carries the rest, and
`deploy/README.md` § Measuring what the power keys do carries the paths.

Run the Player: `uv run python -m arrt_player` (copy `.env.example` to `.env`
first). **It drives the real television**, so it is not a thing to start
casually: it uploads, deletes and selects against whatever `TV_ADDRESS` points
at. **Stop it with SIGTERM, never SIGKILL**: the set holds an abandoned
art-channel client's slot for minutes, and a hard kill can leave the next
connection refused. It declines to touch a set that is not in art mode, so it
will not interrupt somebody watching television.

The mutation sweep tool lives in Arrt (`arrt/tools/mutation_sweep.py`); run it
from a checkout of Arrt with `--project` pointed at this repository.
