"""Arrt Player imports no Arrt module, and opens an HTTP client only in the two modules named here.

**In the Player's own suite since wave 5**, when the repository split put the
server out of reach: everything this guard reads is the Player's, and the one
thing it reads from the contract, the routes, comes through the package.

**Narrowed 2026-09-30 (wave 2b Chunk 04), as the norm's own row scheduled.** The
channel from curation is now the per-wall manifest and content-addressed media,
pulled into a Player-local cache (Arrt's `architecture.md` § Direction). So one module,
`src/arrt_player/pull.py`, may open an HTTP client. The paths it spells are only
the routes the contract's `routes()` names; renders it fetches from the addresses
the manifest's entries give, which is the contract's design. Only the entry point
may import it, so the exemption cannot be reached by re-export. **What no import
reader can see:** a module reaching `arrt_player.pull.aiohttp` as an attribute of a
package the entry point has already loaded. That is evasion rather than the
convenience this guard is for, and it is stated here as the guard's limit. Every other module is held exactly as before: a
second client anywhere else is the "just fetch it live" shortcut this guard
exists for. The no-curation-import clause is whole, and stays whole: the Player
reads documents, never Arrt's code.

**This is the one norm whose violation looks exactly like success.** The ratified
rule is that the theme manifest file is the only channel from curation to
display; the way it gets broken is not malice but convenience — "just fetch the
label text live" — and that works perfectly in development and in every test,
because curation is up in development and in every test. A green suite is what
the violation looks like. So the guard has to be **static**: it reads the source
and holds whether or not anything is running.

**It could not have been written earlier, and had to arrive with the first module
here.** A check over an empty package passes vacuously, which is the green test
that cannot fail this repo explicitly rejects — and the window in which display
code exists unguarded is exactly the window in which the shortcut gets written.
The norm index named this file for three sessions while it did not exist; that is
recorded in Arrt's `project-preferences.md` as that repo's cautionary example, and the
row moves back to `Test` naming this path because the file now does what the row
claims.

**Imports are followed transitively through this repository's own files**, which
is the difference between a guard and a formality: a direct-only check is evaded
by one shared helper, and the helper is where such an import would actually land.
Third-party packages are not followed, and that is what makes the television
exemption structural rather than a special case — `samsungtvws` reaches the set
over a websocket and drags `aiohttp` in behind it, and neither is display's
import to answer for. Talking to the television is this plane's whole job.

**Narrowed again 2026-11-10, for the settings page, and only for a server.** The
Player now listens for one thing, its own settings page
(`src/arrt_player/settings_ui/server.py`). A listening socket requests nothing
from anyone, so it is not the second channel this guard exists to stop, but it
is built from the same package as a client. So that one module may import
`aiohttp.web`, spelled `from aiohttp.web import …` and no other way, because `import
aiohttp` or `from aiohttp import web` would put the package's client in reach
by attribute; it may never name a client class; and only the entry point may
import it, so nothing reaches aiohttp through it by re-export. Every other module
is held as before.

**Narrowed again 2026-11-10, for the slideshow, at the owner's request (#27).**
A slideshow plays a Reddit listing with no Arrt server at all, so it needs a
client of its own, and a post may link media on any host, so no list of hosts
can bound it. One module, `src/arrt_player/sources/fetch.py`, may open an HTTP
client. It is bounded by what it can reach rather than by a list: its listing
and API calls go only to the hosts in its `API_HOSTS`, which this guard holds to
`SLIDESHOW_API_HOSTS` and holds the module to spelling no other; its media
downloads reach public addresses only, which its own tests hold
(`tests/test_fetch.py`). It imports nothing of the server's configuration, the
contract or the pull, so it cannot learn the server's address or token. Only
modules in its own package, the slideshow programme and the entry point may
import it. Every other module is held as before.

Every exception is proven able to fail below, against planted violations, because a
guard nobody has watched go red is a guard nobody knows is wired up.
"""

import ast
import pathlib
import re

import arrt_contract
import pytest
from import_graph import imported_names, package_of, resolve

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DISPLAY_PACKAGE = PROJECT_ROOT / "src" / "arrt_player"

#: The one module that may open an HTTP client: HTTP mode's pull of the manifest,
#: its renders and the heartbeat.
PULL_MODULE = DISPLAY_PACKAGE / "pull.py"

#: The slideshow's one client: listing and API calls to `SLIDESHOW_API_HOSTS`,
#: media from public addresses.
FETCH_MODULE = DISPLAY_PACKAGE / "sources" / "fetch.py"

