---
schema: 1
type: docs
scope: discovery
date: 2026-10-27
title: The Player's classification and its own strategy artifacts
---
Discovery in reconciliation mode for the Player as its own product, plus the fixes
the work turned up.

### What changed
- `project-state.yaml`: the classification and product definition, drafted from the
  repo and Arrt's record and confirmed by the owner on 2026-10-27. v1 is what runs today.
- Six strategy artifacts in `.prawduct/artifacts/` (architecture, data model, security
  model, non-functional requirements, operational spec, observability), describing the
  Player's code and pointing at Arrt's same-named artifacts for the norms. They ratify
  nothing, and each lists where Arrt's record is now wrong about the Player.
- Every artifact name here also exists in Arrt, so citations meaning Arrt's copy say
  "Arrt's", `CLAUDE.md` states the convention, and `tests/test_artifact_citations.py`
  holds a bare `<name>.md` § section to a heading in this repository's copy.
- `tv.close_failed` logs at WARNING; at DEBUG under an INFO root it never reached the journal.
- `tools/tv_api_check.py --image` selects its image only on a set reporting art mode `on`;
  it switched a set showing a programme to art while its docstring said it did not.
- The stand-alone PR's review notes: the owner-confirmed `/opt/arrt-player` path, the
  runbook's F7 naming F9, the suite-job comment, and the empty `backlog.md` removed
  (the backlog is GitHub Issues).
- The defects the drafting surfaced are filed as issues #2–#14; none is closed here.

## Consumer note
none: documentation and governance records, a hand-run tool's safety check, and one log level; the running Player's behaviour is unchanged apart from `tv.close_failed` now appearing in the journal.
