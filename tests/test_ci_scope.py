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
from workflow_scan import PYTEST_INVOCATION, dependabot_problems, unlocked_uv_calls, unpinned_actions, unpinned_setup_uv

PROJECT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT / ".github" / "workflows" / "suites.yml"
DEPENDABOT = PROJECT / ".github" / "dependabot.yml"
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
        elif job and not line.strip().startswith("#") and PYTEST_INVOCATION.search(line):
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


# --- The workflow is pinned: what runs in CI is named, not whatever is newest today. ---
#
# A floating action tag, an unpinned uv and a lockfile `uv run` may rewrite are the
# same fault three times: CI's verdict depends on something that moved while nobody
# looked, so a green run and a red one can differ by nothing in this repository.

_WORKFLOW_TEXT = WORKFLOW.read_text(encoding="utf-8")

_PINNED_CHECKOUT = "      - uses: actions/checkout@" + "a" * 40 + "  # v4\n"
_PINNED_UV = (
    "      - uses: astral-sh/setup-uv@" + "b" * 40 + "  # v5\n"
    "        with:\n"
    '          version: "0.11.32"\n'
    '          python-version: "3.13.5"\n'
)


def test_the_workflow_uses_actions_the_scans_below_can_see():
    """The scans are over `uses:` lines and a setup-uv step; a workflow with none would pass them all."""
    found = [line for line in _WORKFLOW_TEXT.splitlines() if re.match(r"\s*-?\s*uses:", line)]
    assert len(found) >= 6, f"expected checkout and setup-uv in each of three jobs, found {found}"
    assert sum("setup-uv" in line for line in found) >= 3


def test_every_action_is_pinned_to_a_commit_with_its_tag_in_a_comment():
    problems = unpinned_actions(_WORKFLOW_TEXT)
    assert problems == [], "\n".join(problems)


def test_every_setup_uv_names_its_uv_and_its_python():
    problems = unpinned_setup_uv(_WORKFLOW_TEXT)
    assert problems == [], "\n".join(problems)


def test_every_uv_run_and_sync_in_the_workflow_is_locked():
    problems = unlocked_uv_calls(_WORKFLOW_TEXT)
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize(
    ("line", "complaint"),
    [
        ("      - uses: actions/checkout@v4\n", "not pinned to a 40-hex commit"),
        ("      - uses: actions/checkout@" + "a" * 39 + "\n", "not pinned to a 40-hex commit"),
        ("      - uses: actions/checkout@" + "A" * 40 + "  # v4\n", "not pinned to a 40-hex commit"),
        ("      - uses: actions/checkout@main\n", "not pinned to a 40-hex commit"),
        ("      - uses: actions/checkout@" + "a" * 40 + "\n", "names no tag"),
    ],
)
def test_an_action_not_pinned_by_commit_with_its_tag_is_rejected(line, complaint):
    problems = unpinned_actions("    steps:\n" + line)
    assert len(problems) == 1
    assert complaint in problems[0]


def test_a_pinned_action_and_a_comment_that_only_mentions_one_are_accepted():
    """A comment saying `uses: x@v4` in prose is not a step, and the pinned form is not rejected."""
    text = "      # uses: actions/checkout@v4 was here\n" + _PINNED_CHECKOUT + "      - uses: ./.github/actions/local\n"
    assert unpinned_actions(text) == []


@pytest.mark.parametrize(
    ("step", "key"),
    [
        ("      - uses: astral-sh/setup-uv@" + "b" * 40 + "  # v5\n        with:\n          enable-cache: true\n", "version"),
        (
            "      - uses: astral-sh/setup-uv@" + "b" * 40 + "  # v5\n        with:\n          version: latest\n"
            '          python-version: "3.13.5"\n',
            "version",
        ),
        (
            "      - uses: astral-sh/setup-uv@" + "b" * 40 + "  # v5\n        with:\n          version: 0.11.32\n",
            "python-version",
        ),
        (
            "      - uses: astral-sh/setup-uv@" + "b" * 40 + "  # v5\n        with:\n          version: 0.11.32\n"
            '          python-version: "3.13"\n',
            "python-version",
        ),
    ],
)
def test_a_setup_uv_without_an_exact_version_is_rejected(step, key):
    problems = unpinned_setup_uv("    steps:\n" + step + "      - run: true\n")
    assert any(f"`{key}:`" in problem for problem in problems), problems


def test_a_setup_uv_that_names_both_versions_is_accepted_even_beside_another_step():
    text = "    steps:\n" + _PINNED_CHECKOUT + _PINNED_UV + "      - name: next\n        run: uv run --locked pytest\n"
    assert unpinned_setup_uv(text) == []


