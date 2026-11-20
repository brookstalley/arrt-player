"""The unit that runs the Player keeps the properties that keep the wall up.

Carried from Arrt's repository-wide guards when Arrt Player moved into a
repository of its own, where they held both of the units that ran the wall. Each
property below was a failure on a real machine first, and each is invisible when
it holds: a unit that has lost one still starts, and goes wrong only on the day
the fault it guarded against comes back.
"""

import inspect
import pathlib

from arrt_player.client import IDENTITY_READ_BUDGET_SECONDS
from arrt_player.config import DEFAULT_CLIENT_POLL_SECONDS, ClientSettings
from arrt_player.mpv import LOAD_TIMEOUT_SECONDS, START_TIMEOUT_SECONDS, stop_group
from arrt_player.notify import PET_MIN_SECONDS, SETUP_PET_SECONDS
from arrt_player.programmes.slideshow import NOT_SHOWN_PAUSE_S
from arrt_player.pull import ClientPull

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


def _uv_run_options() -> list[str]:
    """What `ExecStart` passes `uv run`, up to the command it runs."""
    (exec_start,) = _directives()["ExecStart"]
    words = exec_start.split()
    assert words[1] == "run", f"ExecStart= must be `uv run …`, got {exec_start!r}"
    return words[2 : words.index("python")]


def test_the_unit_starts_frozen_and_without_the_dev_tools():
    """`uv run` without `--frozen` checks the lock against `pyproject.toml` and
    re-resolves over the network when they differ, so an offline boot fails every
    `RestartSec` while the wall holds a good cache; without `--no-dev` it installs
    pytest and the linters on the Pi. The flags go before `python`, where uv reads
    them, and the two groups are the ones the runbook syncs, without which a start
    would leave the label with no typesetter or driver."""
    options = _uv_run_options()
    assert "--frozen" in options, options
    assert "--no-dev" in options, options
    groups = {options[i + 1] for i, word in enumerate(options) if word == "--group"}
    assert groups == {"raster", "epaper"}, options


def test_the_unit_bounds_its_memory_below_the_pis():
    """Without a bound, a Player that exhausts the Pi takes sshd and the journal
    with it. A share of RAM fits every Pi, and `MemoryHigh` below `MemoryMax`
    reclaims and slows the Player before the kernel kills it."""
    directives = _directives()
    (high,) = directives.get("MemoryHigh", [None])
    (most,) = directives.get("MemoryMax", [None])
    assert str(most).endswith("%"), f"MemoryMax= must be a share of the Pi's RAM, got {most!r}"
    assert str(high).endswith("%"), f"MemoryHigh= must be a share of the Pi's RAM, got {high!r}"
    assert int(high[:-1]) < int(most[:-1]) < 100, (high, most)


def test_the_unit_is_sandboxed_as_far_as_tvpi_owning_the_checkout_allows():
    """The slideshow's mpv decodes strangers' posts in this unit. These are the
    lines that hold while the checkout is tvpi's and `uv run` syncs it at start;
    `ProtectSystem=strict` waits on making it root's. The address families are an
    allowlist of what the Player and mpv open, so a family added to the list is a
    decision rather than a default."""
    directives = _directives()
    assert directives.get("NoNewPrivileges") == ["yes"]
    assert directives.get("PrivateTmp") == ["yes"]
    assert directives.get("ProtectSystem") == ["full"], "strict would make the checkout uv syncs read-only"
    assert directives.get("ProtectHome") == ["yes"]
    (families,) = directives.get("RestrictAddressFamilies", [""])
    assert not families.startswith("~"), "RestrictAddressFamilies= must be an allowlist"
    assert set(families.split()) == {"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"}, families


