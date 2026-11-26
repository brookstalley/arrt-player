"""The log shape, and the one line in it that is a security control.

`configure` had no test and one caller, which for the token clamp meant
`max`→`min`, a renamed constant or a reordering would all pass the suite — and
the failure mode is the television's pairing token sitting in a journal excerpt
somebody pastes into an issue, in a public repository, which is the thing this
project's very first chunk existed to undo.
"""

import ast
import asyncio
import json
import logging
import pathlib
from collections.abc import Iterator

import pytest
from PIL import Image

from arrt_player import logs
from arrt_player.client import LabelAssignment
from arrt_player.config import FileOutputSettings
from arrt_player.displays import file_output
from arrt_player.displays.file_output import FileScreenOutput, captioning, file_label_panel
from arrt_player.label_rule import Drawing, Outcome
from arrt_player.panel import SurfaceUnavailable
from arrt_player.panel.layout import Layout


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Put the root logger back, because `configure` deliberately mutates it.

    Without this, one test's handler formats every later test's captured output
    and the failure reads as a fault in whatever ran next.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    floored = {name: logging.getLogger(name).level for name in ("samsungtvws", "websockets")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, floored_level in floored.items():
        logging.getLogger(name).setLevel(floored_level)


class TestTheTokenCannotReachTheJournal:
    def test_the_television_client_is_floored_above_info_even_in_debug(self):
        """The control itself.

        `configure` attaches the Player's handler to the *root* logger, so
        anything the fork logs is carried out unless its own logger is held above
        it. The floor must clear INFO, not merely reach it: the fork's token line
        is `_LOGGING.info("New token %s", token)`.
        """
        logs.configure(logging.DEBUG)

        assert logging.getLogger("samsungtvws").level > logging.INFO

    def test_the_floor_does_not_drag_the_client_up_past_a_quieter_run(self):
        """`max`, not a bare assignment: a deployment running at ERROR should not
        find one library chattering underneath it."""
        logs.configure(logging.ERROR)

        assert logging.getLogger("samsungtvws").level == logging.ERROR

    def test_a_token_logged_at_info_by_that_logger_is_not_emitted(self, capsys):
        """**The line that actually leaks it, at the level it actually uses.**

        This is the regression for a floor that read INFO until 2026-08-07 and so
        permitted the exact line it was written to stop — and, because the token
        is written on every *pairing* rather than only under debugging, permitted
        it at the default level with nobody doing anything unusual.
        """
        logs.configure(logging.INFO)

        logging.getLogger("samsungtvws.connection").info("New token %s", "a-real-pairing-token")

        assert "a-real-pairing-token" not in capsys.readouterr().err

    def test_a_token_logged_at_debug_by_that_logger_is_not_emitted(self, capsys):
        """Asserted through the handler rather than through the level number, so a
        refactor that keeps the number and loses the effect still fails."""
        logs.configure(logging.DEBUG)

        logging.getLogger("samsungtvws.connection").debug("Got token %s", "a-real-pairing-token")

        assert "a-real-pairing-token" not in capsys.readouterr().err

    def test_the_websocket_library_is_floored_at_warning_even_in_debug(self):
        """**The art channel's URL carries the token**, and `websockets` logs it.

        `websockets/client.py` `ClientProtocol.send_request` writes
        `> GET <path> HTTP/1.1` at DEBUG, and the path is
        `/api/v2/channels/…?name=…&token=…`, through the logger
        `websockets.client`. Flooring only `samsungtvws` left that line one
        `configure(logging.DEBUG)` away from the journal.
        """
        logs.configure(logging.DEBUG)

        assert logging.getLogger("websockets").getEffectiveLevel() >= logging.WARNING
        assert not logging.getLogger("websockets.client").isEnabledFor(logging.DEBUG)

    def test_the_handshake_line_carrying_the_token_is_not_emitted_in_debug(self, capsys):
        """Through the logger and level the library actually uses, so a floor on the wrong name still fails."""
        logs.configure(logging.DEBUG)

        logging.getLogger("websockets.client").debug(
            "> GET %s HTTP/1.1", "/api/v2/channels/com.samsung.art-app?name=dHZwaQ==&token=a-real-pairing-token"
        )

        assert "a-real-pairing-token" not in capsys.readouterr().err


class TestTheLineShape:
    def test_one_line_is_one_json_object_carrying_what_the_call_site_attached(self, capsys):
        logs.configure()

        logging.getLogger("arrt_player.test").info("showing %s", "a work", extra={"event": "rotation.selected", "n": 3})

        line = json.loads(capsys.readouterr().err.strip())
        assert line["message"] == "showing a work"
        assert line["event"] == "rotation.selected"
        assert line["n"] == 3
        assert line["level"] == "INFO"

    def test_a_bound_work_id_rides_every_line_inside_the_block(self, capsys):
        logs.configure()

        with logs.work_context("w-42"):
            logging.getLogger("arrt_player.test").info("inside")
        logging.getLogger("arrt_player.test").info("outside")

        inside, outside = (json.loads(line) for line in capsys.readouterr().err.strip().splitlines())
        assert inside["work_id"] == "w-42"
        assert "work_id" not in outside

    def test_the_current_work_is_the_bound_one_inside_the_block_and_none_outside(self):
        assert logs.current_work_id() is None

        with logs.work_context("w-42"):
            assert logs.current_work_id() == "w-42"
            with logs.work_context("w-43"):
                assert logs.current_work_id() == "w-43"
            assert logs.current_work_id() == "w-42"

        assert logs.current_work_id() is None

    def test_a_worker_thread_reads_the_work_its_caller_bound(self):
        async def read_in_a_thread() -> str | None:
            with logs.work_context("w-42"):
                return await asyncio.to_thread(logs.current_work_id)

        assert asyncio.run(read_in_a_thread()) == "w-42"

    def test_a_nested_block_leaves_the_outer_id_in_place(self, capsys):
        logs.configure()

        with logs.work_context("outer"):
            with logs.work_context("inner"):
                pass
            logging.getLogger("arrt_player.test").info("after")

        assert json.loads(capsys.readouterr().err.strip())["work_id"] == "outer"

    def test_a_value_that_will_not_serialise_is_rendered_rather_than_dropped(self, capsys):
        """A line that vanished because one field was an odd type is the worst
        outcome available to a logger."""
        logs.configure()

        logging.getLogger("arrt_player.test").info("odd", extra={"thing": object()})

        assert "thing" in json.loads(capsys.readouterr().err.strip())

    def test_a_bound_wall_rides_every_line_inside_the_block_and_none_outside(self, capsys):
        logs.configure()

        with logs.wall_context("hall"):
            logging.getLogger("arrt_player.pull").warning("unreachable", extra={"event": "pull.unreachable"})
        logging.getLogger("arrt_player.client").info("stopped", extra={"event": "client.stopped"})

        inside, outside = (json.loads(line) for line in capsys.readouterr().err.strip().splitlines())
        assert inside["wall_id"] == "hall"
        assert "wall_id" not in outside, "a process-level line named a wall"

    def test_a_bound_label_carries_its_label_and_the_wall_it_captions(self, capsys):
        logs.configure()

        with logs.wall_context("porch"):
            with logs.label_context("label-1", "hall"):
                logging.getLogger("arrt_player.label_renderer").info("drawn", extra={"event": "label.drawn"})
            logging.getLogger("arrt_player.pull").info("after", extra={"event": "pull.adopted"})

        drawn, after = (json.loads(line) for line in capsys.readouterr().err.strip().splitlines())
        assert (drawn["label_id"], drawn["wall_id"]) == ("label-1", "hall")
        assert after["wall_id"] == "porch", "the outer wall was not restored"
        assert "label_id" not in after

    @pytest.mark.parametrize("field", ["wall_id", "label_id"])
    def test_a_wall_or_label_the_call_site_passes_is_never_overwritten(self, capsys, field):
        """A line about another wall than the worker's says so itself; the bound one would be the wrong wall."""
        logs.configure()

        with logs.label_context("bound-label", "bound-wall"):
            logging.getLogger("arrt_player.test").info("explicit", extra={"event": "x.y", field: "passed"})

        line = json.loads(capsys.readouterr().err.strip())
        assert line[field] == "passed"

    def test_a_bound_work_is_stamped_over_one_the_call_site_passes_as_it_always_was(self, capsys):
        """Kept as it was when the wall and label joined it: `display.state` passes the work
        the screen now shows, None when dark, inside the block of the work it was showing,
        and its line has always carried that block's work."""
        logs.configure()

        with logs.work_context("bound-work"):
            logging.getLogger("arrt_player.wall").info("dark", extra={"event": "display.state", "work_id": None})

        assert json.loads(capsys.readouterr().err.strip())["work_id"] == "bound-work"

    async def test_a_thread_and_a_task_each_carry_the_wall_they_were_started_for(self, capsys):
        """`asyncio.to_thread` and a task copy the context they start in, so two walls' lines stay apart."""
        logs.configure()

        def draws() -> None:
            logging.getLogger("arrt_player.compose").info("drawn", extra={"event": "compose.done"})

        async def worker(wall_id: str) -> None:
            with logs.wall_context(wall_id):
                await asyncio.sleep(0)
                await asyncio.to_thread(draws)

        await asyncio.gather(worker("A"), worker("B"))

        assert sorted(json.loads(line)["wall_id"] for line in capsys.readouterr().err.strip().splitlines()) == ["A", "B"]

    def test_configuring_twice_does_not_duplicate_every_line(self, capsys):
        logs.configure()
        logs.configure()

        logging.getLogger("arrt_player.test").info("once")

        assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def test_it_removes_only_its_own_handler(self, capsys):
        """Evicting every root handler is the tidier-looking implementation and a
        badly behaved one: the first casualty is a test harness's capture."""
        someone_else = logging.StreamHandler()
        logging.getLogger().addHandler(someone_else)

        logs.configure()

        assert someone_else in logging.getLogger().handlers


#: The logger's methods a line is written through, `log.log(level, …)` among
#: them: the screen's brightness line chooses its level as it is written.
_LEVELS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
_SOURCE = pathlib.Path(logs.__file__).parent


def _log_calls(tree: ast.Module) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LEVELS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "log"
        ):
            yield node