#: The only hosts the slideshow's client may call, other than to download media.
SLIDESHOW_API_HOSTS: frozenset[str] = frozenset({"www.reddit.com", "api.redgifs.com"})

#: Who may import the slideshow's client: its own package, the slideshow
#: programme, and the entry point that composes the slideshow.
FETCH_IMPORTERS_PACKAGE = DISPLAY_PACKAGE / "sources"
FETCH_IMPORTERS: frozenset[pathlib.Path] = frozenset(
    {DISPLAY_PACKAGE / "programmes" / "slideshow.py", DISPLAY_PACKAGE / "__main__.py"}
)

#: What the slideshow's client must never import: the server's configuration,
#: the contract, and the pull. Any of them could hand it the server's address
#: or token.
FETCH_FORBIDDEN_IMPORTS: tuple[str, ...] = ("arrt_player.config", "arrt_player.pull", "arrt_player.client", "arrt_contract")

#: The one module that may listen: the settings page. It may take `aiohttp.web`
#: and nothing else from aiohttp.
SETTINGS_PAGE_MODULE = DISPLAY_PACKAGE / "settings_ui" / "server.py"

#: The server half of aiohttp, the only part the settings page may import.
WEB_SERVER = "aiohttp.web"

#: Names that are aiohttp's client, which the settings page must never spell,
#: however it came by them.
CLIENT_NAMES: frozenset[str] = frozenset({"ClientSession", "ClientRequest", "TCPConnector"})

#: Where a module name may resolve to a file **in this project**, first match
#: winning. Arrt's own source is not among them, and need not be: a server name
#: (`arrt.*`) is rejected by `_forbidden` *before* resolution is ever attempted,
#: so the guard never depends on finding the server's copy of anything.
#: Resolution exists only to keep walking this project's files.
SEARCH_ROOTS: tuple[pathlib.Path, ...] = (
    PROJECT_ROOT / "src",
    PROJECT_ROOT,
)

#: The forbidden side of the channel. Anything under this package is curation's.
CURATION_PACKAGE = "arrt"

#: HTTP clients, by the name a module would import them under. **`websockets` and
#: `samsungtvws` are deliberately absent**: the television is reached over a
#: websocket on the LAN, which is this plane's purpose and the one exemption the
#: norm names. What is banned is a *general* client, because the only thing this
#: plane could reach with one is the curation process — the second channel the
#: norm exists to prevent.
HTTP_CLIENTS: frozenset[str] = frozenset(
    {
        "aiohttp",
        "httpx",
        "requests",
        "urllib3",
        "http.client",
        "urllib.request",
        "httplib2",
        "aiohttp_client_cache",
    }
)


def display_modules() -> list[pathlib.Path]:
    return sorted(DISPLAY_PACKAGE.rglob("*.py"))


def test_the_display_plane_has_modules_to_check():
    """The guard's own vacuity check.

    Everything below passes trivially over an empty package, so this is the
    assertion that says the subject exists. If the plane is ever restructured out
    from under this path, this fails loudly rather than the suite going quietly
    green over nothing.
    """
    assert display_modules(), f"no display modules under {DISPLAY_PACKAGE}; this guard would pass over nothing"


def test_no_display_module_reaches_the_curation_plane():
    offences = [offence for offence in _audit(display_modules()) if offence.kind == "curation"]

    assert offences == [], "\n".join(str(offence) for offence in offences)


def test_no_display_module_but_the_pull_and_the_slideshow_fetch_opens_an_http_client():
    offences = [offence for offence in _audit(display_modules()) if offence.kind == "http" and not _exempt(offence)]

    assert offences == [], "\n".join(str(offence) for offence in offences)


def test_the_pull_is_where_the_http_client_is():
    """The exemption names a real module that really opens a client, or it exempts nothing."""
    assert PULL_MODULE.is_file(), f"{PULL_MODULE} does not exist; the exemption names nothing"
    assert any(offence.kind == "http" and _exempt(offence) for offence in _audit(display_modules()))


def test_the_slideshow_fetch_is_where_its_client_is():
    assert FETCH_MODULE.is_file(), f"{FETCH_MODULE} does not exist; the exemption names nothing"
    assert any(
        offence.kind == "http" and _exempt(offence) and offence.chain[-1] == FETCH_MODULE for offence in _audit(display_modules())
    )


