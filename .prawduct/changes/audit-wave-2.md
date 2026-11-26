---
schema: 1
type: fix
scope: audit-wave-2
date: 2026-11-19
title: The Player's share of the 2026-11-19 audit, wave 2
closes: [brookstalley/arrt-player#5, brookstalley/arrt-player#9, brookstalley/arrt-player#10, brookstalley/arrt-player#18, brookstalley/arrt-player#19, brookstalley/arrt-player#46]
---
The larger and dependent fixes the 2026-11-19 audit left after wave 1, built as
chunks 01–09 and 12 of `build-plan-audit-wave-2.md` (10 and 11, the contract pin and
the settings page's address told to the server, moved to the next plan).

**The television.** The Player now knows each television by its own id
(`device.duid`), not its address. The Frame's store moves to schema 4 and records
which set its bindings were made on; the same set at a new address keeps its
pictures, and a different set starts afresh with every work uploaded, with the
uploaded-pictures category cleared of what the Player cannot account for
(**the household's own uploads included**; audit wave 3 ended that, and the Player
now removes only what it recorded uploading), while the old set keeps the Player's
pictures until the Player is pointed at it again. While a set will not say which
it is, the wall uses no stored picture for fifteen minutes and then shows art
without removing anything. A work no feed has named for 14 days of the wall running
is removed from the set, and a departed wall's directory goes after a week. The
settings page holds the Frame's address and forgets the pairing when it changes.

**Faults stay where they happen.** A cache that refuses a write no longer ends
the wall's pull: the last good feed is kept and the pull goes on (#9). A fault in
a Frame wall's store ends that pass, said once at a level by whether it may clear
(`store.busy` or `store.failed`), and the wall goes on at its next pass rather
than stopping (#19). A wall's directory under `CACHE_DIR` is removed a week after
the wall left this Player, counted only while the Player reads its client
document (#5). Every line logged inside a wall's task carries its `wall_id`
(#10).

**The settings page** says what a save did: slideshow fields saved while the
Player runs in Arrt mode say the Player stays in Arrt mode and the settings wait
for the slideshow, rather than looking applied (#46). Once the Player has its
client document, the page shows the server's name for it read-only.

**Labels.** A label is drawn only on the panel its output names. `EPD_DEVICE`
naming more than one panel is refused at start: one panel per Player is the
limit by the owner's ruling of 2026-11-19, not the design's.

**Running without hardware.** `FILE_OUTPUT_DIR`, `FILE_OUTPUT_SCREEN` and
`FILE_OUTPUT_LABEL` make a Player that draws its screen and label to files and
drives no television, screen or panel, so a real Player can be tested against a
real server. They are not for a wall: the Player refuses to start with one beside
`TV_ADDRESS` or `EPD_DEVICE`.

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
per work (runbook step 19b; the store is schema 5 since wave 3, and going back
across it needs the owner's say-so each time, because the previous revision then
sweeps the set); a settings file that holds `TV_ADDRESS` is layout 3
and must have it moved back into `.env` (step 19a). A Frame wall's loop lines are
`frame.*`, not `daemon.*`. An `.env` whose `EPD_DEVICE` names more than one panel
now stops the start (exit 2): name one. Every step these notes point at that is
new in this release has not yet run on a Pi.
