---
schema: 1
type: chore
scope: stand-alone
date: 2026-10-26
title: The Player stands alone, out of Arrt's repository
---
Arrt's split plan, chunk 04: the Player is carried out of Arrt's repository into
its own, with its history.

### What came with it
- The contract, taken from Arrt's released tag `contract-v1.0.0` and never from a copy.
- The Player's own `.env.example` and README.
- `deploy/`: `display.service` naming a standalone checkout at `/opt/arrt-player`
  with its `.env` at the root, the Pi's deployment record, and the guards on the unit.
- CI (`suites.yml`: the suite, the typesetting job and lint), each suite behind
  `assert_tests_ran.py`. The scope guard derives each leg's ignores, and the two
  workflow scans share one pattern.
- Governance: the norm index re-pointed at the Player's own guards, its findings,
  learnings and the Pi move runbook. The runbook now drives the standalone checkout
  and has a one-time move off the monorepo checkout.

### Waiting on the owner
The `/opt/arrt-player` path needs confirming before the Pi moves (Arrt's chunk 06).

## Consumer note
none: the repository moved and the running Player did not; the Pi is unchanged until it moves.
