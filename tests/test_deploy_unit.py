"""The unit that runs the Player keeps the properties that keep the wall up.

Carried from Arrt's repository-wide guards when Arrt Player moved into a
repository of its own, where they held both of the units that ran the wall. Each
property below was a failure on a real machine first, and each is invisible when
it holds: a unit that has lost one still starts, and goes wrong only on the day
the fault it guarded against comes back.
"""

import pathlib

UNIT = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "display.service"


def _directives() -> dict[str, list[str]]:
    """Map each directive in the unit to every value assigned to it."""
    directives: dict[str, list[str]] = {}
    for raw in UNIT.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "[")):
            continue
        key, _, value = line.partition("=")
        directives.setdefault(key.strip(), []).append(value.strip())
    return directives


def test_the_unit_declares_its_environment_file():
    """The unit declares its environment file, un-prefixed.

    The Player's settings refuse to start without their deployment values, and an
    `EnvironmentFile=` the unit does not declare — or declares with a `-` prefix,
    making it optional — turns a missing file into a startup crash nobody connects
    back to a file that was never placed.
    """
    values = _directives().get("EnvironmentFile", [])
    assert values, "display.service must declare EnvironmentFile=; the Player requires deployment values to start"
    assert not any(v.startswith("-") for v in values), f"EnvironmentFile= must not be optional: {values}"


def test_the_environment_file_is_the_one_at_the_checkouts_root():
    """The unit's `.env` is the one the Player's own settings would find.

    The Player resolves `.env` from its config module's directory upward, so the
    file it finds without systemd is the one at the root of the checkout it runs
    from. A unit naming any other file gives the daemon one set of values and
    every hand-run tool in `tools/` another, and the tools then check something
    other than the wall.
    """
    directives = _directives()
    (working,) = directives.get("WorkingDirectory", [None])
    (environment,) = directives.get("EnvironmentFile", [None])
    assert working, "display.service must declare WorkingDirectory="
    assert environment == f"{working.rstrip('/')}/.env", f"EnvironmentFile= must be {working}/.env, got {environment!r}"


def test_the_unit_cannot_silently_stop_retrying():
    """The stock rate limit turns a reproducible fault into a unit that gave up.

    Five starts in ten seconds is exhausted in about half a second by a fault that
    reproduces on every start, leaving `failed` permanently after five log lines.
    The Player's downtime is visible on the wall, so it restarts on anything.
    """
    directives = _directives()
    assert directives.get("Restart") == ["always"], "the Player's downtime is visible on the wall; it comes back from anything"
    assert directives.get("StartLimitIntervalSec") == [
        "0"
    ], "start rate limiting must be disabled, or a crash loop ends in a silent permanent `failed`"
    restart_sec = directives.get("RestartSec", [])
    assert restart_sec, "RestartSec= must be set; the 100ms default is what exhausts the allowance"
    assert int(restart_sec[0]) >= 5, f"RestartSec= must leave a real gap, got {restart_sec[0]}"


def test_the_unit_names_no_path_under_a_home_directory():
    """The cutover moved the checkout off `/home`, and this is what keeps it off.

    A `--system` account with `nologin` may have no home at all. Every `/home/...`
    path a unit carried named a directory that did not exist on the rebuilt card,
    and systemd reports that as a unit that will not start rather than as a path
    that is wrong. Asserted over values rather than the whole line so the
    explanatory comments, which legitimately discuss the retired paths, do not
    trip it.
    """
    offenders = [f"{key}={value}" for key, values in _directives().items() for value in values if "/home/" in value]
    assert not offenders, f"display.service must name no path under a home directory: {offenders}"


def test_the_unit_names_its_interpreter_absolutely():
    """`/usr/bin/env uv` resolves against a PATH the service account does not have.

    systemd's PATH carries no per-user directory, and the account the unit runs as
    has no login to install one under, so `env uv` finds nothing. The alternative
    fix — an `Environment=PATH=` line — is how the recovered 2024 unit came to
    carry pyenv shims and a stray editor directory that happened to be in
    somebody's shell. An absolute path fails where it is written instead.
    """
    exec_starts = _directives().get("ExecStart", [])
    assert exec_starts, "display.service must declare ExecStart="
    for value in exec_starts:
        assert value.startswith("/"), f"ExecStart= must be absolute, got {value!r}"
        assert not value.startswith("/usr/bin/env "), f"ExecStart= must not resolve through env: {value!r}"


def test_the_unit_gives_the_television_time_to_let_go():
    """A SIGKILL inside a TV connection leaves the set holding a half-open channel.

    A connection attempt is roughly 15s of blocking construction plus the 30s
    art-channel ceiling, and a SIGTERM landing inside one is honoured when that
    pass ends rather than during it — measured at ~22s against a sleeping set,
    with ~45s the worst case. Below that the unit starts SIGKILLing live
    connections, and the set refuses the next one for minutes. This is a floor
    against *shortening* it; what ships is systemd's 90s default, which clears it.
    """
    directives = _directives()
    timeout = directives.get("TimeoutStopSec", [])
    assert timeout, "display.service must state TimeoutStopSec= rather than inherit it silently"
    assert int(timeout[0]) >= 45, f"TimeoutStopSec= must clear the ~45s worst-case television pass, got {timeout[0]}"
    assert directives.get("KillSignal") == ["SIGTERM"], "the Player closes the television on SIGTERM; nothing else does"
