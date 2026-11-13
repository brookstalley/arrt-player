# Release Plan — v0.8.0, Whole-Develop Promotion

**Version:** v0.8.0, a **minor**: it adds behaviour, slideshow mode (#27). A Player
can run with no Arrt server, playing one Reddit listing on one HDMI screen through
mpv, each post captioned on the e-paper label. The slideshow's screen follows the
sun, as an Arrt-mode screen has since v0.7.0. The owner asked for it on 2026-11-13:
"we need to get everything pushed to develop, released to main, then to the pi"
(`owner-rulings.md`).

`develop` was at `e86ea3c` (the merge of #43) when this was cut.

## Release classification

Every release-pending scope ships. `check-releasability --release v0.8.0` listed one
scope, and none was unclassifiable.

**No open issue blocks the release.** brookstalley/arrt-player has no blocker label.
#41 and #42 are follow-ups the boundary review filed, and neither marks anything as
shipping broken.

| Scope | Disposition | Blocker |
|---|---|---|
| slideshow | ships | |

**Nothing is withheld**, so this is a whole-develop promotion.

## Release prep

- The `slideshow` fragment is cut into the change-log archive with `release=v0.8.0`.
- `pyproject.toml` and `uv.lock` go from 0.7.0 to 0.8.0. The contract stays at the
  `arrt-contract` tag it already takes, because this release does not change it.
- **`build-plan-slideshow.md` stays live.** Its Chunk 05 is open, and the open part
  is the acceptance on the Pi, which by the owner's ruling runs at this release's
  deploy, with the owner at the set. Archiving the plan comes after that acceptance,
  together with its record and the close of #27.

## Before deploying

The consumer notes ask three things of a Pi, all from `deploy/README.md`
§ The slideshow:

- **mpv installed** (`apt-get install --no-install-recommends mpv`). The reference
  Pi has mpv 0.40.0 (read 2026-11-13).
- **The unit gains group `render`**, which makes it `SupplementaryGroups=spi gpio video render i2c`.
  Install it as the runbook's steps 9–11 say.
- **Nothing for a wall in Arrt mode.** `.env` needs no new key.

The reference Pi runs `0bf4fa1`, which has v0.7.0's code, so this release's own
changes are the slideshow and the unit's `render` group.

## Rollback

Move the Pi back by `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`.
**One thing needs undoing by hand** once this revision has written the settings file,
which saving any setting or setting the page's password does. An older Player
refuses layout version 2 by name. Before starting it, put the file back to layout 1
as `deploy/README.md` § The slideshow says. The `.slideshow*` and `.mpv` entries in
the cache can stay.

## Deploy

This merge deploys nothing by itself. The Pi moves by the runbook above. Chunk 05's
acceptance on the Pi follows it: switch to the slideshow from the page, check that
the owner's listing plays at 4K with captions on the panel, switch back to Arrt mode
and see the wall return, and check the cookie's renewal.
