"""Every lint waiver says why, in one form.

A per-line `noqa` is how this repository records a deliberate exception to a rule
it otherwise holds everywhere (the owner's 2026-10-05 ruling to select every rule
ruff has). A waiver without its reason is an exception nobody can audit: it reads
the same whether it was argued for or copied from the line above. The form is the
one the logging row in `project-preferences.md` specifies, `# noqa: CODES --
reason`, or a `noqa` paired with a prawduct pragma, which carries its own reason.

Comments are found with the tokenizer, so a docstring that mentions `# noqa` is
prose and not a waiver.

Carried from Arrt's repository-wide guard, which walked every tracked file of the
repository both products shared, when Arrt Player moved into a repository of its
own.
"""

import io
import pathlib
import re
import subprocess
import tokenize

_REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]

_REASONED = re.compile(r"# noqa: [A-Z]+[0-9]+(?:, [A-Z]+[0-9]+)*(?: -- \S|  # prawduct:allow \S+ -- \S)")


def _python_files() -> list[pathlib.Path]:
    names = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],  # noqa: S607 -- git from PATH, as every developer and CI runs it
        cwd=_REPOSITORY_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split("\0")
    files = [_REPOSITORY_ROOT / name for name in names if name]
    # Keyed on identity rather than a count: a scan that misses this module or the
    # daemon's entry point is reading the wrong tree, or none, and would pass vacuously.
    for expected in (pathlib.Path(__file__).resolve(), _REPOSITORY_ROOT / "src" / "arrt_player" / "__main__.py"):
        assert expected in files, f"git ls-files did not list {expected}; the check would be vacuous"
    return files


def unreasoned_waivers(source: str) -> list[int]:
    """The line numbers of every `noqa` comment not in the reasoned form."""
    return [
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and "noqa" in token.string and not _REASONED.search(token.string)
    ]


def test_every_waiver_says_why():
    offenders = [
        f"{path.relative_to(_REPOSITORY_ROOT)}:{line}"
        for path in _python_files()
        for line in unreasoned_waivers(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, "waivers without a reason (`# noqa: CODE -- why`):\n" + "\n".join(offenders)


def test_the_check_knows_a_reason_from_its_absence():
    assert unreasoned_waivers("x = 1  # noqa: E501\n") == [1]
    assert unreasoned_waivers("x = 1  # noqa: E501 - single dash\n") == [1]
    assert unreasoned_waivers("x = 1  # noqa\n") == [1]
    assert unreasoned_waivers("x = 1  # noqa: E501 -- the URL cannot wrap\n") == []
    assert unreasoned_waivers("x = 1  # noqa: BLE001, S110 -- both\n") == []
    assert (
        unreasoned_waivers(
            "try:\n    pass\nexcept Exception:  # noqa: BLE001  # prawduct:allow prawduct/broad-except -- why\n    pass\n"
        )
        == []
    )
    assert unreasoned_waivers('"""Mentions # noqa in prose."""\n') == []