def test_the_sandbox_leaves_the_devices_the_walls_draw_through():
    """The display card, the render node, SPI, GPIO and the DDC bus are reached
    through `SupplementaryGroups=`. Each of these would hide or forbid one of them,
    and the wall would go dark at the next restart with the unit `active`.

    `ProtectClock=` is one of them though it names no device: systemd.exec(5)
    says it implies `DeviceAllow=char-rtc r`, and under the default
    `DevicePolicy=auto` one `DeviceAllow=` makes the policy an allowlist, which
    refuses every device the walls draw through. `MemoryDenyWriteExecute=` and
    `ProcSubset=` are here for the code mpv's drivers map and the board facts
    the GPIO library reads, which the unit's comment names."""
    directives = _directives()
    present = sorted(
        {
            "PrivateDevices",
            "DevicePolicy",
            "DeviceAllow",
            "ProtectKernelModules",
            "ProtectKernelTunables",
            "ProtectClock",
            "MemoryDenyWriteExecute",
            "ProcSubset",
        }
        & directives.keys()
    )
    assert not present, f"display.service must not restrict the devices: {present}"


def test_the_unit_is_restricted_as_far_as_needs_no_path_device_or_update_step():
    """The owner's 2026-11-19 ruling keeps the checkout tvpi's and `ProtectSystem`
    at `full`. These are the restrictions that change none of that; each is
    checked in the unit's comment against what the Player and mpv do."""
    directives = _directives()
    restricted = {
        name: directives.get(name)
        for name in (
            "LockPersonality",
            "RestrictNamespaces",
            "RestrictSUIDSGID",
            "RestrictRealtime",
            "ProtectHostname",
            "ProtectKernelLogs",
            "ProtectControlGroups",
            "SystemCallArchitectures",
            "CapabilityBoundingSet",
        )
    }
    assert restricted == {
        "LockPersonality": ["yes"],
        "RestrictNamespaces": ["yes"],
        "RestrictSUIDSGID": ["yes"],
        "RestrictRealtime": ["yes"],
        "ProtectHostname": ["yes"],
        "ProtectKernelLogs": ["yes"],
        "ProtectControlGroups": ["yes"],
        "SystemCallArchitectures": ["native"],
        # Empty: no capability in the bounding set at all.
        "CapabilityBoundingSet": [""],
    }


def test_the_unit_waits_for_the_players_word_and_ends_one_that_stops_making_progress():
    """`Type=notify` with a watchdog, which the Player answers (`notify.py`).

    **`NotifyAccess=all` because `ExecStart` is `uv run`**: uv is the unit's
    main process and the Player its child, and `main` would drop every message
    the Player sends, so no start would ever complete. **SIGTERM**, so a Player
    the watchdog ends takes the orderly stop that closes the art channel, and
    **the abort window is the stop window**, stated, so a watchdog's SIGTERM
    inside a television connection gets the time `systemctl stop`'s does.
    `Restart=always` restarts after a watchdog (systemd.service(5), Table 1)."""
    directives = _directives()
    assert directives.get("Type") == ["notify"]
    assert _uv_run_options() is not None, "ExecStart= is `uv run`, which is why NotifyAccess= is all"
    assert directives.get("NotifyAccess") == ["all"], "uv is the main process; `main` drops the Player's READY=1"
    assert directives.get("WatchdogSignal") == ["SIGTERM"], "the watchdog must take the stop path that closes the art channel"
    assert directives.get("TimeoutAbortSec") == directives.get("TimeoutStopSec")
    assert directives.get("Restart") == ["always"]


#: What no constant bounds, given its own share of `WatchdogSec`: the first
#: pass's integrity read of every composed picture on the loop (the operator's
#: check 12 measures it), a retired wall's directory removed inside a cycle, a
#: Pi reclaiming memory at `MemoryHigh`. Until check 12 is measured, more than
#: the longest bounded gap itself.
UNBOUNDED_STALLS_SECONDS = 120.0


