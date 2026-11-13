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


def test_a_save_on_the_settings_page_comes_back():
    """The page restarts the Player by sending itself SIGTERM and exiting cleanly
    (`__main__.restart_this_process`). Only `Restart=always` restarts a clean
    exit, so `on-failure` here would leave the Player stopped after a save; and
    the signal it sends has to be the one the unit stops it with, so the stop
    is the orderly one that closes the art channel."""
    directives = _directives()

    assert directives.get("Restart") == ["always"], "a save on the settings page would leave the Player stopped"
    assert directives.get("KillSignal") == ["SIGTERM"]


def test_the_unit_can_reach_the_display_and_the_gpus_render_node():
    """`video` for the display card, `render` for the render node a slideshow's mpv draws through.

    Without `render` the Player starts, and every slideshow item fails with a
    reason that names neither the group nor the unit.
    """
    groups = " ".join(_directives().get("SupplementaryGroups", [])).split()

    assert {"video", "render"} <= set(groups), groups


def test_what_the_player_writes_is_its_own_to_read():
    """The television library writes its pairing token with a plain
    `open(path, "w")`, so the token file gets whatever the umask leaves, and the
    Pi's default leaves it world-readable. `UMask=0077` makes every file the
    Player creates its own from the moment it exists, the token included;
    `SamsungTv.connect` tightens the token file as well, for runs outside the
    unit."""
    assert _directives().get("UMask") == ["0077"]


def test_the_player_can_reach_a_screens_brightness():
    """An HDMI screen's brightness is set over the connector's DDC/CI bus,
    `/dev/i2c-*`, which is `root:i2c 0660` on Raspberry Pi OS. Without the group
    every write fails with a permission error, which the Player reports once and
    retries forever; the wall still runs, so nothing else would show it."""
    groups = " ".join(_directives().get("SupplementaryGroups", [])).split()
    assert "i2c" in groups
    # The groups the screen and the label already needed are still there.
    assert {"spi", "gpio", "video"} <= set(groups)


def test_the_bus_the_brightness_needs_is_loaded_at_boot():
    """`/dev/i2c-*` exists only while `i2c-dev` is loaded. The Pi loaded it only
    through ddcutil's own file (measured 2026-11-13), which removing ddcutil
    would take away; the Player ships its own."""
    shipped = UNIT.parent / "modules-load.d" / "arrt-player.conf"
    lines = [line.strip() for line in shipped.read_text().splitlines()]
    assert "i2c-dev" in [line for line in lines if line and not line.startswith("#")]
