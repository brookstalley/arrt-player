# Owner Rulings

<!-- The owner's own words, each time they decided something about this product, newest first.
     Each entry says where the decision landed; the governing text lives there.
     Format and purpose: prawduct's templates/owner-rulings.md. -->

**2026-11-19, during wave 2 of the audit fixes.** Recorded in full in Arrt's `owner-rulings.md` under the same date ("during wave
2 of the audit fixes, two rulings"). The one that is the Player's:

- **Arrt Player removes from a television only the pictures it recorded uploading.** The
  owner: "yes to recommendations", to the advisor's recommendation of exactly that, for a
  later wave. Until it is built the Player clears every uploaded picture it cannot account
  for on a set it meets fresh, which is the recorded design (`security-model.md`). Built by
  `build-plan-audit-wave-3.md`, whose decision on what happens to pictures an older Player
  uploaded and never recorded is the owner's to veto before any deploy.

**2026-11-19, after wave 1 of the audit fixes.** The full set is in Arrt's `owner-rulings.md` under the same date. The two that are
the Player's:

- **Not now: the Player's code owned by root with `ProtectSystem=strict`.** "That's
  just too heavy for right now. We may do that eventually, but not yet." The
  sandboxing `build-plan-audit-wave-1.md` added stands as it is.
- **One label panel per Player is the recorded limit for now, and nothing may be
  designed that closes off several.** "I'm fine recording one label panel per player
  right now, but we shouldn't design anything that precludes having multiple labels,
  just as we don't prevent having multiple displays per player. In the future, there
  may be players that only do labels and might have multiple. We don't have to build
  that right now, but let's make sure we aren't closing off that path." A label is an
  output a Player has zero or more of, as a display is, and a Player with labels and
  no display must stay describable; what is built today may assume one.

Also confirmed: `arrt.local` is an example name; a guesser with two addresses can
hold a device that has never signed in out of the settings page for as long as the
guessing continues.

**2026-11-13, at the set.** "yep looks great", on the slideshow playing on the LG at 4K
with captions on the panel, then "all good" after switching back to Arrt from the page.
That answer also confirmed the three readings put to the owner with it: a still shows
for the minimum dwell; video pages wait for their own plan (wave 2b); and the
slideshow drives one output. Landed in `hdmi-output-findings.md` § The slideshow on
the Pi: its acceptance, and in `build-plan-slideshow.md` § Goals (no longer
*Inferred*).

**2026-11-13, later.** "yes prawduct should pull from testament", on whether the
declared `test_command` should run on the testament pool. Landed as `test_command`
in `.prawduct/project-state.yaml`.

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
