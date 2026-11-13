---
paths:
  - "pyproject.toml"
  - "uv.lock"
  - ".github/**"
---
# Learnings — tooling

- Run an autofixer only under the project's real rule set, never `--select` with RUF100 in it — because a narrowed selection makes every other rule's `noqa` look unused, and the fix deletes them all.
- Stage new files before recording suite evidence — because guards that walk `git ls-files` (the waiver check among them) skip untracked files, so a green local run can miss what CI then refuses.
- Gate a commit on the check's own exit status, never on a `;` chain or a pipe into a reader — because both report the last command's status, so a failed lint or test run reads as success and the commit lands.
