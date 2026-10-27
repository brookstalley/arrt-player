"""What the default CI leg may leave out of its collection, and what it may not.

Locally `uv run --group raster pytest` collects everything. In CI a default
`uv sync` installs no optional group, so a module that `importorskip`s its group
lands a skip in the report, and `assert_tests_ran.py` reads any skip as a
provisioning failure and fails the job. So the leg names those directories with
`--ignore`, and a job of its own installs the group and runs them.

**That list goes stale silently in both directions, which is why it is derived
here rather than trusted.** Too few, and CI goes red on a passing suite blaming
an install that is fine. Too many, and a directory stops running in CI with
nothing to show for it — which reads exactly like a directory that passes. And
an ignored directory no other job runs is coverage that has gone entirely.

Carried from Arrt's own guard over the workflow both products shared, when Arrt
Player moved into a repository of its own.
"""

import pathlib
import re

import pytest

PROJECT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT / ".github" / "workflows" / "suites.yml"
TESTS = PROJECT / "tests"

#: The job that runs the default suite.
DEFAULT_LEG = "suite"


def _directories_that_import_or_skip(tests_root: pathlib.Path) -> set[str]:
    """Top-level directories under `tests_root` holding a module that may not import.

    A module-scope `importorskip` call is the construct that turns an absent
    optional group into a collection-time skip. Matched as the call, name and
    open parenthesis, not the bare word: this module and the guard it is about
    discuss the construct in prose, and a guard that counts its own docstring
    fails on a healthy tree.
    """
    found = set()
    for path in tests_root.rglob("*.py"):
        if re.search(r"\bimportorskip\s*\(", path.read_text(encoding="utf-8")):
            found.add(path.relative_to(tests_root).parts[0])
    return found


#: A line opening a job: two spaces, a name, a colon, nothing else.
_JOB = re.compile(r"^  ([A-Za-z][\w-]*):\s*$")


def _pytest_invocations_by_job() -> dict[str, list[str]]:
    """Every `uv run … pytest …` command in the workflow, under the job that runs it.

    Text rather than a YAML parse: nothing else here reads YAML, and a dependency
    added to read a handful of lines would cost more than it protects.
    """
    invocations: dict[str, list[str]] = {}
    job = None
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        opening = _JOB.match(line)
        if opening:
            job = opening.group(1)
        elif job and not line.strip().startswith("#") and re.search(r"uv run .*\bpytest\b", line):
            invocations.setdefault(job, []).append(line.strip())
    return invocations


def _ignored_by(job: str) -> set[str]:
    """The directory names that job's pytest invocation excludes."""
    invocations = _pytest_invocations_by_job()
    assert job in invocations, (
        f"no `uv run … pytest` step under a job named {job!r} in suites.yml — either the job was "
        "renamed, or the suite it ran is no longer run in CI at all"
    )
    assert len(invocations[job]) == 1, (
        f"the {job!r} job runs pytest {len(invocations[job])} times: {invocations[job]}. This reads one "
        "invocation per job, so the two cannot cover for each other's gaps."
    )
    return {match.removeprefix("tests/") for match in re.findall(r"--ignore=(\S+)", invocations[job][0])}


def check_ignores(ignored: set[str], needed: set[str]) -> None:
    """Raise unless the two sets agree, naming which direction they disagree in."""
    missing = needed - ignored
    extra = ignored - needed
    assert not missing, (
        f"the default CI leg does not ignore {sorted(missing)}, whose modules cannot import without "
        "their optional group. In CI they land collection skips, and assert_tests_ran.py fails the "
        "job reporting a provisioning fault against a suite that is fine."
    )
    assert not extra, (
        f"the default CI leg ignores {sorted(extra)}, which import cleanly and need no exclusion. Those "
        "directories do not run in CI at all, and report nothing while not running — which is "
        "indistinguishable from passing."
    )


def test_the_default_leg_ignores_exactly_the_directories_that_cannot_import():
    """Derived on both sides, so neither over- nor under-exclusion can sit unnoticed."""
    needed = _directories_that_import_or_skip(TESTS)
    assert needed, "no directory under tests/ uses importorskip — the default CI leg should carry no --ignore at all"
    check_ignores(_ignored_by(DEFAULT_LEG), needed)


def test_a_missing_ignore_is_rejected():
    """Under-exclusion: the job goes red on a healthy suite, blaming a phantom install."""
    with pytest.raises(AssertionError, match="cannot import"):
        check_ignores(ignored={"raster"}, needed={"raster", "panel"})


def test_an_unnecessary_ignore_is_rejected():
    """Over-exclusion: a whole directory stops running in CI and nothing says so.

    The half the real tree cannot demonstrate while its list is correct, so it is
    fabricated here rather than left to a comparison nobody can see fail.
    """
    with pytest.raises(AssertionError, match="import cleanly and need no exclusion"):
        check_ignores(ignored={"raster", "fakes"}, needed={"raster"})


def test_every_directory_the_default_leg_ignores_is_run_by_another_job():
    """Ignoring a directory keeps a green leg honest; it does not keep the code tested.

    A `--ignore` added because a group is optional, with no job installing that
    group, removes the tests from CI entirely. Here that is `tests/raster`, run
    by the typesetting job with the group installed and `assert_tests_ran.py`
    behind it.
    """
    elsewhere = {
        path
        for job, invocations in _pytest_invocations_by_job().items()
        if job != DEFAULT_LEG
        for invocation in invocations
        # A directory another job also excludes is not one it runs, so the
        # `--ignore=` arguments come out before the paths are read.
        for path in re.findall(r"(?<![=\w/])tests/\S+", re.sub(r"--ignore=\S+", "", invocation))
    }
    unrun = {f"tests/{name}" for name in _ignored_by(DEFAULT_LEG)} - elsewhere
    assert not unrun, (
        f"the default leg ignores {sorted(unrun)} and no other job in suites.yml runs it, so those "
        "tests do not execute in CI at all — which is indistinguishable from their passing"
    )
