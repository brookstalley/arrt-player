# Owner Rulings

<!-- The owner's own words, each time they decided something about this product, newest first.
     Each entry says where the decision landed; the governing text lives there.
     Format and purpose: prawduct's templates/owner-rulings.md. -->

**2026-11-13.** "let's push and deploy to the pi", then "yeah we need to get
everything pushed to develop, released to main, then to the pi". The slideshow
branch merges into develop, develop is released to main as v0.8.0, and the Pi is
moved to that release. Chunk 05's Pi acceptance runs at that deploy, after the
release rather than before the PR. Landed as the disposition of R-2 in review
`rev-20261113T204743Z-fdd90287` and in `release-plan-v0.8.0.md`.

**2026-11-11, later.** "yes to all of your recommednations", on the norm registry
put to them in one batch. Confirmed: (A) the Enforcement table carried from Arrt on
2026-10-26 is this repo's ratified registry, with Arrt's Direction sections as the
home of its pointer rows; (B) the Player is stopped with SIGTERM, never SIGKILL;
(C) a contract change is a pull request and tag on Arrt, and the installed package
is never patched; (D) every log line carries its event as a field; (F) everything
the Player writes is its own account's alone. As recommended: (E) "absence checks
read the formatted journal line" is a learnings rule, not a norm; (G) "mpv is handed
files, never addresses" folds into the only-channel norm; (H) "one mode, never both"
stays a requirement in `build-plan-slideshow.md` § Goals, not a norm. Landed in
`project-preferences.md` § Enforcement (each on the branch carrying its code) and
`project-state.yaml` `norm_registry_ratified`.

**2026-11-11.** Six answers, given to questions put in one batch:

1. "yes": file the intermittent `test_availability` failure as a backlog item.
2. "yes please on both": fix backlog #3 (TV token file world-readable) and #4
   (stale `manifest.etag` survives an ETag-less adoption), off the slideshow branch.
3. "keep": `build-plan-slideshow.md` stays at its size, over the 40KB nudge.
   Landed as the dismissal of advisory `governance-oversized-build-plan-v1-8eb924`.
4. "yes add": add the unloaded-plugin tripwire hook to the committed `.claude/settings.json`.
5. "yes write down": collect the standing rules this product follows into `## Direction`
   sections, asking the owner where a real rule cannot be told from a passing habit.
6. "yes run read only check": the norm health sweep, reporting only.
