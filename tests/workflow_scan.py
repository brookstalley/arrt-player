"""What the workflow guards read the CI files for: the patterns, and the readers built on them.

Five things live here, each used by `test_ci_scope.py` and one by
`test_assert_tests_ran.py` too:

- `PYTEST_INVOCATION`, the one pattern that says a workflow line runs pytest
  (both tests use it);
- `unpinned_actions`, every `uses:` that is not a full commit with its tag in a
  comment (`_USES`, `_COMMIT_PINNED`, `_TAG_COMMENT`);
- `unpinned_setup_uv`, every `setup-uv` step that leaves the uv or the Python to a
  default (`_EXACT_VERSION`);
- `unlocked_uv_calls`, every `uv run` or `uv sync` without `--locked`
  (`_UV_LOCK_READER`);
- `dependabot_problems`, what `.github/dependabot.yml` may say.

Two copies of a pattern kept in step by a comment drift the first time one is
fixed and the other is not, and either drifting skips a CI leg in silence. So each
is written once, here, and the tests import it.
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


def _yaml_rows(text: str) -> list[tuple[int, str]]:
    """The lines that say something, as (indent, content): comments and blank lines are gone."""
    rows = []
    for raw in text.splitlines():
        content = "" if raw.lstrip().startswith("#") else raw.split(" #", 1)[0].rstrip()
        if content.strip():
            rows.append((len(content) - len(content.lstrip()), content.strip()))
    return rows


def _yaml_scalar(value: str) -> object:
    value = value.strip()
    if value[:1] in "[{&*|>!":
        raise ValueError(f"{value!r} is outside the YAML subset this reader holds")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return int(value) if value.isdigit() else value


def _yaml_block(rows: list[tuple[int, str]], position: int, indent: int) -> tuple[object, int]:
    """The list or mapping whose lines start at `rows[position]`, at `indent`, and where it ends."""
    if rows[position][1].startswith("- "):
        items: list[object] = []
        while position < len(rows) and rows[position][0] == indent and rows[position][1].startswith("- "):
            # The dash's own key is the first key of a mapping two columns deeper.
            rows[position] = (indent + 2, rows[position][1][2:].strip())
            item, position = _yaml_block(rows, position, indent + 2)
            items.append(item)
        return items, position
    mapping: dict[str, object] = {}
    while position < len(rows) and rows[position][0] == indent:
        key, _, value = rows[position][1].partition(":")
        position += 1
        if value.strip():
            mapping[key.strip()] = _yaml_scalar(value)
        elif position < len(rows) and rows[position][0] > indent:
            mapping[key.strip()], position = _yaml_block(rows, position, rows[position][0])
        else:
            mapping[key.strip()] = None
    return mapping, position


def _read_mapping_tree(text: str) -> object:
    """A small reader for the YAML subset a Dependabot file uses: mappings, lists of mappings, scalars.

    No YAML library is a dependency of this project, and the file is short. Anything
    outside the subset (flow collections, anchors, block scalars) is refused rather
    than read wrongly, so a file this cannot read fails the test that holds it.
    """
    rows = _yaml_rows(text)
    if not rows:
        return {}
    tree, end = _yaml_block(rows, 0, rows[0][0])
    if end != len(rows):
        raise ValueError(f"line {end + 1} of the file is not where the reader expected the structure to continue")
    return tree


#: The one thing Dependabot watches here, and how often. Everything else is a hand step.
_DEPENDABOT_ECOSYSTEM = "github-actions"
_DEPENDABOT_INTERVAL = "monthly"


def _dependabot_entry_problems(index: int, entry: object) -> list[str]:
    if not isinstance(entry, dict):
        return [f"update {index} is not a mapping"]
    problems = []
    ecosystem = entry.get("package-ecosystem")
    if ecosystem != _DEPENDABOT_ECOSYSTEM:
        problems.append(
            f"update {index} watches {ecosystem!r}; only {_DEPENDABOT_ECOSYSTEM!r} is watched, "
            "and `uv.lock` stays a hand step (a lock update moves the Waveshare pin and the contract's git source)"
        )
    if entry.get("directory") != "/":
        problems.append(
            f"update {index} has `directory:` {entry.get('directory')!r}, not '/' (where the workflows are read from)"
        )
    schedule = entry.get("schedule")
    interval = schedule.get("interval") if isinstance(schedule, dict) else None
    if interval != _DEPENDABOT_INTERVAL:
        problems.append(f"update {index} has `schedule.interval:` {interval!r}, not {_DEPENDABOT_INTERVAL!r}")
    return problems


def dependabot_problems(text: str) -> list[str]:
    """What is wrong with a `dependabot.yml`: anything but GitHub Actions watched monthly from the root."""
    try:
        tree = _read_mapping_tree(text)
    except ValueError as exc:
        return [f"the file could not be read: {exc}"]
    if not isinstance(tree, dict):
        return ["the file is not a mapping"]
    problems = [] if tree.get("version") == 2 else [f"`version:` is {tree.get('version')!r}, not 2"]
    updates = tree.get("updates")
    if not isinstance(updates, list) or not updates:
        return [*problems, "`updates:` lists nothing, so no action is watched"]
    for index, entry in enumerate(updates, start=1):
        problems.extend(_dependabot_entry_problems(index, entry))
    if not any(isinstance(entry, dict) and entry.get("package-ecosystem") == _DEPENDABOT_ECOSYSTEM for entry in updates):
        problems.append(f"no update watches {_DEPENDABOT_ECOSYSTEM!r}, so the pinned actions never learn their tag moved")
    return problems
