"""A section cited by a bare artifact name resolves in this repository's copy.

Six artifact names exist both here and in Arrt (`CLAUDE.md` § Where artifacts
live): Arrt's hold the norms, these describe the Player's code. The convention is
that a citation meaning Arrt's copy says "Arrt's", so a bare name means this
repository's. Prose stating that convention keeps nothing apart, so this checks
the one part of it a test can reach: **a bare `<name>.md` § <section> must name a
heading in `.prawduct/artifacts/<name>.md` here.** A citation that lost its
"Arrt's" almost always fails that, because the norms' sections (§ Direction, § The
television belongs to whoever is using it) are not headings here.

What it cannot reach: a bare name with no section; a section cited from Arrt's
copy, which is not in this repository to resolve against; and a heading both
copies inherited from the same template (§ Performance, § Availability,
§ Configuration and the like), which resolves here whichever copy was meant. A
section here never takes the title of one of Arrt's norms, for that reason: this
copy's "Leaving the television to whoever is using it" was "The television
belongs to whoever is using it" until 2026-10-27, and a citation of the norm
that lost its "Arrt's" resolved to it.
"""

import pathlib
import re
import subprocess

PROJECT = pathlib.Path(__file__).resolve().parents[1]

#: The names both repositories carry. A name only Arrt has needs no qualifier.
SHARED_NAMES = (
    "architecture",
    "data-model",
    "security-model",
    "nonfunctional-requirements",
    "operational-spec",
    "observability-strategy",
)

_CITATION = re.compile(r"`(" + "|".join(SHARED_NAMES) + r")\.md`\s*§\s*\"?([^\n]*)")
#: Comment and quote markers a qualifier may sit behind when a line wraps.
_LINE_MARKERS = re.compile(r"\n\s*(?:#:?|>|\*|-|\|)?\s*")
_HEADING = re.compile(r"^#+\s+(.+?)\s*$", re.MULTILINE)


def _qualified_as_arrts(text: str, start: int) -> bool:
    before = _LINE_MARKERS.sub(" ", text[max(0, start - 80) : start]).rstrip(' (*"')
    return before.endswith("Arrt's")


def _resolves(cited: str, headings: list[str]) -> bool:
    """The heading is a prefix of the citation, or a wrapped citation of the heading."""
    fragment = re.split(r"[.,;:)|`\"]", cited, maxsplit=1)[0].strip()
    return any(cited.startswith(h) or (fragment and h.startswith(fragment)) for h in headings)


def _bare_citations() -> list[tuple[str, int, str, str]]:
    tracked = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 -- git from PATH, as every guard here runs it
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=True,
    )
    found = []
    for name in tracked.stdout.split():
        path = PROJECT / name
        if path.suffix not in {".md", ".py", ".yml", ".yaml", ".toml", ".service"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        found.extend(
            (name, text.count("\n", 0, match.start()) + 1, match.group(1), match.group(2))
            for match in _CITATION.finditer(text)
            if not _qualified_as_arrts(text, match.start())
        )
    return found


def _headings(artifact: str) -> list[str]:
    path = PROJECT / ".prawduct" / "artifacts" / f"{artifact}.md"
    return [h.strip("*") for h in _HEADING.findall(path.read_text(encoding="utf-8"))] if path.is_file() else []


def test_every_bare_section_citation_resolves_here():
    unresolved = [
        f"{name}:{line}: `{artifact}.md` § {cited}"
        for name, line, artifact, cited in _bare_citations()
        if not _resolves(cited, _headings(artifact))
    ]
    assert not unresolved, (
        "a bare citation names a section this repository's copy does not have; "
        "if it means Arrt's, say \"Arrt's\":\n" + "\n".join(unresolved)
    )


def test_the_scan_finds_bare_citations():
    """Guards the guard: a scan that matched nothing would pass forever."""
    assert any(artifact == "observability-strategy" for _, _, artifact, _ in _bare_citations())


def test_a_qualifier_behind_a_wrapped_comment_still_counts():
    # Assembled, so the scan over this file does not read the fixture as a citation.
    citation = "`architecture" + ".md` § Direction"
    text = f"# held by Arrt's\n# {citation}"
    assert _qualified_as_arrts(text, text.index("`"))
    text = f"# held by Arrt\n# {citation}"
    assert not _qualified_as_arrts(text, text.index("`"))