def test_a_setup_uv_that_is_not_its_steps_first_key_is_still_read():
    """`name:` before `uses:` puts the step's dash two columns left of `uses:`; the body must still end at the next step."""
    text = (
        "    steps:\n      - name: uv\n        uses: astral-sh/setup-uv@"
        + "b" * 40
        + "  # v5\n        with:\n          version: 0.11.32\n          python-version: 3.13.5\n"
        + "      - name: elsewhere\n        with:\n          version: 9.9.9\n          python-version: 9.9.9\n"
    )
    assert unpinned_setup_uv(text) == []
    assert unpinned_setup_uv(text.replace("          version: 0.11.32\n", "")) != []


@pytest.mark.parametrize(
    "line",
    [
        "        run: uv sync\n",
        "        run: uv run pytest tests\n",
        "        run: uv run --group raster pytest tests/raster\n",
        "        run: |\n          uv run --locked ruff check .\n          uv run black --check .\n",
        "        run: uv run \\\n          pytest tests\n",
    ],
)
def test_a_uv_run_or_sync_without_locked_is_rejected(line):
    assert unlocked_uv_calls(line) != []


@pytest.mark.parametrize(
    "line",
    [
        "        run: uv sync --locked\n",
        "        run: uv run --locked pytest tests\n",
        "        run: uv run --group raster --locked pytest tests/raster\n",
        "        run: uv run \\\n          --locked pytest tests\n",
        "        # run: uv sync, said in a comment\n",
        "        run: python3 .github/scripts/assert_tests_ran.py results.xml\n",
    ],
)
def test_a_locked_uv_call_and_a_comment_that_only_mentions_one_are_accepted(line):
    assert unlocked_uv_calls(line) == []


# Dependabot is how a pin by commit learns that its tag moved. It watches the
# actions and nothing else: `uv.lock` stays a hand step (Goals, the Dependabot
# decision), so a second ecosystem added here would be a bot opening lock updates
# nobody exercised.

_ACTIONS_ONLY = (
    "version: 2\n"
    "updates:\n"
    '  - package-ecosystem: "github-actions"\n'
    '    directory: "/"\n'
    "    schedule:\n"
    '      interval: "monthly"\n'
)


def test_dependabot_watches_the_pinned_actions_monthly_and_nothing_else():
    assert DEPENDABOT.is_file(), ".github/dependabot.yml is missing, so a pinned action never learns its tag moved"
    problems = dependabot_problems(DEPENDABOT.read_text(encoding="utf-8"))
    assert problems == [], "\n".join(problems)


def test_there_are_actions_for_dependabot_to_watch():
    """The file is held to `github-actions` because the workflow has pinned actions; none would make it vacuous."""
    assert re.search(r"^\s*-?\s*uses:\s*[\w-]+/", _WORKFLOW_TEXT, re.MULTILINE), "no `uses:` in the workflow to watch"


def test_an_actions_only_monthly_file_is_accepted():
    assert dependabot_problems(_ACTIONS_ONLY) == []


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        (_ACTIONS_ONLY.replace("github-actions", "uv"), "'uv'"),
        (_ACTIONS_ONLY.replace("github-actions", "pip"), "'pip'"),
        (_ACTIONS_ONLY + '  - package-ecosystem: "uv"\n    directory: "/"\n    schedule:\n      interval: "monthly"\n', "'uv'"),
        (_ACTIONS_ONLY.replace("monthly", "weekly"), "'weekly', not 'monthly'"),
        (_ACTIONS_ONLY.replace("monthly", "daily"), "'daily', not 'monthly'"),
        (_ACTIONS_ONLY.replace('"/"', '"/src"'), "not '/'"),
        (_ACTIONS_ONLY.replace("version: 2", "version: 1"), "not 2"),
        ("version: 2\nupdates:\n", "lists nothing"),
        ("", "not 2"),
        (_ACTIONS_ONLY.replace('"monthly"', "{ interval: monthly }"), "could not be read"),
    ],
)
def test_a_dependabot_file_that_watches_more_or_less_often_or_less_is_rejected(text, complaint):
    problems = dependabot_problems(text)
    assert problems, f"accepted: {text!r}"
    assert any(complaint in problem for problem in problems), problems


def test_a_comment_in_the_dependabot_file_is_not_a_setting():
    text = "# interval: weekly\n" + _ACTIONS_ONLY.replace('"monthly"', '"monthly"  # not "weekly"')
    assert dependabot_problems(text) == []
