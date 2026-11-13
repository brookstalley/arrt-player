# Release Plan — v0.7.0, Whole-Develop Promotion

**Version:** v0.7.0, a **minor**. It is the Player's first release from its own
repository. `main` still holds the history carried from Arrt at the move, ending at
Arrt's v0.6.0, and `pyproject.toml` read 0.6.0. The owner asked for it on 2026-11-13
("yes release"). It is a minor because it adds behaviour: a settings page and a first
run with no server configured (#28), and an HDMI screen's brightness following the sun
(#37). It also carries:

- the move out of Arrt's repository (#1);
- the Player's discovery and its own strategy artifacts (#15);
- Pillow 13 and certifi, for their security fixes (#26);
- the suite's move onto the testament pool (#29);
- the private token file and the stale-ETag fix (#33).

`develop` was at `a24f7d1` (the merge of #38) when this was cut.

## Release classification

Every release-pending scope ships. `check-releasability --release v0.7.0` listed 7
scopes, none of them unclassifiable.

**No open issue blocks the release.** brookstalley/arrt-player has no blocker label and
no open issue that names one.

| Scope | Disposition | Blocker |
|---|---|---|
| stand-alone | ships | |
| discovery | ships | |
| pillow-13 | ships | |
| settings-ui | ships | |
| testament | ships | |
| token-mode-and-stale-etag | ships | |
| hdmi-brightness | ships | |

**Nothing is withheld**, so this is a whole-develop promotion. `main` is an ancestor
of `develop`, so the promotion is a merge commit with no conflicts.

`stand-alone`, `discovery`, `pillow-13`, `testament` and `token-mode-and-stale-etag`
have no build plan, and that is expected. Each was a move, a chore or a fix on its own
branch, reviewed at its PR, and its change record says what it changed.

**The slideshow does not ship.** `feature/slideshow` (#27) has not merged into
`develop`, so none of it is in this tree.

## Release prep

- The seven change fragments are cut into the change-log archive with `release=v0.7.0`.
- `pyproject.toml` and `uv.lock` go from 0.6.0 to 0.7.0. The contract stays at the
  `arrt-contract` tag it already takes; this release does not change it.
- `plan-backfill` archived `build-plan-settings-ui.md`.
- `build-plan-hdmi-brightness.md` was archived with `archive-plan --state completed`.
  The sweep refused it because chunk state reads chunks 01 and 03 as open. Their
  commits do not carry the plan's scope tag in the form chunk state reads: chunk 01's
  is filed under `ddc`, and chunk 03's is "Chunk 03, part 1". PR #37 records that both
  chunks shipped inside its cumulative review's span. The archive stamp
  `unbuilt_at_archive` repeats the chunk-state reading. It is not a statement that the
  work is missing.

## Before deploying

The consumer notes ask three things of a Pi. They come from `deploy/README.md`, in
these dated sections:

- **§ The settings page (2026-11-10).** Check `.env` first. A malformed `SERVER_URL`,
  a `CLIENT_TOKEN` containing whitespace, or a `PLAYER_NAME` over 64 characters now
  stops the start with exit 2. Port 8771 is listened on unless `UI_BIND` narrows it.
- **§ The television's token is private to `tvpi` (2026-11-11, #3).** Install the
  updated `deploy/display.service`, then run `systemctl daemon-reload`.
- **§ An HDMI screen's brightness follows the sun (2026-11-13, #34).** Install
  `deploy/modules-load.d/arrt-player.conf` and the updated unit.

The deployment's own Pi already runs `develop` at `0bf4fa1` (handoff of 2026-11-13),
and that revision's code is this release's. The release adds only governance records
(#38). Whether all three steps above were carried out on that Pi has not been
re-checked for this release. The deployment record in the owner's homelab repo says
what was installed.

## Rollback

Move the Pi back to the earlier revision by
`.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`. Nothing in the cache
needs undoing:

- An earlier Player ignores the settings file.
- A token file that is now mode 600 still reads for `tvpi`.
- A brightness set over DDC/CI stays wherever it was last put, until the monitor or
  the next Player changes it.

## Deploy

This merge deploys nothing by itself. The Pi moves by the runbook above.