def _carries_an_event(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
            return any(isinstance(key, ast.Constant) and key.value == "event" for key in keyword.value.keys)
    return False


_MODULES = sorted(_SOURCE.rglob("*.py"))


class TestEveryLineIsFindableByItsEvent:
    """A line whose event name lives only in its message is invisible to
    `jq 'select(.event == ...)'`, the query the journal is shaped for, and it is
    invisible silently: the line is still written, so nothing looks wrong until
    somebody asks the journal a question and gets fewer answers than happened."""

    def test_the_walk_reaches_the_source(self):
        """The walk below passes vacuously over an empty tree or one with no
        `log` calls, so it has to be seen reaching both."""
        assert _SOURCE / "__main__.py" in _MODULES
        assert sum(1 for module in _MODULES for _ in _log_calls(ast.parse(module.read_text()))) > 100
        brightness = ast.parse((_SOURCE / "displays" / "screen_brightness.py").read_text())
        assert any(call.func.attr == "log" for call in _log_calls(brightness)), "a `log.log` call went unwalked"

    @pytest.mark.parametrize("module", _MODULES, ids=lambda path: str(path.relative_to(_SOURCE)))
    def test_every_log_call_passes_its_event_as_a_field(self, module):
        tree = ast.parse(module.read_text())

        missing = [call.lineno for call in _log_calls(tree) if not _carries_an_event(call)]

        assert missing == [], f"log calls with no `extra={{'event': ...}}` at lines {missing}"

    @pytest.mark.parametrize("module", _MODULES, ids=lambda path: str(path.relative_to(_SOURCE)))
    def test_nothing_logs_through_a_logger_the_walk_does_not_see(self, module):
        """The walk above finds calls through the name `log`. A logger bound to
        any other name and logged through would go unseen; one bound only to be
        configured (`logs.configure` sets the root's handler) logs nothing."""
        tree = ast.parse(module.read_text())
        others = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "getLogger"
            for target in node.targets
            if isinstance(target, ast.Name) and target.id != "log"
        }

        logged_through = sorted(
            {
                node.func.value.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LEVELS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in others
            }
        )

        assert logged_through == []


