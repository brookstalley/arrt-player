---
schema: 1
type: fix
scope: audit-wave-2
date: 2026-11-19
title: The Player's share of the 2026-11-19 audit, wave 2
closes: [brookstalley/arrt-player#5, brookstalley/arrt-player#9, brookstalley/arrt-player#10, brookstalley/arrt-player#18, brookstalley/arrt-player#19, brookstalley/arrt-player#46]
---
The larger and dependent fixes the 2026-11-19 audit left after wave 1, built as the
chunks of `build-plan-audit-wave-2.md`. The plan's last chunk completes this body
with what was built.

**The television.** The Player now knows each television by its own id
(`device.duid`), not its address. The Frame's store moves to schema 4 and records
which set its bindings were made on; the same set at a new address keeps its
pictures, and a different set starts afresh with every work uploaded, with the
uploaded-pictures category cleared of what the Player cannot account for
(**the household's own uploads included**), while the old set keeps the Player's
pictures until the Player is pointed at it again. While a set will not say which
it is, the wall uses no stored picture for fifteen minutes and then shows art
without removing anything. A work no feed has named for 14 days of the wall running
is removed from the set, and a departed wall's directory goes after a week. The
settings page holds the Frame's address and forgets the pairing when it changes.

**The unit.** `Type=notify` with a progress watchdog (`WatchdogSec=300`), and
restrictions that move no path or device. None of it has run under systemd.

## Consumer note
An operator upgrading: **if a Frame's address was ever moved to a different
television, delete that wall's `display-state.sqlite` before starting the new
revision** (runbook step 6a), because nothing on the Pi records which set the old
rows were made on. The unit changes, so steps 9–11 install it and steps 12a to 14c
check that every mode starts, that a refused `.env` still restarts every 5 s and
that a stopped Player is restarted. **The way back** across the store's schema:
the previous revision refuses a schema-4 store and parks that wall, so stop the
Player and set each Frame wall's `PRAGMA user_version` to what the previous
revision writes, 2 (not 3, which it still refuses), at the cost of one re-upload
per work (runbook step 19b); a settings file that holds `TV_ADDRESS` is layout 3
and must have it moved back into `.env` (step 19a). A Frame wall's loop lines are
`frame.*`, not `daemon.*`.
