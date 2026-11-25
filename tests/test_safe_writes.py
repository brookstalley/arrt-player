"""One way to write a file whole, and every writer that uses it.

What a person sees when this is wrong: a wall that boots after a power cut onto a
settings page, a cached feed, a heartbeat or a composed picture that exists and
holds nothing, or a cache directory littered with half-written files that a
later tidy has to guess about. The helper's own tests prove the sequence; the
writers' tests prove each one still writes under its own temporary name and
leaves nothing when it fails.
"""

import ast
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from arrt_player import client as client_module
from arrt_player import compose as compose_module
from arrt_player import pull as pull_module
from arrt_player.compose import Geometry, compose, composed_path
from arrt_player.displays.file_output import write_atomically as file_output_write
from arrt_player.heartbeat import Health, path_in, write
from arrt_player.programmes.slideshow import save_listing
from arrt_player.safe_write import write_whole, write_whole_with
from arrt_player.settings_ui import store as settings_store

SRC = Path(__file__).resolve().parent.parent / "src"


class _Refused(OSError):
    pass


def _leftovers(directory: Path, *, besides: set[str]) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name not in besides)


class TestTheHelper:
    def test_the_file_holds_the_bytes_and_no_temporary_file_remains(self, tmp_path: Path):
        path = tmp_path / "out.json"

        write_whole(path, b"new", temporary=tmp_path / ".out.json.tmp")

        assert path.read_bytes() == b"new"
        assert _leftovers(tmp_path, besides={"out.json"}) == []

    def test_the_bytes_reach_the_disk_before_the_rename_and_it_is_the_staged_file_that_is_forced(
        self, tmp_path: Path, monkeypatch
    ):
        path, temporary = tmp_path / "out.json", tmp_path / ".out.json.tmp"
        events: list[tuple[str, bool]] = []
        real_fsync, real_replace = os.fsync, Path.replace

        def fsync(fd: int) -> None:
            events.append(("fsync", os.fstat(fd).st_ino == temporary.stat().st_ino and temporary.read_bytes() == b"whole"))
            real_fsync(fd)

        def replace(self: Path, target):
            events.append(("replace", self == temporary))
            return real_replace(self, target)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(Path, "replace", replace)

        write_whole(path, b"whole", temporary=temporary)

        assert events == [("fsync", True), ("replace", True)]

    @pytest.mark.parametrize("step", ["open", "fsync", "replace"])
    def test_a_failure_at_any_step_leaves_the_old_file_whole_and_no_temporary_file(self, tmp_path: Path, monkeypatch, step: str):
        path, temporary = tmp_path / "out.json", tmp_path / ".out.json.tmp"
        path.write_bytes(b"old")

        def refuse(*_args, **_kwargs):
            raise _Refused(28, "No space left on device")

        if step == "open":
            real_open = os.open
            monkeypatch.setattr(
                os, "open", lambda name, *rest, **kw: refuse() if Path(name) == temporary else real_open(name, *rest, **kw)
            )
        elif step == "fsync":
            monkeypatch.setattr(os, "fsync", refuse)
        else:
            monkeypatch.setattr(Path, "replace", refuse)

        with pytest.raises(_Refused):
            write_whole(path, b"new", temporary=temporary)

        assert path.read_bytes() == b"old"
        assert _leftovers(tmp_path, besides={"out.json"}) == []

    def test_a_failure_to_remove_the_temporary_file_does_not_hide_the_failure_that_caused_it(self, tmp_path: Path, monkeypatch):
        def no_disk(_fd):
            raise _Refused(28, "No space left on device")

        def no_unlink(self: Path, missing_ok=False):
            raise PermissionError(13, "refused")

        monkeypatch.setattr(os, "fsync", no_disk)
        monkeypatch.setattr(Path, "unlink", no_unlink)

        with pytest.raises(_Refused, match="No space"):
            write_whole(tmp_path / "out.json", b"new", temporary=tmp_path / ".out.json.tmp")

    def test_an_interrupt_between_the_write_and_the_rename_leaves_no_temporary_file(self, tmp_path: Path, monkeypatch):
        def interrupted(_fd):
            raise KeyboardInterrupt

        monkeypatch.setattr(os, "fsync", interrupted)

        with pytest.raises(KeyboardInterrupt):
            write_whole(tmp_path / "out.json", b"new", temporary=tmp_path / ".out.json.tmp")

        assert _leftovers(tmp_path, besides=set()) == []

    def test_a_missing_directory_is_the_callers_oserror_and_creates_nothing(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            write_whole(tmp_path / "gone" / "out.json", b"x", temporary=tmp_path / "gone" / ".out.json.tmp")

        assert _leftovers(tmp_path, besides=set()) == []

    @pytest.mark.parametrize("umask", [0o077, 0o022, 0o000])
    def test_a_mode_is_exact_over_a_stale_temporary_file_and_whatever_the_umask(self, tmp_path: Path, monkeypatch, umask: int):
        path, temporary = tmp_path / "secret.json", tmp_path / "secret.json.tmp"
        temporary.write_bytes(b"left by a crash")
        temporary.chmod(0o644)
        seen: list[int] = []
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            # Whatever bits the staged file carries while it holds the secrets.
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync)
        before = os.umask(umask)
        try:
            write_whole(path, b"token", temporary=temporary, mode=0o600)
        finally:
            os.umask(before)

        assert seen == [0o600], "the data sat in the temporary file at a looser mode"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes() == b"token"

    def test_without_a_mode_the_file_takes_the_umask_as_any_created_file_does(self, tmp_path: Path):
        before = os.umask(0o027)
        try:
            write_whole(tmp_path / "plain", b"x", temporary=tmp_path / ".plain.tmp")
        finally:
            os.umask(before)

        assert stat.S_IMODE((tmp_path / "plain").stat().st_mode) == 0o640

    def test_a_producer_handed_the_staged_path_is_forced_then_renamed_and_its_failure_leaves_nothing(
        self, tmp_path: Path, monkeypatch
    ):
        path, temporary = tmp_path / "out.bin", tmp_path / "out.bin.staged"
        path.write_bytes(b"old")
        events: list[str] = []
        real_fsync, real_replace = os.fsync, Path.replace
        monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
        monkeypatch.setattr(Path, "replace", lambda self, target: (events.append("replace"), real_replace(self, target))[1])

        write_whole_with(path, lambda staged: staged.write_bytes(b"new"), temporary=temporary)

        assert (events, path.read_bytes()) == (["fsync", "replace"], b"new")

        def broken(staged: Path) -> None:
            staged.write_bytes(b"half")
            raise _Refused(5, "Input/output error")

        with pytest.raises(_Refused):
            write_whole_with(path, broken, temporary=temporary)

        assert path.read_bytes() == b"new"
        assert _leftovers(tmp_path, besides={"out.bin"}) == []