class TestTheFileOutputsReadWhatTheJournalBinds:
    """The file screen and label record the work and mapping the journal's own binding names, and refuse a drawing without it."""

    @pytest.fixture
    def files(self, tmp_path: pathlib.Path) -> FileOutputSettings:
        return FileOutputSettings(directory=tmp_path / "drawn", screen=(160, 90), label=True)

    def _render(self, tmp_path: pathlib.Path) -> pathlib.Path:
        path = tmp_path / "render.jpg"
        Image.new("RGB", (160, 90), (200, 30, 30)).save(path, format="JPEG")
        return path

    def test_a_picture_drawn_outside_any_work_records_no_work_id(self, files, tmp_path):
        screen = FileScreenOutput(files, wall_id="living-room")

        screen.show(self._render(tmp_path))

        assert json.loads((screen.directory / "shown.json").read_text())["work_id"] is None

    def test_a_picture_drawn_inside_a_works_context_records_that_work(self, files, tmp_path):
        screen = FileScreenOutput(files, wall_id="living-room")

        with logs.work_context("w-42"):
            screen.show(self._render(tmp_path))
        record = json.loads((screen.directory / "shown.json").read_text())

        assert record["work_id"] == "w-42"

    def test_a_refresh_after_the_work_context_has_ended_still_names_the_work_that_was_drawn(self, files, tmp_path):
        screen = FileScreenOutput(files, wall_id="living-room")
        with logs.work_context("w-42"):
            screen.show(self._render(tmp_path))
        (screen.directory / "picture.png").unlink()

        screen.refresh()

        assert json.loads((screen.directory / "shown.json").read_text())["work_id"] == "w-42"

    def test_a_file_label_handed_a_layout_with_no_mapping_bound_is_refused_by_name_and_writes_nothing(self, files):
        surface = file_label_panel(files).surface
        token = file_output._DRAWING.set(file_output._Drawn(drawing=Drawing(Outcome.BLANK, state="dark"), label_id="lab-1"))
        try:
            with pytest.raises(SurfaceUnavailable, match="no mapping"):
                surface.show(Layout(surface=surface.geometry, blocks=(), dropped=()))
        finally:
            file_output._DRAWING.reset(token)

        assert not (files.directory / "file-label-0").exists()

    def test_a_draw_outside_a_mapping_is_said_and_leaves_the_panel_not_connected(self, files, caplog):
        panel = file_label_panel(files)

        with caplog.at_level(logging.WARNING):
            asyncio.run(panel.draw(Drawing(Outcome.BLANK, state="dark"), label_id="lab-1"))

        assert not panel.report().connected
        assert [r for r in caplog.records if getattr(r, "event", None) == "label.failed" and "no mapping" in r.getMessage()]
        assert not (files.directory / "file-label-0").exists()

    def test_a_draw_inside_a_mapping_is_unchanged(self, files):
        panel = file_label_panel(files)

        with captioning(LabelAssignment(label_id="lab-1", output="file-label-0", wall_id="living-room")):
            asyncio.run(panel.draw(Drawing(Outcome.BLANK, state="dark"), label_id="lab-1"))

        record = json.loads((files.directory / "file-label-0" / "shown.json").read_text())
        assert (record["wall_id"], record["outcome"]) == ("living-room", "blank")
