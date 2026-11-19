"""The Player's repository keeps its source committed, its secrets out, and its deployment values in `.env`.

Carried from the repository-wide guards when Arrt Player moved into a
repository of its own, so each still holds here:

- **No source is silently excluded from version control.** An unanchored
  `.gitignore` rule once matched a package directory on a case-insensitive
  filesystem; the module existed on disk, imported, and passed every test, and
  was never committed. A fresh clone, or the Pi, would have lacked it.
- **No credential file is tracked.** The television's token was committed to a
  public repository once.
- **No deployment value is in the working tree.** This repository is public, and
  a private address, a home path or a host name in a tool's docstring, a
  runbook or a record is published the moment it is pushed. The check knows the
  shapes of such values, not the values, so it publishes nothing of its own:
  a hit is named by file, line and kind. It reads the working tree, never
  history, and every file in it whatever its suffix (the shapes that leaked were
  in documents and in `.prawduct/`, which a scan of `*.py` never opened).
- **Every test module is in `tests/`**, where `testpaths` collects it; one
  anywhere else looks like a test and guards nothing.
"""

import os
import pathlib
import re
import subprocess
import tomllib

import pytest

PROJECT = pathlib.Path(__file__).resolve().parents[1]

#: The trees whose absence from a fresh clone would matter. `tools/` is here
#: because the operator runs it by hand at the set, and it holds the only code
#: that presses power on the television.
SOURCE_TREES = ("src", "tests", "tools")

#: The pieces of the private-range shapes, named so each can be reported as its own kind.
_PRIVATE_IPV4 = r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
_HOME_PATH = r"/(?:Users|home)/[A-Za-z][\w.-]*"

#: Shapes no source line may carry, comments included: a private-range IPv4
#: address, and a path under a named person's home directory. Documentation
#: names such a path with a placeholder (`/home/<user>`), which this does not
#: match.
_PRIVATE_SHAPES = re.compile(f"{_PRIVATE_IPV4}|{_HOME_PATH}")

#: What a hit is called in a failure, and the shape that finds it. **Kinds, never
#: values**: this guard runs over a public repository and publishes nothing of its own.
#:
#: The address shapes are the ranges a home network and its overlay actually use:
#: RFC 1918, CGNAT 100.64.0.0/10 (Tailscale's), IPv4 link-local 169.254.0.0/16, and
#: IPv6 unique-local fc00::/7 and link-local fe80::/10. The lookbehind and lookahead
#: keep a version string (`1.100.64.1.2`) and a hex run from being an address.
_ADDRESS_SHAPES = {
    "private IPv4 address": re.compile(_PRIVATE_IPV4),
    "home-directory path": re.compile(_HOME_PATH),
    "CGNAT address (100.64/10)": re.compile(r"(?<![\w.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![\w]|\.\d)"),
    "link-local IPv4 address (169.254/16)": re.compile(r"(?<![\w.])169\.254\.\d{1,3}\.\d{1,3}(?![\w]|\.\d)"),
    "IPv6 unique-local or link-local address": re.compile(
        r"(?<![0-9A-Fa-f:])(?:[fF][cCdD][0-9A-Fa-f]{2}|[fF][eE][89abAB][0-9A-Fa-f])(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Fa-f:])"
    ),
}

#: A loopback host after `user@`: a login there names no deployment. Whole
#: literals only (`127.0.0.0/8`, `localhost`; `[::1]` never starts a host the
#: login shape reads), so `127.0.0.1.<name>.lan` is still that name's login.
_LOOPBACK = r"(?:127(?:\.\d{1,3}){3}|localhost)(?![\w.-]|\.\w)"

#: Host-name and login shapes: a `.local` or `.lan` name, and `user@host`.
#: A login at a loopback host (`_LOOPBACK`) is not one.
#:
#: `user@host` is a target when the host is an IPv4 address or a `.local`/`.lan`
#: name, or when the line is an `ssh`, `scp`, `rsync` or `sftp` command, where any
#: host is. A bare `name@label` elsewhere is a plugin (`prawduct@prawduct`), an
#: action ref (`checkout@<sha>`) or a pin (`testament@main`), and is not matched.
_HOST_SHAPES = {
    ".local or .lan host name": re.compile(r"[\w-]\.(?:local|lan)\b(?![(\w-]|\.\w)"),
    "user@host login": re.compile(
        r"[A-Za-z_][\w.-]*@(?!" + _LOOPBACK + r")(?:\d{1,3}(?:\.\d{1,3}){3}\b|[A-Za-z0-9][\w.-]*\.(?:local|lan)\b)"
        r"|\b(?:ssh|scp|rsync|sftp)\b.*?(?<![\w./<])[A-Za-z_][\w.-]*@(?!" + _LOOPBACK + r")[A-Za-z0-9][\w.-]*"
    ),
}