def test_only_the_slideshow_imports_its_fetch():
    """As with the pull: the exemption is for the file, not for whatever imports it from elsewhere."""
    assert _fetch_importers_outside(display_modules()) == []


def test_the_slideshow_fetch_calls_only_its_api_hosts():
    """Its host set is the one this guard names, and it spells no other host."""
    from arrt_player.sources import fetch

    assert fetch.API_HOSTS == SLIDESHOW_API_HOSTS
    assert _hosts_spelled(FETCH_MODULE) <= SLIDESHOW_API_HOSTS, _hosts_spelled(FETCH_MODULE) - SLIDESHOW_API_HOSTS
    assert _hosts_spelled(FETCH_MODULE), "the fetch spells no host, so this checks nothing"


def test_the_slideshow_fetch_cannot_learn_the_server():
    assert _forbidden_fetch_imports(FETCH_MODULE) == []


def test_the_settings_page_is_where_the_web_server_is():
    """The page's exemption names a real module that really imports the server half, or it exempts nothing."""
    assert SETTINGS_PAGE_MODULE.is_file(), f"{SETTINGS_PAGE_MODULE} does not exist; the exemption names nothing"
    assert any(
        offence.kind == "http" and _exempt(offence) and offence.chain[-1] == SETTINGS_PAGE_MODULE
        for offence in _audit(display_modules())
    )


def test_the_settings_page_names_no_client():
    assert _client_names(SETTINGS_PAGE_MODULE) == set()


def test_the_settings_page_takes_aiohttp_only_as_from_aiohttp_web():
    """`import aiohttp.web` binds the name `aiohttp`, which reaches `aiohttp.request` by attribute."""
    assert _aiohttp_spellings(SETTINGS_PAGE_MODULE) == []


def test_only_the_entry_point_imports_the_settings_page():
    """As with the pull: an exemption for a file must not become an exemption for whatever imports it."""
    importers = sorted(
        str(path.relative_to(PROJECT_ROOT))
        for path in display_modules()
        if path != SETTINGS_PAGE_MODULE and "arrt_player.settings_ui.server" in set(imported_names(path))
    )

    assert importers == ["src/arrt_player/__main__.py"]


def test_only_the_entry_point_imports_the_pull():
    """The exemption is for the pull, not for whatever the pull re-exports.

    `from arrt_player.pull import aiohttp` in any other module would reach the client
    through the one file allowed to hold it, and the transitive audit, which
    stops at that file, would see nothing. So the pull has exactly one importer,
    the entry point that starts it.
    """
    importers = sorted(
        str(path.relative_to(PROJECT_ROOT))
        for path in display_modules()
        if path != PULL_MODULE
        and any(name == "arrt_player.pull" or name.startswith("arrt_player.pull.") for name in imported_names(path))
    )

    assert importers == ["src/arrt_player/__main__.py"]


def test_the_pull_requests_only_the_routes_the_contract_names():
    """Its route constants are the contract's, and it spells no other path.

    Media is the one route it does not spell: its address comes from each
    manifest entry's `media.url`, which the contract says to resolve against the
    manifest's own URL.
    """
    routes = arrt_contract.routes()
    contract_paths = {route["path"] for route in routes.values()}
    tree = ast.parse(PULL_MODULE.read_text(encoding="utf-8"))
    spelled = {
        node.value
        for node in ast.walk(tree)
        # A string that starts a path segment: "/walls/…", or "/api/" as an
        # f-string splits one. A bare "/" joining the cache's own relative paths
        # names no route.
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.match(r"/\w", node.value)
    }

    assert spelled, "the pull spells no path, so this checks nothing"
    assert spelled <= contract_paths, f"paths the contract does not name: {spelled - contract_paths}"


def _exempt(
    offence: "Offence",
    pull: pathlib.Path = PULL_MODULE,
    page: pathlib.Path = SETTINGS_PAGE_MODULE,
    fetch: pathlib.Path = FETCH_MODULE,
) -> bool:
    """Whether the file that imports it is allowed to: the pull and the fetch any client, the page only the server half."""
    importer = offence.chain[-1]
    if importer in (pull, fetch):
        return True
    return importer == page and (offence.name == WEB_SERVER or offence.name.startswith(f"{WEB_SERVER}."))


