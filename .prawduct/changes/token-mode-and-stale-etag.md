---
schema: 1
type: fix
scope: token-mode-and-stale-etag
date: 2026-11-13
title: The television's token file is private, and a cached ETag never outlives its document
closes: [brookstalley/arrt-player#3, brookstalley/arrt-player#4]
---
Two defects that discovery filed, fixed on one branch.

### What changed
- **#3:** the television library wrote the pairing token with the process umask, so it was world-readable on the Pi and anyone on the Pi could use it to drive the set's art channel. `deploy/display.service` now sets `UMask=0077`, so everything the Player writes belongs to `tvpi` alone. `SamsungTv.connect` sets the token file to 0600 after every connection attempt, in a `finally`. A mode it cannot set is logged as `tv.token_file_open`. The Player never creates the file itself.
- **#4:** `Pull._adopt_feed` left the previous feed's `manifest.etag` behind after adopting a feed served with no ETag. When the server returned to the old feed, it answered that ETag with 304, and the wall stayed on a feed the server no longer served. The old ETag is now removed before the new manifest is written, and the new one is written after. A crash between the two writes therefore costs one full download instead of a stale ETag. `ClientPull` gets the same order.
- A test that passed with the token file's `FileNotFoundError` branch removed now asserts that no `tv.token_file_open` record appears.
- `project-preferences.md`: the norm that everything the Player writes belongs to its account alone, ratified by the owner 2026-11-11 and held by the unit's UMask test and `TestTheTokenFileIsPrivate`.
- Records: `security-model.md`, `observability-strategy.md`, `data-model.md` and `deploy/README.md` § The television's token is private to `tvpi`.

## Consumer note
On the Pi, install the updated `deploy/display.service` as the runbook says, then run `systemctl daemon-reload`. After the Player's next connection to the set, the token file reads mode 600, and nothing is re-paired.
