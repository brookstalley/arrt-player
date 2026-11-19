---
schema: 1
type: fix
scope: audit-wave-1
date: 2026-11-19
title: The Player's share of the 2026-11-19 audit, wave 1
closes: [brookstalley/arrt-player#6, brookstalley/arrt-player#7, brookstalley/arrt-player#8, brookstalley/arrt-player#13, brookstalley/arrt-player#14]
---
The fixes the 2026-11-19 audit found that need no ruling from the owner and no
change to the contract package, built as the eleven chunks of
`build-plan-audit-wave-1.md`.

**The television.** Every write to the set (upload, delete, brightness, the
slideshow switch-off, as well as selection) is sent through one gate in the seam,
which reads the set immediately before it and holds the write unless the set is in
art mode, or dark. A verb added later inherits the read. The held writes are
retried on one shared wait, and the websocket's token no longer reaches the journal
or the heartbeat's `last_error`.

**The pull.** An ETag is sent only for a document that parses. A master that fails
for a reason that may clear holds the feed for at most ten minutes, after which the
feed is cached without it and asked for again on every poll. Masters are streamed
under a running hash and stopped at the bytes the feed declares, and the client and
label documents are read to 1 MiB. The wall heartbeat goes on its own loop, so a
failing master no longer delays it.

**The walls.** A composed picture a power loss left empty or cut short is found and
composed again. A wall the server takes off an HDMI connector draws it black, a
client matches its walls again when its outputs change, and a Player that is
stopping leaves its screens as they are. The label holds its caption on the
monotonic clock, captions from a cached document across a restart, and shutdown
waits for a draw still in the driver before powering the panel down.

**The settings page.** It answers only to its own names (a 421 for any other `Host`;
`UI_ALLOWED_HOSTS` lists more), throttles wrong passwords per source address under a
ceiling, refuses a wall id that names a file of the cache, and says the server's
words ("Server address", "Token").

**The unit and CI.** `display.service` starts frozen and without the dev group,
bounds memory (`MemoryHigh=60%`, `MemoryMax=75%`) and is sandboxed as far as the
checkout's ownership allows; mpv starts with an environment of five names. CI pins
its actions by commit, uv and Python by version, and every `uv` call `--locked`; the
leak guard scans every file the repository can commit and finds the shapes that
leaked.

**The words.** The old "plane" wording is gone from the code, the unit, the tools
and the records; a Player that cannot start says "Arrt Player cannot start". The
architecture, data model, observability, security, NFR, HDMI, operational and
television records say what the code now does.

## Consumer note
An operator upgrading: an `.env` that holds a `*_SECONDS` of 0, below 0, `nan` or `inf`
(or a latitude or longitude out of range) now stops the Player at start with exit
2; the runbook's step 7b greps for it. A name only the router's DNS gives the
Player must be listed in `UI_ALLOWED_HOSTS`, or the settings page refuses it
(step 7c). The unit changes, so steps 9–11 install it, and step 14a checks that it
starts, draws and has its memory bound. None of the hardened unit has run under
systemd yet.