class _Promotions(ast.NodeVisitor):
    """Each call that renames one file over another, with the innermost function holding it."""

    def __init__(self) -> None:
        self.found: list[str] = []
        self._inside: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._inside.append(node.name)
        self.generic_visit(node)
        self._inside.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._inside.append(node.name)
        self.generic_visit(node)
        self._inside.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if _is_a_promotion(node):
            self.found.append(self._inside[-1] if self._inside else "<module>")
        self.generic_visit(node)


def _promotions(tree: Path) -> list[str]:
    """Each place under `tree` that renames one file over another, as `path:function`."""
    found: set[str] = set()
    for source in sorted(tree.rglob("*.py")):
        visitor = _Promotions()
        visitor.visit(ast.parse(source.read_text(encoding="utf-8")))
        found.update(f"{source.relative_to(tree).as_posix()}:{name}" for name in visitor.found)
    return sorted(found)


def _is_a_promotion(call: ast.Call) -> bool:
    """A one-argument `.replace` or `.rename` is a `Path`'s (a string's takes two).

    `os.replace`, `os.rename` and `shutil.move` are named for what they do.
    """
    function = call.func
    if not isinstance(function, ast.Attribute):
        return False
    if function.attr in {"replace", "rename"} and len(call.args) == 1 and not call.keywords:
        return True
    owner = function.value
    return isinstance(owner, ast.Name) and (owner.id, function.attr) in {
        ("os", "replace"),
        ("os", "rename"),
        ("shutil", "move"),
    }