#: The host names the product's own words use as examples: `arrt.local`
#: (`SERVER_URL` is "e.g. http://arrt.local:8770" in a validation message and a
#: settings hint, and in tests), and the settings page's `wall.lan` (the
#: `UI_ALLOWED_HOSTS` example) and `studio.local` (a machine's own name, written
#: `Studio.local` where its case folding is the test). Named exactly and compared
#: without case, so a second `.local` or `.lan` name, or one with a further label
#: before or after (`my` + `wall.lan`, `my-` + `wall.lan`), is still a hit. A
#: leading hyphen alone (`-wall.lan`, the settings page's refused-name case) is
#: no other name, since no label starts with one.
_EXAMPLE_HOSTS = ("arrt.local", "wall.lan", "studio.local")
_EXAMPLE_HOST = re.compile(
    r"(?<![\w.])(?<![\w.]-)(?:" + "|".join(re.escape(host) for host in _EXAMPLE_HOSTS) + r")(?![\w-]|\.\w)", re.IGNORECASE
)

#: Trees whose test doubles carry private, CGNAT, link-local and ULA addresses on
#: purpose: `tests/test_fetch.py` proves the downloader refuses each, and the
#: television's address is `10.0.0.x` in a dozen tests. They keep the host shapes
#: and lose only the address shapes.
_FIXTURE_TREES = ("tests/",)

#: Hits the tree carries that are recorded rather than fixed, by file and kind,
#: each with why. An entry no longer matching fails `test_every_accepted_residue_still_matches`,
#: so this list shrinks and never outlives its cause.
_ACCEPTED_RESIDUE: dict[tuple[str, str], str] = {}

#: The deployment's published paths. Public (the deploy docs name them), so not a
#: leak; still forbidden in code, because the same code runs on a dev machine
#: where they do not exist. Comments may name them.
_DEPLOYMENT_PATHS = re.compile(r"/srv/art|/opt/samsung-frame-art-loader")


def _tracked() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 -- git from PATH, as every guard here runs it
        cwd=PROJECT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    tracked = [name for name in listed.split("\0") if name]
    # An anchor rather than a count: the entry point is tracked, or the listing is
    # not this project's and every check below would be vacuous.
    assert "src/arrt_player/__main__.py" in tracked, "git ls-files did not list this project; every check here would be vacuous"
    return tracked


