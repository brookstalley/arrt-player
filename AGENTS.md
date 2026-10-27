<!-- PRAWDUCT:ANCHOR — governance pointer managed by the prawduct plugin; keep it small and version-free. -->

## Governance (Prawduct)

This repo is governed by **Prawduct**, loaded through your agent host's
Prawduct integration.

**Check first: is that integration loaded?** If not, **governance is OFF** — no
Stop gate, no Critic, nothing below enforced. Tell the user; don't proceed as if
governed. If you can't tell, assume it isn't.

- **Claude Code:** loaded when `/prawduct:*` commands are available; if not, run
  `claude plugin install prawduct@prawduct` and restart.

**With it loaded — before writing any code, STOP and read the build cycle**
(Claude Code: `/prawduct:methodology building`). Skipping it is the #1 governance
failure.

Hardest rules:

- **Tests are contracts** — fix the code, never weaken a test.
- **No "pre-existing" exception** — fix what you find, or flag why you can't.
  The fix half is bounded to BLOCKING; below it, the flag is the whole answer.
- **Never silently drop a requirement** — say so explicitly.
- **Run the Critic after medium+ work** — never write findings yourself; the
  independence is the value. Rigor is stage-keyed: a mid-build review blocks
  only on what would ship broken, the review at the merge boundary runs
  everything and is never skipped, and unsure defaults to the cheaper mid-build
  review.

**Enforcement is structural — while the integration is loaded:** its Stop hook
runs at session end and **blocks** if code changed against an active build plan
with no Critic findings.