#: The renames under `src/` that are not a whole-file write. The helper is the
#: one writer of a whole file; the two streaming writers stage a download whose
#: bytes are not in hand when the file is opened.
STREAMING_WRITERS = {"arrt_player/pull.py:_cache_media", "arrt_player/sources/fetch.py:_save"}
THE_HELPER = {"arrt_player/safe_write.py:_replacing"}
#: A directory moved aside to be deleted, which writes no file.
DIRECTORY_MOVES = {"arrt_player/client.py:_remove"}


def test_no_file_is_renamed_into_place_under_src_but_by_the_helper_and_the_two_streaming_writers():
    assert set(_promotions(SRC)) == STREAMING_WRITERS | THE_HELPER | DIRECTORY_MOVES


def test_the_scan_finds_a_hand_written_temp_and_rename_wherever_it_sits(tmp_path: Path):
    tree = tmp_path / "src"
    tree.mkdir()
    (tree / "a.py").write_text(
        "import os\nimport shutil\n"
        "def one(p, t):\n    t.replace(p)\n"
        "async def two(p, t):\n    os.replace(t, p)\n"
        "class K:\n    def three(self, p, t):\n        t.rename(p)\n"
        "def four(p, t):\n    shutil.move(t, p)\n"
        "def not_one(s):\n    return s.replace('a', 'b')\n"
        "def outer(p, t):\n    def inner():\n        t.replace(p)\n    return inner\n"
    )

    assert _promotions(tree) == ["a.py:four", "a.py:inner", "a.py:one", "a.py:three", "a.py:two"]


class TestEveryWriterKeepsItsOwnTemporaryName:
    """Each directory's tidy knows its own temporary names, so none may change when the writer does."""

    @pytest.fixture
    def promoted(self, monkeypatch) -> list[tuple[str, str]]:
        sources: list[tuple[str, str]] = []
        real = Path.replace

        def recording(self: Path, target):
            sources.append((self.name, Path(target).name))
            return real(self, target)

        monkeypatch.setattr(Path, "replace", recording)
        return sources

    def test_the_pulls_cache_files(self, tmp_path: Path, promoted):
        pull_module._write_atomically(tmp_path / "manifest.json", b"{}")

        assert promoted == [(".manifest.json.tmp", "manifest.json")]

    def test_a_walls_departure_stamp(self, tmp_path: Path, promoted):
        caches = client_module.WallCaches(tmp_path, now=lambda: datetime.now(UTC), monotonic=lambda: 0.0)
        stamp = client_module._Stamp(left_at=datetime.now(UTC), absent_seconds=5.0, written_seconds=0.0)

        caches._write_stamp(tmp_path, stamp)

        assert promoted == [(f".{client_module.LEFT_STAMP_FILENAME}.tmp", client_module.LEFT_STAMP_FILENAME)]
        assert stamp.written_seconds == 5.0

    def test_the_heartbeat_is_staged_under_a_name_the_pull_never_reads(self, tmp_path: Path, promoted):
        write(tmp_path, Health(), wall_id="living-room", reported_at=datetime(2026, 11, 25, tzinfo=UTC))

        heartbeat = path_in(tmp_path, "living-room")
        assert promoted == [(f"{heartbeat.name}.tmp", heartbeat.name)]
        assert heartbeat.read_text().endswith("\n")

    def test_the_settings_file(self, tmp_path: Path, promoted):
        settings_store.write(tmp_path, settings_store.Stored(values={"PLAYER_NAME": "Hall"}))

        assert promoted == [(f"{settings_store.SETTINGS_FILENAME}.tmp", settings_store.SETTINGS_FILENAME)]

    def test_the_file_outputs_pictures_and_records(self, tmp_path: Path, promoted):
        file_output_write(tmp_path / "picture.png", b"png")

        assert promoted == [(".picture.png.tmp", "picture.png")]

    def test_the_slideshows_listing(self, tmp_path: Path, promoted):
        save_listing(tmp_path / "listing.json", ())

        assert promoted == [("listing.json.tmp", "listing.json")]

    def test_a_composed_picture(self, tmp_path: Path, promoted):
        master = tmp_path / "m.jpg"
        Image.new("RGB", (600, 400), (220, 20, 20)).save(master, format="JPEG")
        geometry = Geometry(screen=(400, 300), pixels_per_inch=None, mat_width_inches=1.5, bottom_weight=1.15)

        out = compose(
            master,
            master_sha256="a" * 64,
            mat_color="#336699",
            mode="proportional",
            geometry=geometry,
            directory=tmp_path / "out",
        )

        assert promoted == [(f"{out.name}.composing", out.name)]


