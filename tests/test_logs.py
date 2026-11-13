"""The log shape, and the one line in it that is a security control.

`configure` had no test and one caller, which for the token clamp meant
`max`→`min`, a renamed constant or a reordering would all pass the suite — and
the failure mode is the television's pairing token sitting in a journal excerpt
somebody pastes into an issue, in a public repository, which is the thing this
project's very first chunk existed to undo.
"""

import ast
import json
import logging
import pathlib
from collections.abc import Iterator

import pytest

from arrt_player import logs


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Put the root logger back, because `configure` deliberately mutates it.

    Without this, one test's handler formats every later test's captured output
    and the failure reads as a fault in whatever ran next.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    fork_level = logging.getLogger("samsungtvws").level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("samsungtvws").setLevel(fork_level)


class TestTheTokenCannotReachTheJournal:
    def test_the_television_client_is_floored_above_info_even_in_debug(self):
        """The control itself.

        `configure` attaches this plane's handler to the *root* logger, so
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


_LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
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