async def test_the_watchdog_outlasts_the_longest_honest_gap_in_each_mode(client_settings: ClientSettings):
    """A watchdog shorter than an honest gap between two pets restarts a healthy Player, and a restart reconnects to the Frame.

    Each gap is summed from the constants that bound it, read from the code:
    in Arrt mode a supervisor cycle (the client poll, the Frame's identity read,
    the heartbeat and the document, each at the client session's total) and the
    pet rate limit; in slideshow mode the longest wait and the longest load
    (mpv stopped and started, the file put up) and the rate limit; in setup mode
    its period. A constant raised past the unit's number fails here by name.
    """
    link = ClientPull(client_settings)
    try:
        request = link._client().timeout.total  # the bound the cycle actually runs under
    finally:
        await link.close()
    assert request is not None, "the client session has no total timeout, so no cycle is bounded"
    mpv_grace = inspect.signature(stop_group).parameters["grace"].default
    # `MpvOutput._stop`: `quit` with a 1 s timeout, then the group's grace, then SIGKILL and the grace again.
    mpv_stop = 1.0 + 2 * mpv_grace
    gaps = {
        "arrt": DEFAULT_CLIENT_POLL_SECONDS + IDENTITY_READ_BUDGET_SECONDS + 2 * request + PET_MIN_SECONDS,
        "slideshow": NOT_SHOWN_PAUSE_S + mpv_stop + 2 * START_TIMEOUT_SECONDS + 4 * LOAD_TIMEOUT_SECONDS + PET_MIN_SECONDS,
        "setup": SETUP_PET_SECONDS + PET_MIN_SECONDS,
    }
    (watchdog,) = _directives().get("WatchdogSec", [None])
    assert watchdog is not None, "display.service has no WatchdogSec="
    short = {mode: gap for mode, gap in gaps.items() if float(watchdog) < gap + UNBOUNDED_STALLS_SECONDS}
    assert not short, f"WatchdogSec={watchdog} leaves less than {UNBOUNDED_STALLS_SECONDS:g} s of stalls above {short}"


def test_a_start_that_syncs_is_not_killed_before_it_finishes():
    """Type=simple had no start deadline. Under `Type=notify` a start without
    `READY=1` is failed at `TimeoutStartSec`, and `uv run` installs what the
    venv lacks before the Player runs; systemd's default 90 s would kill that
    install on every start, and the wall would never come back."""
    (timeout,) = _directives().get("TimeoutStartSec", ["90"])
    assert float(timeout) > 90, f"TimeoutStartSec={timeout} is no more than the default that would kill a syncing start"


def test_the_directives_the_sandbox_must_not_move_are_as_they_were():
    """Each of these was a failure on a real machine before it was a line here
    (the tests above say which), and hardening the unit is not a reason to touch
    any of them."""
    directives = _directives()
    kept = {name: directives.get(name) for name in ("KillSignal", "TimeoutStopSec", "UMask", "StartLimitIntervalSec")}
    assert kept == {
        "KillSignal": ["SIGTERM"],
        "TimeoutStopSec": ["90"],
        "UMask": ["0077"],
        "StartLimitIntervalSec": ["0"],
    }
    assert directives.get("SupplementaryGroups") == ["spi gpio video render i2c"]


#: The two documents the operator follows on the Pi.
DOCUMENTS = (
    UNIT.parent / "README.md",
    UNIT.parents[1] / ".prawduct" / "runbooks" / "moving-the-pi-player-to-a-new-revision.md",
)


def _uv_commands(document: pathlib.Path) -> list[str]:
    """Each `uv sync`, and each `uv run` as the service account, in a document, with continued lines joined."""
    joined = document.read_text().replace("\\\n", " ")
    lines = [" ".join(line.split()) for line in joined.splitlines()]
    return [line for line in lines if "uv sync" in line or ("uv run" in line and "sudo -u tvpi" in line)]


def test_every_sync_the_operator_runs_on_the_pi_is_frozen():
    """A sync that is not frozen re-resolves the lock over the network when it
    disagrees with `pyproject.toml`, and may rewrite `uv.lock` in the checkout,
    which the runbook's next `git checkout` then refuses. The Player's syncs also
    leave out the dev group, as the unit does."""
    commands = [(document.name, line) for document in DOCUMENTS for line in _uv_commands(document)]
    assert any(name.endswith("revision.md") for name, _ in commands), "the runbook's sync was not found"
    unfrozen = [line for _, line in commands if "--frozen" not in line]
    assert not unfrozen, unfrozen
    with_dev = [line for _, line in commands if "--group raster" in line and "--no-dev" not in line]
    assert not with_dev, with_dev