class TestWhatTheOldWritersGuaranteedWithoutSayingSo:
    def test_the_settings_file_is_0600_over_a_stale_temporary_file_and_under_any_umask(self, tmp_path: Path):
        stale = tmp_path / f"{settings_store.SETTINGS_FILENAME}.tmp"
        stale.write_text("a crash left this world-readable")
        stale.chmod(0o666)
        before = os.umask(0o000)
        try:
            settings_store.write(tmp_path, settings_store.Stored(values={"PLAYER_NAME": "Hall"}, password="hunter2"))
        finally:
            os.umask(before)

        written = tmp_path / settings_store.SETTINGS_FILENAME
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
        assert not stale.exists()
        assert settings_store.read(tmp_path).password == "hunter2"

    def test_the_settings_file_is_created_if_the_cache_directory_is_not(self, tmp_path: Path):
        settings_store.write(tmp_path / "cache", settings_store.Stored(values={"PLAYER_NAME": "Hall"}))

        assert (tmp_path / "cache" / settings_store.SETTINGS_FILENAME).is_file()

    def test_a_heartbeat_that_cannot_be_written_leaves_the_last_one_whole(self, tmp_path: Path, monkeypatch):
        write(tmp_path, Health(), wall_id="living-room", reported_at=datetime(2026, 11, 25, 12, tzinfo=UTC))
        heartbeat = path_in(tmp_path, "living-room")
        before = heartbeat.read_bytes()

        def no_disk(_fd):
            raise _Refused(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", no_disk)
        with pytest.raises(OSError, match="No space"):
            write(tmp_path, Health(), wall_id="living-room", reported_at=datetime(2026, 11, 25, 13, tzinfo=UTC))

        assert heartbeat.read_bytes() == before
        assert _leftovers(tmp_path, besides={heartbeat.name}) == []

    def test_a_composed_picture_that_cannot_be_saved_leaves_nothing_under_its_final_name_or_beside_it(
        self, tmp_path: Path, monkeypatch
    ):
        master = tmp_path / "m.jpg"
        Image.new("RGB", (600, 400), (220, 20, 20)).save(master, format="JPEG")
        geometry = Geometry(screen=(400, 300), pixels_per_inch=None, mat_width_inches=1.5, bottom_weight=1.15)
        out = tmp_path / "out"
        destination = composed_path(out, master_sha256="a" * 64, mat_color="#336699", mode="proportional", geometry=geometry)

        def half(self: Image.Image, fp, *args, **kwargs):
            Path(fp).write_bytes(b"half a picture")
            raise _Refused(28, "No space left on device")

        monkeypatch.setattr(compose_module.Image.Image, "save", half)

        with pytest.raises(OSError, match="No space"):
            compose(master, master_sha256="a" * 64, mat_color="#336699", mode="proportional", geometry=geometry, directory=out)

        assert not destination.exists()
        assert list(out.iterdir()) == []
