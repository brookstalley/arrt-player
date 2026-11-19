"""The one pattern that says a workflow line runs pytest.

Both `test_assert_tests_ran.py` and `test_ci_scope.py` scan the workflows for it.
Two copies of a pattern kept in step by a comment drift the first time one is
fixed and the other is not, and either drifting skips a CI leg in silence.
"""

import re

#: `uv run`, anything uv itself takes, then `pytest`. **Whatever sits between is
#: the point.** Matching the literal `uv run pytest` skipped every `uv run --group
#: raster pytest …` in silence, and that is the typesetting job: the one leg whose
#: suite skips itself when its group is absent. A pattern spelling out which
#: options may appear skips the next option it does not know in the same silence,
#: so nothing between the two words is constrained.
PYTEST_INVOCATION = re.compile(r"\buv run\b.*?\bpytest\b")


#: A step's `uses:` line, with the reference and whatever comment trails it.
_USES = re.compile(r"^(?P<indent>\s*)-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")

#: What a pinned reference ends in: `@`, a full 40-hex commit, nothing after.
_COMMIT_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")

#: A trailing comment that names the tag the commit was resolved from.
_TAG_COMMENT = re.compile(r"^\s+#\s*v?\d")

#: `uv run` and `uv sync`, the two calls that read `uv.lock` and may rewrite it.
_UV_LOCK_READER = re.compile(r"\buv (?:run|sync)\b")

#: An exact `x.y.z`, quoted or not, and nothing else on the line.
_EXACT_VERSION = r'"?\d+\.\d+\.\d+"?\s*(?:#.*)?'


def _live_lines(text: str) -> list[tuple[int, str]]:
    """The workflow's lines that are not comments, numbered from 1, backslash continuations joined.

    A comment in these workflows says `uv sync` and `uses:` in prose, and a scan
    that reads it reports a clean tree as a dirty one or, worse, reads a pinned
    comment as the pin.
    """
    lines: list[tuple[int, str]] = []
    start = 0
    joined = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        if not joined and raw.strip().startswith("#"):
            continue
        if not joined:
            start = number
        joined = f"{joined} {raw.strip()}" if joined else raw
        if joined.rstrip().endswith("\\"):
            joined = joined.rstrip().removesuffix("\\")
            continue
        lines.append((start, joined))
        joined = ""
    return lines


def unpinned_actions(text: str) -> list[str]:
    """Every `uses:` that does not name a full commit followed by a comment naming its tag."""
    problems = []
    for number, line in _live_lines(text):
        found = _USES.match(line)
        if not found or found["ref"].startswith("./"):
            continue
        if not _COMMIT_PINNED.match(found["ref"]):
            problems.append(f"line {number}: {found['ref']} is not pinned to a 40-hex commit")
        elif not _TAG_COMMENT.match(found["rest"]):
            problems.append(f"line {number}: {found['ref']} names no tag in a trailing comment")
    return problems


def unlocked_uv_calls(text: str) -> list[str]:
    """Every `uv run` and `uv sync` in a workflow line that does not say `--locked`."""
    return [
        f"line {number}: {line.strip()}"
        for number, line in _live_lines(text)
        if _UV_LOCK_READER.search(line) and "--locked" not in line
    ]


def _step_body(lines: list[tuple[int, str]], start: int) -> list[str]:
    """The lines of the step whose `uses:` is `lines[start]`, up to the next step or the end of the job."""
    first = lines[start][1]
    indent = len(first) - len(first.lstrip())
    if not first.lstrip().startswith("-"):
        indent -= 2  # `uses:` is a later key of its step, whose dash sits two columns left
    body = []
    for _, line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return body


def unpinned_setup_uv(text: str) -> list[str]:
    """Every `setup-uv` step that leaves the uv version or the Python version to a default."""
    lines = _live_lines(text)
    problems: list[str] = []
    for index, (number, line) in enumerate(lines):
        found = _USES.match(line)
        if not found or not found["ref"].startswith("astral-sh/setup-uv@"):
            continue
        body = _step_body(lines, index)
        problems.extend(
            f"line {number}: setup-uv sets no exact `{key}:`"
            for key in ("version", "python-version")
            if not any(re.fullmatch(rf"\s+{key}:\s*{_EXACT_VERSION}", body_line) for body_line in body)
        )
    return problems
