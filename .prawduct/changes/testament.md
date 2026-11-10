---
schema: 1
type: chore
scope: testament
date: 2026-11-10
title: The suite runs on the testament pool
---
The Player's suite runs on the testament pool, as Arrt's suites already do. The
move out of Arrt's repository did not bring that setup along.

### What was added
- `.testament.toml`: one container suite, the declared one
  (`uv run --frozen --group raster pytest` over `tests/`). `--group raster` is in
  the setup step too, because a plain `uv sync` uninstalls it and
  `tests/raster` then skips itself while the run stays green.
- `.devcontainer/Dockerfile`, the test image. It is Debian trixie, because the
  locked PyGObject builds from source against girepository-2.0, which bookworm
  lacks. It installs Python 3.13 through uv, the cairo, pango and girepository
  headers and a C toolchain for the build, DejaVu fonts (Pango with no fonts
  renders blank), git and tar, and ends as a non-root user. Both base images are
  pinned by digest.
- `AGENTS.md` § Running tests (testament's agent block), and CLAUDE.md's Test
  command points at it.

### Verified
- In the image, built locally: `uv sync --frozen --group raster`, then the whole
  suite passed with none skipped, the same count as a local run of the same tree.
- On the pool: `testament onboard -- tests/test_config.py` ran on a runner, and
  `testament run` ran the whole suite, all passed, with no fallback and nothing
  missing.

### Also carried: the settings page's records, after its deploy
The runbook, the settings-ui plan and `deploy/README.md` § The settings page
said the on-Pi checks had not been run. They ran when the wall Pi moved to
9ca66b2 on 2026-11-10, and all three records now say so. They ride this branch
because `develop` takes changes only by PR.

### Confirmed by the owner
The container executor and one suite, 2026-11-10.

## Consumer note
none: how the suite is run changed, not what the Player does. CI and the
prawduct `test_command` still run pytest directly.