def _fetch_importers_outside(
    modules: list[pathlib.Path],
    fetch: pathlib.Path = FETCH_MODULE,
    package: pathlib.Path = FETCH_IMPORTERS_PACKAGE,
    allowed: frozenset[pathlib.Path] = FETCH_IMPORTERS,
) -> list[str]:
    """Every module that imports the fetch and is neither in its package nor one of `allowed`."""
    name = ".".join(package_of(fetch))
    return sorted(
        _relative(path)
        for path in modules
        if path != fetch
        and package not in path.parents
        and path not in allowed
        and any(imported == name or imported.startswith(f"{name}.") for imported in imported_names(path))
    )


#: What a host looks like when spelled as a string: dotted labels, or a URL's authority.
_HOST = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.IGNORECASE)


def _hosts_spelled(path: pathlib.Path) -> set[str]:
    """Every host a module spells in a string other than a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    hosts: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            for match in _HOST.finditer(node.value):
                host = match.group(1).lower()
                # A module's own file name ("connector.py") is not a host.
                if not host.endswith(".py"):
                    hosts.add(host)
    return hosts


def _forbidden_fetch_imports(path: pathlib.Path) -> list[str]:
    return sorted(
        name
        for name in set(imported_names(path))
        if any(name == forbidden or name.startswith(f"{forbidden}.") for forbidden in FETCH_FORBIDDEN_IMPORTS)
    )


def _aiohttp_spellings(path: pathlib.Path) -> list[str]:
    """Every import of aiohttp in a file that is not `from aiohttp.web import …`."""
    wrong: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            wrong += [f"import {alias.name}" for alias in node.names if alias.name.split(".")[0] == "aiohttp"]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "aiohttp" and node.module != WEB_SERVER:
            wrong.append(f"from {node.module} import …")
    return wrong


def _client_names(path: pathlib.Path) -> set[str]:
    """Every client name a file spells, as a name, an attribute or an imported alias."""
    spelled: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            spelled.add(node.id)
        elif isinstance(node, ast.Attribute):
            spelled.add(node.attr)
        elif isinstance(node, ast.alias):
            spelled.add(node.name.rsplit(".", 1)[-1])
    return spelled & CLIENT_NAMES


class TestTheGuardCanFail:
    """Planted violations, because a check never seen red is a check never wired up."""

    def test_the_exemption_covers_the_pull_and_nothing_beside_it(self, tmp_path: pathlib.Path):
        """The narrowed clause, planted: the pull may hold a client, a second module may not."""
        pull = _plant(tmp_path, "pull.py", "import aiohttp\n")
        second = _plant(tmp_path, "second.py", "import httpx\n")

        offences = [offence for offence in _audit([pull, second], roots=(tmp_path,)) if not _exempt(offence, pull)]

        assert [offence.chain[-1].name for offence in offences] == ["second.py"]

    def test_the_exemption_covers_the_fetch_and_nothing_beside_it(self, tmp_path: pathlib.Path):
        fetch = _plant(tmp_path, "fetch.py", "import aiohttp\n")
        second = _plant(tmp_path, "slideshow.py", "import aiohttp\n")

        offences = [offence for offence in _audit([fetch, second], roots=(tmp_path,)) if not _exempt(offence, fetch=fetch)]

        assert [offence.chain[-1].name for offence in offences] == ["slideshow.py"]

    def test_it_catches_an_import_of_the_fetch_from_outside_the_slideshow(self, tmp_path: pathlib.Path):
        sources = tmp_path / "sources"
        sources.mkdir()
        (tmp_path / "__init__.py").write_text("")
        (sources / "__init__.py").write_text("")
        fetch = _plant(sources, "fetch.py", "import aiohttp\n")
        sibling = _plant(sources, "reddit.py", "from .fetch import Fetcher\n")
        allowed = _plant(tmp_path, "composition.py", f"from {tmp_path.name}.sources.fetch import Fetcher\n")
        stray = _plant(tmp_path, "label.py", f"from {tmp_path.name}.sources import fetch\n")
        relative = _plant(tmp_path, "wall.py", "from .sources.fetch import Fetcher\n")

        found = _fetch_importers_outside(
            [fetch, sibling, allowed, stray, relative], fetch=fetch, package=sources, allowed=frozenset({allowed})
        )

        assert [pathlib.Path(name).name for name in found] == ["label.py", "wall.py"]

    def test_it_catches_a_host_outside_the_set(self, tmp_path: pathlib.Path):
        fetch = _plant(
            tmp_path,
            "fetch.py",
            '"""Docstrings may name https://www.example.com/ and www.example.org."""\n'
            'API_HOSTS = frozenset({"www.reddit.com"})\n'
            'LABELS = "https://arrt.local:8000/labels"\n'
            'NOTE = "or fall back to cdn.example.net."\n'
            "def f(host):\n"
            '    """Nor does a function\'s: api.example.com."""\n'
            '    return f"https://api.imgur.com/3/{host}"\n',
        )

        assert _hosts_spelled(fetch) == {"www.reddit.com", "arrt.local", "api.imgur.com", "cdn.example.net"}

    def test_it_catches_the_fetch_reading_the_server_configuration(self, tmp_path: pathlib.Path):
        fetch = _plant(tmp_path, "fetch.py", "from arrt_player.config import ClientSettings\nimport arrt_contract\n")

        assert _forbidden_fetch_imports(fetch) == ["arrt_contract", "arrt_player.config", "arrt_player.config.ClientSettings"]

    def test_the_settings_page_may_take_the_server_half_and_nothing_else(self, tmp_path: pathlib.Path):
        """The page's clause, planted: `from aiohttp.web import …` passes; every road to the client does not."""
        allowed = _plant(tmp_path, "server.py", "from aiohttp.web import Application, TCPSite\n")
        assert [offence for offence in _audit([allowed], roots=(tmp_path,)) if not _exempt(offence, page=allowed)] == []

        for source in (
            "import aiohttp\n",
            "from aiohttp import web\n",
            "from aiohttp.web import Application\nfrom aiohttp import ClientSession\n",
            "import aiohttp.web\n\naiohttp.ClientSession()\n",
            "import aiohttp.web\n\naiohttp.request('GET', 'http://elsewhere')\n",
            "from aiohttp.web_app import Application\n",
        ):
            page = _plant(tmp_path, "server.py", source)
            caught = [offence for offence in _audit([page], roots=(tmp_path,)) if not _exempt(offence, page=page)]
            assert caught or _client_names(page) or _aiohttp_spellings(page), f"the page's exemption let this through: {source!r}"

    def test_the_server_half_is_not_exempt_beside_the_page(self, tmp_path: pathlib.Path):
        page = _plant(tmp_path, "server.py", "from aiohttp.web import Application\n")
        second = _plant(tmp_path, "second.py", "from aiohttp.web import Application\n")

        offences = [offence for offence in _audit([page, second], roots=(tmp_path,)) if not _exempt(offence, page=page)]

        assert [offence.chain[-1].name for offence in offences] == ["second.py"]

    def test_it_catches_a_client_name_on_the_page(self, tmp_path: pathlib.Path):
        page = _plant(tmp_path, "server.py", "from aiohttp.web import Application\n\nsession = web.ClientSession()\n")

        assert _client_names(page) == {"ClientSession"}

    def test_it_catches_a_curation_import(self, tmp_path: pathlib.Path):
        module = _plant(tmp_path, "shortcut.py", "from arrt.library.services.catalogue import CatalogueService\n")

        offences = _audit([module], roots=(tmp_path,))

        assert [offence.kind for offence in offences] == ["curation"]

    def test_it_catches_an_http_client(self, tmp_path: pathlib.Path):
        module = _plant(tmp_path, "shortcut.py", "import aiohttp\n\n\nasync def label(): return aiohttp.ClientSession()\n")

        offences = _audit([module], roots=(tmp_path,))

        assert [offence.kind for offence in offences] == ["http"]

    def test_it_catches_a_curation_import_one_helper_away(self, tmp_path: pathlib.Path):
        """The evasion the transitive half exists for.

        A direct-only check reads `shortcut.py`, sees an innocent local import,
        and passes — while the helper next to it does the forbidden thing.
        """
        _plant(tmp_path, "helper.py", "from arrt.config import load\n")
        module = _plant(tmp_path, "shortcut.py", "import helper\n")

        offences = _audit([module], roots=(tmp_path,))

        assert [offence.kind for offence in offences] == ["curation"]
        assert "helper.py" in str(offences[0]), "the offence did not name the file that actually does it"

    def test_it_catches_an_http_client_one_helper_away(self, tmp_path: pathlib.Path):
        _plant(tmp_path, "helper.py", "import requests\n")
        module = _plant(tmp_path, "shortcut.py", "from helper import requests\n")

        offences = _audit([module], roots=(tmp_path,))

        assert [offence.kind for offence in offences] == ["http"]

    def test_it_catches_a_dynamically_named_client(self, tmp_path: pathlib.Path):
        """An import expressed as a string is still an import.

        Banning the `import` line and stopping there leaves `import_module` as a
        one-line hole, and the person who reaches for it is by definition working
        around the rule rather than unaware of it.
        """
        module = _plant(tmp_path, "shortcut.py", "import importlib\n\nhttp = importlib.import_module('requests')\n")

        offences = _audit([module], roots=(tmp_path,))

        assert [offence.kind for offence in offences] == ["http"]

    def test_it_does_not_object_to_the_television(self, tmp_path: pathlib.Path):
        """The exemption, asserted rather than assumed — talking to the set is the job."""
        module = _plant(
            tmp_path,
            "shortcut.py",
            "from samsungtvws.async_art import SamsungTVAsyncArt\nfrom websockets.exceptions import WebSocketException\n",
        )

        assert _audit([module], roots=(tmp_path,)) == []

    def test_a_relative_import_is_followed(self, tmp_path: pathlib.Path):
        """`from .helper import x` resolves to a file like any other name.

        Missing this is how a guard passes over a package that only ever imports
        itself relatively — which is most well-formed packages.
        """
        package = tmp_path / "plane"
        package.mkdir()
        (package / "__init__.py").write_text("")
        _plant(package, "helper.py", "import requests\n")
        module = _plant(package, "shortcut.py", "from .helper import requests\n")

        offences = _audit([module], roots=(tmp_path,))

        assert [offence.kind for offence in offences] == ["http"]


