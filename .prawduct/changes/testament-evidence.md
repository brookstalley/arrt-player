---
schema: 1
type: chore
scope: testament-evidence
date: 2026-11-13
title: Test evidence is recorded from the testament pool, in one run
---
The declared `test_command` in `.prawduct/project-state.yaml` is now
`testament run --junit-xml {junit_xml}` (owner, 2026-11-13). Before this, a session ran the suite on
the pool as `AGENTS.md` says, and then `prawduct-hook test-evidence record` ran it again locally
through pytest. The suite and its `--group raster` are unchanged, and `.testament.toml` carries them.
With no coordinator answering, testament runs that same command on the machine itself.

## Consumer note
none: how the repo records its own test evidence; nothing a Pi or a server runs changes.