def _ignored(paths: list[pathlib.Path]) -> list[str]:
    """The subset of `paths` git would exclude, whatever the index holds (`--no-index`)."""
    result = subprocess.run(
        ["git", "check-ignore", "--stdin", "--no-index"],  # noqa: S607 -- git from PATH, as every guard here runs it
        cwd=PROJECT,
        input="\n".join(str(path) for path in paths),
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 0 = some paths ignored, 1 = none ignored. Anything else is a real error.
    assert result.returncode in (0, 1), f"git check-ignore failed: {result.stderr}"
    return [line for line in result.stdout.splitlines() if line]


@pytest.mark.parametrize("tree", SOURCE_TREES)
def test_every_tree_the_scans_name_exists(tree):
    assert (PROJECT / tree).is_dir(), f"{tree} is not a directory, so the scans below pass over nothing there"


def test_no_source_or_package_directory_is_gitignored():
    candidates = [
        path
        for tree in SOURCE_TREES
        for path in (PROJECT / tree).rglob("*")
        if "__pycache__" not in path.parts and (path.suffix == ".py" or path.is_dir())
    ]

    assert candidates, "expected source files; has the layout moved?"
    assert _ignored(candidates) == [], "these would never be committed"


def test_no_credential_file_is_tracked():
    offenders = [name for name in _tracked() if pathlib.PurePosixPath(name).name in {"token_file", ".env"}]

    assert offenders == [], f"credential files must never be tracked: {offenders}"


def test_no_source_file_carries_a_deployment_value():
    modules = sorted((PROJECT / "src").rglob("*.py")) + sorted((PROJECT / "tools").rglob("*.py"))
    assert modules, "no source to check; has the layout moved?"
    offenders = [
        f"{path.relative_to(PROJECT)}:{number}"
        for path in modules
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _PRIVATE_SHAPES.search(line) or (not line.strip().startswith("#") and _DEPLOYMENT_PATHS.search(line))
    ]

    assert offenders == [], "a deployment value, which belongs in .env:\n" + "\n".join(offenders)


def _working_tree() -> list[str]:
    """Every file git would commit from this tree: tracked, and untracked but not ignored.

    The working tree, never history. Untracked files are in because the guard
    should refuse a leak before `git add`, not after it.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607 -- git from PATH
        cwd=PROJECT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    names = sorted({name for name in listed.split("\0") if name})
    assert "src/arrt_player/__main__.py" in names, "git ls-files did not list this project; the scan below would be vacuous"
    return names


def _kinds_in(line: str, name: str) -> list[str]:
    """The kinds of deployment value `line` carries, for a file at `name`."""
    line = _EXAMPLE_HOST.sub("example-host", line)
    shapes = dict(_HOST_SHAPES)
    if not name.startswith(_FIXTURE_TREES):
        shapes = {**_ADDRESS_SHAPES, **shapes}
    return [kind for kind, shape in shapes.items() if shape.search(line)]


def _leaks(root: pathlib.Path, names: list[str]) -> list[tuple[str, str]]:
    """Every `(file:line, kind)` under `root` that carries a deployment value, residue included.

    Text files only (a font or an image is skipped by failing to decode as
    UTF-8), whatever their suffix. A file's comment lines are read like the rest.
    """
    found = []
    for name in names:
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or deleted in the working tree
        for number, line in enumerate(text.splitlines(), start=1):
            found.extend((f"{name}:{number}", kind) for kind in _kinds_in(line, name))
    return found


def test_no_file_in_the_working_tree_carries_a_deployment_value():
    """Documents, `deploy/`, `.prawduct/` and config as well as source; it names file, line and kind, never the value."""
    leaks = [
        f"{where}: {kind}"
        for where, kind in _leaks(PROJECT, _working_tree())
        if (where.rsplit(":", 1)[0], kind) not in _ACCEPTED_RESIDUE
    ]

    assert leaks == [], "a deployment value, which belongs in .env or nowhere:\n" + "\n".join(leaks)


def test_every_accepted_residue_still_matches():
    """An entry the tree no longer needs is deleted, so the list never excuses a hit that is gone."""
    present = {(where.rsplit(":", 1)[0], kind) for where, kind in _leaks(PROJECT, _working_tree())}
    stale = sorted(set(_ACCEPTED_RESIDUE) - present)

    assert stale == [], f"accepted residue that no longer matches anything, so remove it: {stale}"


def test_the_scan_opens_documents_deploy_and_prawduct_not_only_source():
    """The shapes that leaked were in documents and in `.prawduct/`, and a scan of `*.py` never saw them."""
    scanned = set(_working_tree())

    for known in (
        ".prawduct/artifacts/architecture.md",
        ".prawduct/project-state.yaml",
        "deploy/README.md",
        "deploy/display.service",
        "README.md",
        "pyproject.toml",
        ".github/workflows/suites.yml",
        "src/arrt_player/__main__.py",
    ):
        assert known in scanned, f"{known} is not in the scanned set"


# The seeds are assembled from parts. Written whole, the host-shaped ones would be
# hits in this very file, which the scan reads like any other.
_NAS = "nas" + ".lan"
_BOX = "box" + ".local"
#: One value per case, so a pattern that loses one of its alternatives fails a case of its own.
_SEEDS = [
    ("private IPv4 address", "host 10.4.5.6"),
    ("private IPv4 address", "host 192." + "168.1.9"),
    ("private IPv4 address", "host 172." + "20.0.3"),
    ("home-directory path", "see /" + "Users/somebody/art"),
    ("home-directory path", "see /" + "home/somebody/art"),
    ("CGNAT address (100.64/10)", "tailnet " + "100.64.0.9"),
    ("CGNAT address (100.64/10)", "tailnet " + "100.127.255.1"),
    ("CGNAT address (100.64/10)", "tailnet " + "100.100.1.2"),
    ("CGNAT address (100.64/10)", "tailnet " + "100.80.1.2"),
    ("link-local IPv4 address (169.254/16)", "fallback " + "169." + "254.7.8"),
    ("IPv6 unique-local or link-local address", "listens on " + "fd12:3456:789a::1"),
    ("IPv6 unique-local or link-local address", "listens on " + "fc00::2"),
    ("IPv6 unique-local or link-local address", "listens on " + "fe80::1"),
    ("IPv6 unique-local or link-local address", "listens on " + "FEBF:0:0:1::7"),
    (".local or .lan host name", f"open http://{_BOX}:8770"),
    (".local or .lan host name", f"ping {_NAS}."),
    (".local or .lan host name", f"{_BOX}"),
    ("user@host login", "ssh " + "pi" + "@raspberrypi"),
    ("user@host login", "scp file " + "root@" + "10.9.8.7:/srv"),
    ("user@host login", "then rsync -a x " + "brooks@" + "somewhere.example.com:/y"),
    ("user@host login", "mail " + "pi@" + _NAS),
    ("user@host login", "run " + "pi@" + "10.0.0.5"),
    # Beside the loopback and example-host exemptions: what each must not excuse.
    ("user@host login", "run " + "pi@" + "192." + "168.4.5"),
    ("user@host login", "mail " + "pi@" + "other" + ".local"),
    ("user@host login", "ssh " + "pi@" + "127.0.0.1." + "other" + ".lan"),
    (".local or .lan host name", "ping " + "other" + ".lan"),
    (".local or .lan host name", "open http://" + "my" + "wall.lan" + ":8771"),
    (".local or .lan host name", "open http://" + "x." + "studio.local"),
    (".local or .lan host name", "open http://" + "my-" + "wall.lan"),
]

#: Documentation forms and near-misses that are not deployment values.
_LOOKALIKES = [
    "/home/<user>/art and /Users/<name>",
    "100.63.255.1 and 100.128.0.1 and 1.100.64.1.2",
    "169.253.1.1 and 169.255.1.1",
    "fe7f::1 and fec0::1 and fb00::1 and fe80",
    "a hash 3fd12:3456 and mac fc:aa:bb:cc:dd:ee",
    "threading.local() and ~/.local/bin and foo.localhost and bar.local.cfg and x.landing",
    "git+ssh://git@github.com/someone/testament@main",
    "claude plugin install prawduct@prawduct",
    "uses: actions/checkout@" + "0" * 40,
    "mail someone@example.com",
    "ssh <user>@<host>",
    "git+ssh://some.user@github.com/a/b",
    "ssh://git@github.com/a/b",
    "ssh <user@host>",
    "ssh into the box",
    "http://arrt.local:8770 and user:pw@arrt.local",
    "UI_ALLOWED_HOSTS=wall.lan,WALL.LAN. and gethostname Studio.local and studio.local",
    "http://user@127.0.0.1:8771 and pi@127.255.0.9 and mail pi@localhost",
    "ssh pi@127.0.0.1 and ssh pi@localhost and scp f pi@[::1]:/x",
]


@pytest.mark.parametrize(("kind", "seed"), _SEEDS)
@pytest.mark.parametrize(
    "where", ["docs/a-runbook.md", ".prawduct/artifacts/a-record.md", "deploy/a.service", "pyproject.toml", "src/a.py"]
)
def test_a_planted_value_of_each_shape_fails_the_scan_wherever_it_sits(tmp_path, where, kind, seed):
    """Every kind, planted in a document, a record, a unit, a config and a module, is a hit naming that kind.

    The suffix and the directory are what the old scan keyed on, so each is varied.
    """
    target = tmp_path / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# prose\n" + seed + "\n", encoding="utf-8")

    found = _leaks(tmp_path, [where])

    assert kind in {found_kind for _, found_kind in found}, f"{kind} planted in {where} was not found; found {found}"
    assert all(location.startswith(f"{where}:") for location, _ in found)
    assert all(location.rsplit(":", 1)[1] != "1" for location, _ in found), "the prose line above the seed is not a hit"


@pytest.mark.parametrize("line", _LOOKALIKES)
def test_a_lookalike_is_not_a_deployment_value(tmp_path, line):
    (tmp_path / "doc.md").write_text(line + "\n", encoding="utf-8")

    assert _leaks(tmp_path, ["doc.md"]) == []


def test_a_hit_is_reported_by_its_location_and_kind_and_never_by_its_value(tmp_path):
    (tmp_path / "doc.md").write_text("ok\nopen http://" + _BOX + ":1\n", encoding="utf-8")

    assert _leaks(tmp_path, ["doc.md"]) == [("doc.md:2", ".local or .lan host name")]


def test_a_fixture_tree_keeps_the_host_shapes_and_loses_only_the_address_shapes(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text('A = "10.0.0.1"\nB = "' + _BOX + '"\n', encoding="utf-8")

    assert _leaks(tmp_path, ["tests/test_x.py"]) == [("tests/test_x.py:2", ".local or .lan host name")]


def test_a_binary_file_is_skipped_and_a_missing_one_is_too(tmp_path):
    (tmp_path / "font.woff2").write_bytes(b"\xff\xfe10.1.2.3\x00")

    assert _leaks(tmp_path, ["font.woff2", "gone.md"]) == []


@pytest.mark.parametrize(
    ("line", "leaks"),
    [
        ('HOST = "10.1.2.3"', True),
        ('HOST = "192.168.0.20"', True),
        ('HOST = "172.20.0.1"', True),
        ("# copied from /Users/someone/thing", True),
        ('HOST = "172.32.0.1"', False),
        ('HOST = "192.0.2.10"', False),
        ('HOST = "110.1.2.3"', False),
        ("# documented as /home/<user>/art", False),
    ],
)
def test_the_shape_check_knows_a_leak_from_a_lookalike(line, leaks):
    assert bool(_PRIVATE_SHAPES.search(line)) is leaks


def _this_machines_values() -> dict[str, str]:
    """The deployment values this checkout's `.env` holds, and this user's home.

    Read with `dotenv_values`, the parser the Player loads `.env` through, so an
    `export` prefix or an inline comment means here what it means there.
    Coordinates and the television's address are what a public repository must
    not carry; `$HOME` stands in for every path under it.
    """
    from dotenv import dotenv_values

    values = {}
    dotenv = PROJECT / ".env"
    if dotenv.is_file():
        declared = dotenv_values(dotenv)
        for key in ("TV_ADDRESS", "LATITUDE", "LONGITUDE"):
            value = (declared.get(key) or "").strip()
            if len(value) >= 4:
                values[key] = value
    home = os.environ.get("HOME", "")
    if len(home) > len("/home/"):
        values["HOME"] = home
    return values


def test_no_tracked_file_carries_this_machines_deployment_values():
    """Every tracked file, against the values this checkout uses; it names the key that leaked, never the value."""
    values = _this_machines_values()
    if not values:
        pytest.skip("no .env deployment values and no $HOME here, so there is nothing of this machine's to look for")
    offenders = []
    for name in _tracked():
        try:
            text = (PROJECT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or deleted in the working tree
        offenders.extend(f"{name}: this machine's {key}" for key, value in values.items() if value in text)

    assert offenders == [], "a tracked file carries this machine's deployment values:\n" + "\n".join(offenders)


def test_every_test_module_is_in_tests():
    misplaced = [
        name
        for name in _tracked()
        if pathlib.PurePosixPath(name).name.startswith("test_") and name.endswith(".py") and not name.startswith("tests/")
    ]

    assert misplaced == [], f"test modules outside tests/, which pytest never collects: {misplaced}"


def _git_overrides() -> dict[str, str]:
    """The package name and requirement of each `[tool.uv] override-dependencies` entry that is a git URL."""
    pyproject = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = pyproject["tool"]["uv"]["override-dependencies"]
    return {re.split(r"[\[@ ;]", entry, maxsplit=1)[0].lower(): entry for entry in overrides if "git+" in entry}


@pytest.mark.parametrize("package", ["it8951", "waveshare-epd"])
def test_a_git_dependency_omni_epd_leaves_unpinned_is_pinned_here_by_a_full_commit(package):
    """`omni_epd` names both drivers with no commit, so a re-lock would move each to its default branch."""
    overrides = _git_overrides()

    assert package in overrides, f"{package} is not pinned by an override; a re-lock would resolve it to a branch head"
    assert re.search(r"\.git@[0-9a-f]{40}(?:#|$)", overrides[package]), f"{package}'s override names no full commit"


def test_the_lock_records_the_commit_each_git_override_names():
    """The override and the lock agree, so adding the override moved nothing and `uv run --locked` accepts it."""
    lock = (PROJECT / "uv.lock").read_text(encoding="utf-8")
    for package, entry in _git_overrides().items():
        commit = re.search(r"\.git@([0-9a-f]{40})", entry)[1]
        recorded = re.search(
            rf'\[\[package\]\]\nname = "{re.escape(package)}"\nversion = "[^"]*"\nsource = \{{ git = "[^"]*?#([0-9a-f]{{40}})"',
            lock,
        )
        assert recorded, f"{package} has no git source in uv.lock"
        assert recorded[1] == commit, f"uv.lock holds a different commit for {package} than its override names"