# -- the audit itself -----------------------------------------------------


class Offence:
    """One forbidden import, and the chain of files that reaches it."""

    def __init__(self, kind: str, name: str, chain: tuple[pathlib.Path, ...]) -> None:
        self.kind = kind
        self.name = name
        self.chain = chain

    def __repr__(self) -> str:
        return str(self)

    def __str__(self) -> str:
        route = " -> ".join(_relative(path) for path in self.chain)
        subject = "the curation plane" if self.kind == "curation" else "an HTTP client"
        return f"{route} imports {self.name!r}, which is {subject}"


def _relative(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _audit(entry_points: list[pathlib.Path], roots: tuple[pathlib.Path, ...] = SEARCH_ROOTS) -> list[Offence]:
    """Every forbidden import reachable from these files, through repo-local ones.

    Breadth-first over files rather than recursive, so a cycle between two modules
    — legal in Python and common in packages — terminates instead of recursing
    until the stack gives out.
    """
    offences: list[Offence] = []
    seen: set[pathlib.Path] = set()
    # One offence per (kind, package, file). `from arrt.x import Y` yields
    # both `arrt.x` and `arrt.x.Y` — the resolver needs both readings,
    # since only the filesystem knows whether the tail is a submodule — but a
    # reader wants one line per place that does the forbidden thing, not one per
    # spelling of it.
    reported: set[tuple[str, str, pathlib.Path]] = set()
    queue: list[tuple[pathlib.Path, tuple[pathlib.Path, ...]]] = [(path, (path,)) for path in entry_points]

    while queue:
        path, chain = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)

        for name in imported_names(path):
            forbidden = _forbidden(name)
            if forbidden is not None:
                kind, package = forbidden
                if (kind, package, path) not in reported:
                    reported.add((kind, package, path))
                    offences.append(Offence(kind, name, chain))
                continue
            local = resolve(name, roots)
            if local is not None and local not in seen:
                queue.append((local, (*chain, local)))

    return offences


def _forbidden(name: str) -> tuple[str, str] | None:
    """Which rule this import breaks, if any, and the package that identifies it."""
    if name == CURATION_PACKAGE or name.startswith(f"{CURATION_PACKAGE}."):
        return ("curation", CURATION_PACKAGE)
    # The server half is its own package for de-duplication: a file importing
    # both `aiohttp.web` and `aiohttp` must report both, or the exempt one would
    # stand for the other.
    if name == WEB_SERVER or name.startswith(f"{WEB_SERVER}."):
        return ("http", WEB_SERVER)
    for client in HTTP_CLIENTS:
        if name == client or name.startswith(f"{client}."):
            return ("http", client)
    return None


def _plant(directory: pathlib.Path, filename: str, source: str) -> pathlib.Path:
    module = directory / filename
    module.write_text(source, encoding="utf-8")
    return module


@pytest.fixture(autouse=True)
def _guard_is_pointed_at_something_real():
    """Fails the whole module if the plane moves, rather than passing over nothing."""
    assert DISPLAY_PACKAGE.is_dir(), f"{DISPLAY_PACKAGE} does not exist; this guard has lost its subject"
