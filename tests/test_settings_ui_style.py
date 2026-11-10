"""The settings page's stylesheet, held to the rules Arrt holds its own to.

The page wears Arrt's look by copying its tokens (`settings_ui/static/settings.css`
says from where), and Arrt's `design-direction.md` § Direction is the rule
those tokens answer to there: every text pair clears WCAG 2.1 AA, every control
boundary 3:1, and no colour is written outside the token blocks. A copy is not
covered by Arrt's test, so this one reads this file the same way. The pairs
are the ones this page draws, in both schemes.
"""

import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from arrt_player.config import UiSettings
from arrt_player.settings_ui import auth
from arrt_player.settings_ui.server import STATIC_CACHE_CONTROL, STATIC_DIRECTORY, PageState, build

STYLESHEET = STATIC_DIRECTORY / "settings.css"

#: Text on the ground it sits on, held to 4.5:1. Each pair is one the page draws.
TEXT_PAIRS = [
    ("text-strong", "surface-0"),  # names, headings, labels
    ("text", "surface-0"),  # prose and facts
    ("text-muted", "surface-0"),  # hints and captions
    ("text", "surface-1"),  # what is typed in a field
    ("text-muted", "surface-2"),  # a field the environment pins
    ("text", "surface-2"),  # a note
    ("text-strong", "crit-quiet"),  # a problem
    ("accent-text", "accent"),  # the filled act's label
    ("text", "surface-1"),  # a quiet act's label
    ("good", "surface-0"),  # ● Running
    ("warn", "surface-0"),  # ▲ Setup mode
    ("accent", "surface-0"),  # a link
]

#: A control's edge against what it sits on, held to 3:1 (WCAG 1.4.11).
BOUNDARY_PAIRS = [
    ("border-strong", "surface-0"),
    ("border-strong", "surface-1"),
    ("accent", "surface-0"),  # the filled act has no border of its own colour
    ("focus", "surface-0"),
    ("crit", "crit-quiet"),  # a problem's rule
]


def _blocks(css: str) -> tuple[str, str, str]:
    """The light `:root`, the dark scheme's `:root`, and everything else."""
    light = re.search(r"^:root \{(.*?)^\}", css, re.DOTALL | re.MULTILINE)
    dark = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}\s*\}", css, re.DOTALL)
    assert light, "no light :root block found; the scan has nothing to read"
    assert dark, "no dark :root block found; the scan has nothing to read"
    rest = css.replace(light.group(0), "").replace(dark.group(0), "")
    return light.group(1), dark.group(1), rest


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))


def _schemes() -> dict[str, dict[str, str]]:
    light, dark, _ = _blocks(STYLESHEET.read_text(encoding="utf-8"))
    light_tokens = _tokens(light)
    return {"light": light_tokens, "dark": {**light_tokens, **_tokens(dark)}}


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("foreground", "ground"), TEXT_PAIRS)
def test_every_text_pair_the_page_draws_clears_aa(scheme: str, foreground: str, ground: str):
    tokens = _schemes()[scheme]

    ratio = contrast(tokens[foreground], tokens[ground])

    assert ratio >= 4.5, f"{scheme}: --{foreground} on --{ground} is {ratio:.2f}:1, under 4.5:1"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("edge", "ground"), BOUNDARY_PAIRS)
def test_every_control_boundary_clears_three_to_one(scheme: str, edge: str, ground: str):
    tokens = _schemes()[scheme]

    ratio = contrast(tokens[edge], tokens[ground])

    assert ratio >= 3, f"{scheme}: --{edge} against --{ground} is {ratio:.2f}:1, under 3:1"


def test_no_colour_is_written_outside_the_token_blocks():
    _, _, rest = _blocks(STYLESHEET.read_text(encoding="utf-8"))

    stray = re.findall(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(", rest)

    assert rest.count("var(--") > 20, "the scan read almost nothing outside the tokens, so it checks nothing"
    assert stray == [], f"colours outside the token blocks: {stray}"


def test_every_token_the_stylesheet_uses_is_defined():
    """A misspelt `var(--x)` draws nothing at all, and no other check here would see it."""
    css = STYLESHEET.read_text(encoding="utf-8")
    light, _, _ = _blocks(css)
    defined = set(re.findall(r"--([\w-]+):", light))

    used = set(re.findall(r"var\(--([\w-]+)\)", css))

    assert used - defined == set()


def test_every_typeface_the_stylesheet_names_is_on_disk():
    sources = re.findall(r'url\("([^"]+)"\)', STYLESHEET.read_text(encoding="utf-8"))

    assert sources, "the stylesheet names no typeface; the scan checks nothing"
    assert [source for source in sources if not (STATIC_DIRECTORY / source).is_file()] == []


class TestTheStyleIsServed:
    @pytest.fixture
    async def page(self, cache_dir: Path):
        state = PageState(
            ui=UiSettings(cache_dir=cache_dir, bind="127.0.0.1", port=0, pinned=frozenset()),
            setup_code="code",
            now=lambda: 0.0,
            throttle=auth.Throttle(monotonic=lambda: 0.0),
        )
        client = TestClient(TestServer(build(state)))
        await client.start_server()
        yield client
        await client.close()

    async def test_the_pages_link_the_stylesheet_and_no_inline_style(self, page):
        text = await (await page.get("/")).text()

        assert '<link rel="stylesheet" href="/static/settings.css">' in text
        assert "<style" not in text, "an inline style, which the page's CSP refuses"

    @pytest.mark.parametrize(
        ("path", "kind"),
        [
            ("/static/settings.css", "text/css"),
            ("/static/fonts/newsreader/latin-normal.woff2", "font/woff2"),
            ("/static/fonts/instrument-sans/latin-normal.woff2", "font/woff2"),
        ],
    )
    async def test_the_style_and_typefaces_are_served_without_a_session_and_kept_a_day(self, page, path, kind):
        response = await page.get(path)

        assert response.status == 200
        assert response.content_type == kind
        assert response.headers["Cache-Control"] == STATIC_CACHE_CONTROL

    @pytest.mark.parametrize("path", ["/static/../store.py", "/static/%2e%2e/store.py", "/static/"])
    async def test_nothing_outside_the_static_directory_is_served(self, page, path):
        response = await page.get(path, allow_redirects=False)

        assert response.status in {403, 404}
        assert "SETTINGS_FILENAME" not in await response.text()

    async def test_the_csp_lets_the_page_load_its_own_style_and_typefaces_and_nothing_else(self, page):
        policy = (await page.get("/")).headers["Content-Security-Policy"]

        assert "style-src 'self'" in policy
        assert "font-src 'self'" in policy
        assert "default-src 'none'" in policy
        assert "unsafe-inline" not in policy
