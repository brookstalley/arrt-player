"""Deployment values: what stops the process, and what quietly defaults.

The distinction is the whole point of this module. A value nobody typed gets a
default when there is a right answer and a refusal when there is not; a value
somebody typed *and got wrong* always refuses, because substituting a default
there hides the typo behind behaviour that looks deliberate.

**A Player is a client** (`clients.md`): it is told its server, its token and its
cache, and learns its walls from the server. The Frame is an output it may or
may not have, so the Frame's own required values are required only on a client
configured with one.
"""

import ast
import re
from pathlib import Path

import pytest

from arrt_player import config, pull
from arrt_player.config import (
    CACHE_DIR_NAMES,
    DEFAULT_UI_BIND,
    DEFAULT_UI_PORT,
    PAGE_KEYS_READ,
    PLAIN_COMPONENT,
    RETIRED_SETTINGS,
    ConfigError,
    SetupRequired,
    SlideshowSettings,
    WallIdUnusable,
    load,
    load_ui,
    resolve,
    tv_address_problem,
)
from arrt_player.settings_ui import server as settings_server
from arrt_player.settings_ui import store

FRAME = {
    "TV_ADDRESS": "10.0.0.1",
    "LATITUDE": "45.68",
    "LONGITUDE": "-111.04",
    "LOCATION_NAME": "Bozeman",
}


def an_environment(cache_dir: Path, *, frame: bool = True, **overrides: str) -> dict[str, str]:
    environment = {
        "SERVER_URL": "http://127.0.0.1:8770/",
        "CLIENT_TOKEN": "the-clients-token",
        "CACHE_DIR": str(cache_dir),
        **(FRAME if frame else {}),
    }
    environment.update(overrides)
    return environment


class TestWhatMustBeSet:
    def test_a_client_without_its_cache_does_not_start(self, cache_dir: Path):
        """The cache is where the settings page keeps what it is told, so there is no page without it."""
        environment = an_environment(cache_dir, frame=False)
        del environment["CACHE_DIR"]

        with pytest.raises(ConfigError, match="CACHE_DIR"):
            load(environment)
        with pytest.raises(ConfigError, match="CACHE_DIR"):
            load_ui(environment)

    @pytest.mark.parametrize("missing", ["SERVER_URL", "CLIENT_TOKEN"])
    def test_a_client_without_its_server_or_its_token_starts_in_setup_mode(self, cache_dir: Path, missing: str):
        """Owner, 2026-11-10: the settings page is how they get set, so their absence starts it rather than refusing.

        `SetupRequired` is asserted not to be a `ConfigError`, because the entry
        point answers that one with exit 2 and a handler catching it would turn
        setup mode back into a refusal.
        """
        environment = an_environment(cache_dir, frame=False)
        del environment[missing]

        with pytest.raises(SetupRequired) as raised:
            load(environment)

        assert raised.value.missing == (missing,)
        assert not isinstance(raised.value, ConfigError)

    def test_an_empty_value_is_as_missing_as_an_absent_one(self, cache_dir: Path):
        """`.env.example` ships `SERVER_URL=`, so a `.env` copied from it says nothing rather than pinning an empty server."""
        with pytest.raises(SetupRequired) as raised:
            load(an_environment(cache_dir, frame=False, SERVER_URL="", CLIENT_TOKEN=""))

        assert raised.value.missing == ("SERVER_URL", "CLIENT_TOKEN")


class TestTheSettingsPagesFile:
    """The page's values sit beneath the environment: they fill what it leaves empty and nothing else."""

    def test_the_page_supplies_what_the_environment_does_not(self, cache_dir: Path):
        store.write(cache_dir, store.Stored(values={"SERVER_URL": "http://arrt.example:8770", "CLIENT_TOKEN": "from-page"}))
        environment = an_environment(cache_dir, frame=False)
        del environment["SERVER_URL"]
        del environment["CLIENT_TOKEN"]

        settings = load(environment)

        assert (settings.server_url, settings.client_token) == ("http://arrt.example:8770", "from-page")

    def test_the_environment_wins_over_the_page(self, cache_dir: Path):
        store.write(cache_dir, store.Stored(values={"SERVER_URL": "http://from-the-page:8770"}))

        assert (
            load(an_environment(cache_dir, frame=False, SERVER_URL="http://from-env:8770")).server_url == "http://from-env:8770"
        )

    def test_an_empty_environment_value_does_not_pin_over_the_page(self, cache_dir: Path):
        store.write(cache_dir, store.Stored(values={"SERVER_URL": "http://from-the-page:8770"}))

        assert load(an_environment(cache_dir, frame=False, SERVER_URL="")).server_url == "http://from-the-page:8770"

    def test_the_page_shows_read_only_exactly_the_managed_keys_the_environment_sets(self, cache_dir: Path):
        """Set and managed is pinned; set and empty is not; set and not the page's (`TV_PORT`) is not."""
        ui = load_ui(an_environment(cache_dir, frame=False, PLAYER_NAME="", TV_ADDRESS="10.0.0.1", TV_PORT="8002"))

        assert ui.pinned == {"SERVER_URL", "CLIENT_TOKEN", "TV_ADDRESS"}

    def test_an_untrustworthy_file_refuses_to_start_and_names_it(self, cache_dir: Path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        store.path_in(cache_dir).write_text('{"version": 99}', encoding="utf-8")

        for reader in (load, load_ui):
            with pytest.raises(ConfigError, match=r"\.settings\.json"):
                reader(an_environment(cache_dir, frame=False))


class TestWhereThePageListens:
    def test_every_interface_on_its_own_port_by_default(self, cache_dir: Path):
        ui = load_ui(an_environment(cache_dir, frame=False))

        assert (ui.bind, ui.port) == (DEFAULT_UI_BIND, DEFAULT_UI_PORT)
        assert DEFAULT_UI_BIND == "0.0.0.0"  # noqa: S104 -- asserting the default, binding nothing

    def test_bind_and_port_are_the_environments(self, cache_dir: Path):
        ui = load_ui(an_environment(cache_dir, frame=False, UI_BIND="127.0.0.1", UI_PORT="9123"))

        assert (ui.bind, ui.port) == ("127.0.0.1", 9123)

    @pytest.mark.parametrize("port", ["0", "65536", "http"])
    def test_a_port_that_is_not_one_is_refused_by_name(self, cache_dir: Path, port: str):
        with pytest.raises(ConfigError, match="UI_PORT"):
            load_ui(an_environment(cache_dir, frame=False, UI_PORT=port))

    @pytest.mark.parametrize("name", ["CACHE_DIR", "TV_TOKEN_FILE"])
    def test_a_relative_path_to_the_players_state_is_refused_by_name(self, cache_dir: Path, name: str):
        """A relative path resolves inside the checkout, where a redeploy that removes a project directory deletes it."""
        with pytest.raises(ConfigError, match=f"{name} must be an absolute path"):
            load(an_environment(cache_dir, **{name: "cache/state"}))

    def test_a_home_relative_cache_is_absolute_once_expanded(self, cache_dir: Path):
        assert load(an_environment(cache_dir, CACHE_DIR="~/arrt-player-cache")).cache_dir.is_absolute()

    @pytest.mark.parametrize("retired", sorted(RETIRED_SETTINGS))
    def test_a_setting_a_one_wall_player_read_is_refused_by_name(self, cache_dir: Path, retired: str):
        """A stale `.env` fails loudly rather than being half-read.

        Each of these was read once at start and never again, so a Player that
        ignored one would start, pull nothing for the wall its operator believes
        it serves, and say nothing about why.
        """
        with pytest.raises(ConfigError, match=f"{retired} is retired") as refused:
            load(an_environment(cache_dir, **{retired: "anything"}))

        assert RETIRED_SETTINGS[retired] in str(refused.value)

    @pytest.mark.parametrize(
        ("retired", "replaced_by"),
        [
            ("WALL_ID", "Settings › Clients"),
            ("WALL_TOKEN", "CLIENT_TOKEN"),
            ("MANIFEST_SOURCE", "SERVER_URL"),
        ],
    )
    def test_the_refusal_says_what_replaced_it(self, cache_dir: Path, retired: str, replaced_by: str):
        with pytest.raises(ConfigError, match=replaced_by):
            load(an_environment(cache_dir, **{retired: "anything"}))

    def test_a_retired_setting_left_empty_is_not_set(self, cache_dir: Path):
        """`WALL_ID=` is a key nobody filled in, which is the same as no key."""
        assert load(an_environment(cache_dir, WALL_ID="", WALL_TOKEN="", MANIFEST_SOURCE="")).server_url

    def test_the_servers_pace_settings_in_a_shared_env_are_left_to_the_server(self, cache_dir: Path):
        """`ROTATION_INTERVAL_SECONDS` and `ROTATION_SHUFFLE` are Arrt's: the default pace a theme inherits.

        One `.env` serves both on a development checkout, so a Player that
        refused them would not start beside its server. It reads neither: the
        schedule it is sent sets the pace.
        """
        settings = load(an_environment(cache_dir, ROTATION_INTERVAL_SECONDS="931", ROTATION_SHUFFLE="false"))

        assert settings.server_url
        assert "ROTATION_INTERVAL_SECONDS" not in RETIRED_SETTINGS
        assert "ROTATION_SHUFFLE" not in RETIRED_SETTINGS

    @pytest.mark.parametrize("missing", ["LATITUDE", "LONGITUDE", "LOCATION_NAME"])
    def test_a_frame_without_the_sun_it_follows_does_not_start(self, cache_dir: Path, missing: str):
        environment = an_environment(cache_dir)
        del environment[missing]

        with pytest.raises(ConfigError, match=missing):
            load(environment)

    def test_a_number_that_is_not_one_is_refused_rather_than_defaulted(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="TV_PORT"):
            load(an_environment(cache_dir, TV_PORT="eight-thousand"))

    def test_a_label_panel_with_no_frame_is_a_client_with_a_label_output(self, cache_dir: Path):
        """**Reversed on purpose** (`labels-and-surfaces.md` ruling 3): a label output
        may live on a client with no display at all, and the server maps it to a
        wall on any client. So `EPD_DEVICE` without `TV_ADDRESS` starts, with no
        Frame and a panel."""
        settings = load(an_environment(cache_dir, frame=False, EPD_DEVICE="waveshare_epd.it8951"))

        assert settings.frame is None
        assert settings.panel.epd_device == "waveshare_epd.it8951"


class TestOnePanelIsTheRecordedLimit:
    """The owner's ruling of 2026-11-19: one label panel per Player is recorded as
    the limit for now, and nothing may close off several. So a configuration
    naming two panels is refused at start, by name, citing the limit as the
    ruling's — never handed to the driver as one identifier (which would fail as
    a panel that will not open, and read as broken hardware), and never cut down
    to its first panel with the second silently undriven."""

    @pytest.mark.parametrize(
        "named",
        ["waveshare_epd.it8951,omni_epd.mock", "waveshare_epd.it8951 omni_epd.mock", "waveshare_epd.it8951; omni_epd.mock"],
    )
    def test_epd_device_naming_two_panels_is_refused_by_name_citing_the_ruling(self, cache_dir: Path, named: str):
        with pytest.raises(ConfigError) as refused:
            load(an_environment(cache_dir, frame=False, EPD_DEVICE=named))

        said = str(refused.value)
        assert "EPD_DEVICE" in said
        assert "2 panels" in said, "the refusal does not say how many panels the value names"
        assert "waveshare_epd.it8951" in said
        assert "omni_epd.mock" in said
        assert "one label panel" in said, "the refusal does not name the limit"
        assert "ruling of 2026-11-19" in said, "the refusal does not cite the ruling the limit is"
        assert "not a limit of the design" in said

    def test_the_slideshow_refuses_two_panels_as_a_client_does(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="EPD_DEVICE names 2 panels"):
            load(a_slideshow(cache_dir, EPD_DEVICE="waveshare_epd.it8951,omni_epd.mock"))

    def test_one_panel_with_spaces_around_it_is_one_panel(self, cache_dir: Path):
        settings = load(an_environment(cache_dir, frame=False, EPD_DEVICE="  omni_epd.mock  "))

        assert settings.panel.epd_device == "omni_epd.mock"


class TestDrawingToFiles:
    """`FILE_OUTPUT_DIR`: a Player with no hardware, which must be impossible to turn on by accident on a wall.

    Off unless the directory is named; every value of its three keys refused by
    name when it is unusable; never beside a key that drives hardware, never in
    slideshow mode, and never inside the cache the Player tidies.
    """

    def test_unset_a_player_draws_to_no_files(self, cache_dir: Path):
        settings = load(an_environment(cache_dir))

        assert settings.files is None
        assert "FILE_OUTPUT_DIR is not set" in str(settings.startup_lines()["files"])

    def test_the_directory_alone_is_a_1080p_screen_and_no_label(self, cache_dir: Path, tmp_path: Path):
        settings = load(an_environment(cache_dir, frame=False, FILE_OUTPUT_DIR=str(tmp_path / "drawn")))

        assert settings.files == config.FileOutputSettings(directory=tmp_path / "drawn", screen=(1920, 1080), label=False)
        assert settings.startup_lines()["files"]["directory"] == str(tmp_path / "drawn")

    def test_its_size_and_its_label_are_read(self, cache_dir: Path, tmp_path: Path):
        settings = load(
            an_environment(
                cache_dir,
                frame=False,
                FILE_OUTPUT_DIR=str(tmp_path / "drawn"),
                FILE_OUTPUT_SCREEN="640X360",
                FILE_OUTPUT_LABEL="on",
            )
        )

        assert settings.files is not None
        assert (settings.files.screen, settings.files.label) == ((640, 360), True)

    @pytest.mark.parametrize(
        "value", ["0x1080", "1920x0", "-1920x1080", "1920", "1920x1080x3", "widexhigh", "1920 x 1080", "16385x1080", "1e3x1080"]
    )
    def test_a_size_that_is_not_one_is_refused_by_name(self, cache_dir: Path, tmp_path: Path, value: str):
        with pytest.raises(ConfigError, match="FILE_OUTPUT_SCREEN"):
            load(an_environment(cache_dir, frame=False, FILE_OUTPUT_DIR=str(tmp_path / "drawn"), FILE_OUTPUT_SCREEN=value))

    def test_the_largest_side_is_accepted(self, cache_dir: Path, tmp_path: Path):
        side = config.FILE_OUTPUT_MAX_SIDE_PX
        settings = load(
            an_environment(cache_dir, frame=False, FILE_OUTPUT_DIR=str(tmp_path / "drawn"), FILE_OUTPUT_SCREEN=f"{side}x1")
        )

        assert settings.files is not None
        assert settings.files.screen == (side, 1)

    @pytest.mark.parametrize("value", ["yes", "true", "1", "label"])
    def test_a_label_switch_that_is_not_on_or_off_is_refused_by_name(self, cache_dir: Path, tmp_path: Path, value: str):
        with pytest.raises(ConfigError, match="FILE_OUTPUT_LABEL"):
            load(an_environment(cache_dir, frame=False, FILE_OUTPUT_DIR=str(tmp_path / "drawn"), FILE_OUTPUT_LABEL=value))

    @pytest.mark.parametrize("key", ["FILE_OUTPUT_SCREEN", "FILE_OUTPUT_LABEL"])
    def test_its_other_keys_without_the_directory_are_refused_by_name(self, cache_dir: Path, key: str):
        value = "on" if key == "FILE_OUTPUT_LABEL" else "640x360"

        with pytest.raises(ConfigError) as refused:
            load(an_environment(cache_dir, frame=False, **{key: value}))

        assert key in str(refused.value)
        assert "FILE_OUTPUT_DIR is not" in str(refused.value)

    def test_a_relative_directory_is_refused_by_name(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="FILE_OUTPUT_DIR must be an absolute path"):
            load(an_environment(cache_dir, frame=False, FILE_OUTPUT_DIR="drawn"))

    @pytest.mark.parametrize("hardware", list(config.FILE_OUTPUT_EXCLUDES))
    def test_it_is_refused_beside_a_key_that_drives_hardware(self, cache_dir: Path, tmp_path: Path, hardware: str):
        environment = an_environment(cache_dir, frame=True, FILE_OUTPUT_DIR=str(tmp_path / "drawn"))
        environment.pop("TV_ADDRESS")
        environment[hardware] = "10.0.0.1" if hardware == "TV_ADDRESS" else "omni_epd.mock"

        with pytest.raises(ConfigError) as refused:
            load(environment)

        assert "FILE_OUTPUT_DIR" in str(refused.value)
        assert hardware in str(refused.value)

    @pytest.mark.parametrize("where", ["the cache", "inside it", "around it"])
    def test_it_is_refused_where_the_player_tidies(self, cache_dir: Path, where: str):
        directory = {"the cache": cache_dir, "inside it": cache_dir / "drawn", "around it": cache_dir.parent}[where]

        with pytest.raises(ConfigError, match=r"FILE_OUTPUT_DIR .*CACHE_DIR"):
            load(an_environment(cache_dir, frame=False, FILE_OUTPUT_DIR=str(directory)))

    @pytest.mark.parametrize("key", list(config.FILE_OUTPUT_KEYS))
    def test_a_slideshow_refuses_each_of_its_keys_by_name(self, cache_dir: Path, tmp_path: Path, key: str):
        value = {"FILE_OUTPUT_DIR": str(tmp_path / "drawn"), "FILE_OUTPUT_SCREEN": "640x360", "FILE_OUTPUT_LABEL": "on"}[key]

        with pytest.raises(ConfigError) as refused:
            load(a_slideshow(cache_dir, **{key: value}))

        assert key in str(refused.value)
        assert "arrt mode only" in str(refused.value)

    @pytest.mark.parametrize("key", list(config.FILE_OUTPUT_KEYS))
    def test_each_key_is_documented_under_the_heading_the_servers_test_quotes(self, key: str):
        example = (Path(config.__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
        section = example.split("# --- Drawing to files: a Player with no hardware", 1)

        assert len(section) == 2, "the heading the server's test quotes is gone from .env.example"
        assert re.search(rf"^{key}=$", section[1].split("\n# --- ", 1)[0], re.MULTILINE), f"{key} is not under the heading"


class TestTheFrameIsAnOutputAClientMayHave:
    def test_tv_address_present_is_a_frame(self, cache_dir: Path):
        settings = load(an_environment(cache_dir))

        assert settings.frame is not None
        assert settings.frame.tv_address == "10.0.0.1"

    def test_tv_address_absent_is_no_frame_and_needs_none_of_its_values(self, cache_dir: Path):
        settings = load(an_environment(cache_dir, frame=False))

        assert settings.frame is None
        with pytest.raises(ValueError, match="no Frame"):
            settings.frame_wall("living-room")


class TestWhatDefaults:
    def test_a_client_with_a_frame_needs_seven_values(self, cache_dir: Path):
        settings = load(an_environment(cache_dir))

        assert settings.server_url == "http://127.0.0.1:8770", "a trailing slash doubled every route's first one"
        assert settings.client_token == "the-clients-token"
        assert settings.cache_dir == cache_dir
        assert settings.poll_interval_seconds == 1.0
        assert settings.client_poll_seconds == 30.0
        assert settings.frame.tv_port == 8002
        assert settings.panel.epd_panel_width_px == 1448
        assert settings.panel.epd_panel_height_px == 1072
        assert settings.frame.tv_client_name == "tvpi"
        assert settings.frame.tv_token_file == cache_dir / "token_file"

    def test_the_panel_is_configurable_because_nothing_may_hardcode_one(self, cache_dir: Path):
        """This deployment is a 1448×1072 IT8951; the product must run on any."""
        settings = load(an_environment(cache_dir, EPD_PANEL_WIDTH_PX="800", EPD_PANEL_HEIGHT_PX="600"))

        assert (settings.panel.epd_panel_width_px, settings.panel.epd_panel_height_px) == (800, 600)

    def test_the_three_values_that_decide_whether_this_device_has_a_panel(self, cache_dir: Path):
        """**The names a misspelling makes invisible.**

        These three are the whole of what `.env` says about the label surface, and
        every other test in the Player builds its settings directly — so a misspelt
        key or a wrong default here would leave `epd_device` empty on a Pi that has
        a panel, `label_surface` would return None, and the heartbeat would report
        a device with no panel. That is the exact distinction the Player was built
        to draw, collapsed by a typo nothing else would catch.
        """
        panel = load(
            an_environment(cache_dir, EPD_DEVICE="waveshare_epd.it8951", EPD_MARGIN_PX="64", EPD_ROTATE_DEGREES="0")
        ).panel

        assert panel.epd_device == "waveshare_epd.it8951"
        assert panel.epd_margin_px == 64
        assert panel.epd_rotate_degrees == 0

    def test_a_deployment_that_says_nothing_about_a_panel_has_none(self, cache_dir: Path):
        """The supported deployment rather than the degraded one.

        The rotation still takes the reference deployment's default, because it
        describes how a panel is used rather than whether there is one. The margin
        does not: it derives from the type, so this value is an override nobody
        has exercised rather than a default everybody inherits.
        """
        panel = load(an_environment(cache_dir)).panel

        assert panel.epd_device == ""
        assert panel.epd_margin_px is None
        assert panel.epd_rotate_degrees == 180

    def test_the_viewing_conditions_have_no_defaults_and_must_not_acquire_any(self, cache_dir: Path):
        """**The one pair in this module that may never be guessed.**

        A wrong *viewing distance* is not visible at all — it produces type nobody
        can read from where they stand, while the daemon starts, the panel draws
        and every test passes. That is not hypothetical; it is what shipped. A
        default here would restore it.
        """
        stated = load(an_environment(cache_dir, EPD_PANEL_DIAGONAL_INCHES="6", EPD_VIEWING_DISTANCE_INCHES="84")).panel
        assert (stated.epd_panel_diagonal_inches, stated.epd_viewing_distance_inches) == (6.0, 84.0)

        unstated = load(an_environment(cache_dir)).panel
        assert unstated.epd_panel_diagonal_inches is None
        assert unstated.epd_viewing_distance_inches is None

    def test_a_viewing_measurement_that_is_not_a_number_is_refused_rather_than_dropped(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="EPD_VIEWING_DISTANCE_INCHES"):
            load(an_environment(cache_dir, EPD_VIEWING_DISTANCE_INCHES="seven feet"))


class TestEachWallHasItsOwnDirectory:
    """**The paths are derived, never configured**, and that is the whole mechanism
    keeping two walls on one client apart: a wall's worker opens exactly the files
    its id names, so another room's are files it never opens."""

    def test_a_walls_files_are_under_its_own_directory_in_the_cache(self, cache_dir: Path):
        wall = load(an_environment(cache_dir)).wall("study")

        assert wall.wall_dir == cache_dir / "study"
        assert wall.manifest_path == cache_dir / "study" / "manifest.json"
        assert wall.render_root == wall.heartbeat_root == cache_dir / "study"
        assert wall.state_path == cache_dir / "study" / "display-state.sqlite"

    def test_two_walls_on_one_client_share_no_file(self, cache_dir: Path):
        settings = load(an_environment(cache_dir))
        study, hall = settings.wall("study"), settings.wall("hall")

        assert {study.manifest_path, study.state_path, study.wall_dir}.isdisjoint(
            {hall.manifest_path, hall.state_path, hall.wall_dir}
        )

    def test_a_wall_on_the_frame_carries_the_frame_and_the_wall(self, cache_dir: Path):
        settings = load(an_environment(cache_dir, TV_PORT="8003"))
        on_the_frame = settings.frame_wall("study")

        assert on_the_frame.wall_dir == cache_dir / "study"
        assert on_the_frame.tv_port == 8003
        assert on_the_frame.client_token == "the-clients-token"

    @pytest.mark.parametrize("unusable", ["..", ".", "a/b", "../elsewhere", ".client", "", "wall id", "x\x00y"])
    def test_a_wall_id_that_is_not_a_plain_directory_name_is_refused(self, cache_dir: Path, unusable: str):
        """The server mints UUIDs; an id that would put a wall's files outside its own directory is refused by name."""
        with pytest.raises(WallIdUnusable):
            load(an_environment(cache_dir)).wall(unusable)

    @pytest.mark.parametrize("reserved", sorted(CACHE_DIR_NAMES | {pull.CLIENT_ETAG_FILENAME}))
    def test_a_wall_id_that_names_a_file_the_client_keeps_beside_the_walls_is_refused(self, cache_dir: Path, reserved: str):
        """A wall called `token_file` would put its manifest inside the television's pairing token."""
        client = load(an_environment(cache_dir))

        for spelling in (reserved, reserved.upper()):
            with pytest.raises(WallIdUnusable):
                client.wall(spelling)

    def test_the_names_reserved_are_the_ones_the_client_writes(self, cache_dir: Path):
        """The reserved set is read from the paths themselves, so a rename that leaves it behind turns this red."""
        settings = load(an_environment(cache_dir))
        written = {
            settings.frame.tv_token_file,
            settings.client_document_path,
            store.path_in(cache_dir),
        }

        assert {path.relative_to(cache_dir).name for path in written} <= CACHE_DIR_NAMES
        assert all(path.parent == cache_dir for path in written)

    @pytest.mark.parametrize("usable", ["3f2a9c1e-8b7d-4e6f-a1b2-c3d4e5f60718", "living-room", "w_1", "wall.v2"])
    def test_ids_the_server_mints_are_accepted(self, cache_dir: Path, usable: str):
        assert load(an_environment(cache_dir)).wall(usable).wall_dir == cache_dir / usable


#: The values every `*_SECONDS` key refuses. The settings page is held to refuse them too, for the keys it
#: writes (`tests/test_settings_ui.py`), so this list is the one both read.
REFUSED_SECONDS = ["0", "-1", "nan", "inf", "-inf", "1e400"]

#: Every `*_SECONDS` key the configuration reads, from its own source, so a key added later is checked the day it is.
SECONDS_KEYS = sorted(set(re.findall(r'"([A-Z][A-Z0-9_]*_SECONDS)"', Path(config.__file__).read_text(encoding="utf-8"))))


def reading(cache_dir: Path, key: str, value: str) -> dict[str, str]:
    """An environment in which `key` is read: the slideshow's own keys in slideshow mode, the rest on a Frame."""
    if key.startswith("SLIDESHOW_"):
        return a_slideshow(cache_dir, **{key: value})
    return an_environment(cache_dir, **{key: value})


class TestIntervalsAndTimeouts:
    def test_the_keys_are_found(self):
        """Not vacuous: the scan of the source finds the keys this was written for."""
        assert {
            "MANIFEST_POLL_SECONDS",
            "BRIGHTNESS_INTERVAL_SECONDS",
            "TV_UPLOAD_TIMEOUT_SECONDS",
            "TV_UPLOAD_RETRY_SECONDS",
            "TV_SELECT_CONFIRM_SECONDS",
            "TV_CONNECT_TIMEOUT_SECONDS",
            "TV_RETRY_MIN_SECONDS",
            "TV_RETRY_MAX_SECONDS",
            "SLIDESHOW_MIN_SECONDS",
            "SLIDESHOW_MAX_SECONDS",
        } <= set(SECONDS_KEYS)

    @pytest.mark.parametrize("value", REFUSED_SECONDS)
    @pytest.mark.parametrize("key", SECONDS_KEYS)
    def test_zero_a_negative_nan_and_infinity_are_refused_by_name(self, cache_dir: Path, key: str, value: str):
        """`MANIFEST_POLL_SECONDS=0` spins the wall loop and the pull; NaN passes every check built on `<`."""
        with pytest.raises(ConfigError, match=key):
            load(reading(cache_dir, key, value))

    @pytest.mark.parametrize("key", [key for key in SECONDS_KEYS if not key.startswith("SLIDESHOW_")])
    def test_a_fraction_of_a_second_is_accepted(self, cache_dir: Path, key: str):
        load(reading(cache_dir, key, "0.5"))


class TestWhereTheSunIsSeenFrom:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("LATITUDE", "nan"),
            ("LATITUDE", "inf"),
            ("LATITUDE", "90.5"),
            ("LATITUDE", "-91"),
            ("LONGITUDE", "nan"),
            ("LONGITUDE", "-inf"),
            ("LONGITUDE", "180.5"),
            ("LONGITUDE", "-181"),
        ],
    )
    def test_a_coordinate_off_the_globe_is_refused_by_name(self, cache_dir: Path, key: str, value: str):
        with pytest.raises(ConfigError, match=key):
            load(an_environment(cache_dir, **{key: value}))

    @pytest.mark.parametrize(("latitude", "longitude"), [("90", "180"), ("-90", "-180"), ("0", "0")])
    def test_the_edges_of_the_globe_are_on_it(self, cache_dir: Path, latitude: str, longitude: str):
        frame = load(an_environment(cache_dir, LATITUDE=latitude, LONGITUDE=longitude)).frame

        assert (frame.latitude, frame.longitude) == (float(latitude), float(longitude))

    def test_a_measurement_that_is_not_finite_is_refused_by_name(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="EPD_VIEWING_DISTANCE_INCHES"):
            load(an_environment(cache_dir, EPD_VIEWING_DISTANCE_INCHES="nan"))


class TestTheNamesThePageAnswersTo:
    def test_none_are_listed_by_default(self, cache_dir: Path):
        assert load_ui(an_environment(cache_dir, frame=False)).allowed_hosts == frozenset()

    def test_listed_names_are_read_lowercased_without_a_trailing_dot(self, cache_dir: Path):
        ui = load_ui(an_environment(cache_dir, frame=False, UI_ALLOWED_HOSTS=" Wall.LAN., wall.example.com ,,::1"))

        assert ui.allowed_hosts == {"wall.lan", "wall.example.com", "::1"}

    @pytest.mark.parametrize("listed", ["http://wall.lan", "wall.lan:8771", "wall.lan/settings", "wall lan", "-wall.lan"])
    def test_a_url_a_port_or_a_path_is_refused_by_name(self, cache_dir: Path, listed: str):
        """Refused rather than reduced: `http://wall.lan` says the operator meant something this does not do."""
        with pytest.raises(ConfigError, match="UI_ALLOWED_HOSTS"):
            load_ui(an_environment(cache_dir, frame=False, UI_ALLOWED_HOSTS=listed))


class TestTheStartupLine:
    def test_the_clients_line_names_its_server_and_cache_and_says_whether_it_has_a_frame(self, cache_dir: Path):
        with_frame = load(an_environment(cache_dir)).startup_lines()
        without = load(an_environment(cache_dir, frame=False)).startup_lines()

        assert with_frame["server_url"] == "http://127.0.0.1:8770"
        assert with_frame["cache_dir"] == str(cache_dir)
        assert with_frame["frame"]["tv_address"] == "10.0.0.1:8002"
        assert "TV_ADDRESS is not set" in str(without["frame"])

    def test_a_walls_line_names_the_wall_and_both_files_that_follow_from_it(self, cache_dir: Path):
        """**The value whose being wrong has no other symptom.** The heartbeat's
        path is where an operator looks when the health panel says a wall is
        silent, and the manifest's is the file being waited on."""
        lines = load(an_environment(cache_dir)).frame_wall("study").startup_lines()

        assert lines["wall_id"] == "study"
        assert lines["manifest_path"] == str(cache_dir / "study" / "manifest.json")
        assert lines["heartbeat_path"] == str(cache_dir / "study" / "display-heartbeat-study.json")

    def test_the_clients_line_names_its_panel_with_or_without_a_frame(self, cache_dir: Path):
        """The panel is the client's now, so its geometry is on the client's line, Frame or no Frame."""
        for frame in (True, False):
            lines = load(an_environment(cache_dir, frame=frame, EPD_DEVICE="waveshare_epd.it8951")).startup_lines()
            assert lines["panel"]["epd_panel_px"] == "1448x1072"
            assert lines["panel"]["epd_device"] == "waveshare_epd.it8951"

    def test_it_names_the_viewing_conditions_the_type_was_sized_from(self, cache_dir: Path):
        lines = load(an_environment(cache_dir, EPD_PANEL_DIAGONAL_INCHES="6", EPD_VIEWING_DISTANCE_INCHES="84")).startup_lines()

        assert "6.0" in str(lines["panel"]["epd_viewing"])
        assert "84.0" in str(lines["panel"]["epd_viewing"])

    def test_unstated_viewing_conditions_are_reported_as_what_they_cost(self, cache_dir: Path):
        line = str(load(an_environment(cache_dir)).startup_lines()["panel"]["epd_viewing"])

        assert "not stated" in line
        assert "draws none" in line, f"the line does not say what the absence costs: {line}"

    def test_it_names_the_frames_geometry_and_the_mat_it_draws_from(self, cache_dir: Path):
        """The Player composes for the Frame, so a wrong diagonal must be one journal line away."""
        lines = repr(
            load(
                an_environment(cache_dir, TV_PANEL_DIAGONAL_INCHES="55", TV_PANEL_WIDTH_PX="1920", TV_PANEL_HEIGHT_PX="1080")
            ).startup_lines()
        )

        assert "1920x1080 at 55.0 in" in lines
        assert "1.5 in, bottom x1.15" in lines

    def test_the_pairing_token_is_reported_as_a_path_and_never_as_its_contents(self, cache_dir: Path, tmp_path: Path):
        token = tmp_path / "token_file"
        token.write_text("a-real-pairing-token")

        lines = load(an_environment(cache_dir, TV_TOKEN_FILE=str(token))).startup_lines()

        assert lines["frame"]["tv_token_file"] == str(token)
        assert "a-real-pairing-token" not in repr(lines)

    def test_the_client_token_is_in_no_repr_and_no_startup_line(self, cache_dir: Path):
        settings = load(an_environment(cache_dir))
        wall = settings.wall("study")
        on_the_frame = settings.frame_wall("study")

        for said in (repr(settings), repr(wall), repr(on_the_frame), repr(settings.startup_lines())):
            assert "the-clients-token" not in said
        assert "the-clients-token" not in repr(on_the_frame.startup_lines())
        assert "the-clients-token" not in repr(wall.wall_lines())


class TestTheGeometryThePlayerComposesFor:
    """The Frame's size and the mat's proportions, which moved here from the server (`feeds-and-players.md` ruling 7)."""

    def test_unstated_they_are_the_servers_defaults(self, cache_dir: Path):
        settings = load(an_environment(cache_dir))

        assert (settings.frame.tv_panel_width_px, settings.frame.tv_panel_height_px) == (3840, 2160)
        assert settings.frame.tv_panel_diagonal_inches == 42.0
        assert (settings.mat_width_inches, settings.mat_bottom_weight) == (1.5, 1.15)

    def test_the_frame_wall_composes_for_the_configured_panel_at_its_density(self, cache_dir: Path):
        settings = load(
            an_environment(
                cache_dir,
                TV_PANEL_WIDTH_PX="1920",
                TV_PANEL_HEIGHT_PX="1080",
                TV_PANEL_DIAGONAL_INCHES="43",
                MAT_WIDTH_INCHES="2",
                MAT_BOTTOM_WEIGHT="1.3",
            )
        )

        geometry = settings.frame_wall("living-room").geometry

        assert geometry.screen == (1920, 1080)
        assert geometry.pixels_per_inch == pytest.approx((1920**2 + 1080**2) ** 0.5 / 43)
        assert (geometry.mat_width_inches, geometry.bottom_weight) == (2.0, 1.3)

    def test_a_screen_wall_takes_the_mat_proportions_and_no_density(self, cache_dir: Path):
        settings = load(an_environment(cache_dir, frame=False, MAT_BOTTOM_WEIGHT="1.25"))

        geometry = settings.wall("hall").geometry_for((1920, 1200))

        assert geometry.screen == (1920, 1200)
        assert geometry.pixels_per_inch is None
        assert geometry.bottom_weight == 1.25

    @pytest.mark.parametrize(
        "name", ["TV_PANEL_WIDTH_PX", "TV_PANEL_HEIGHT_PX", "TV_PANEL_DIAGONAL_INCHES", "MAT_WIDTH_INCHES", "MAT_BOTTOM_WEIGHT"]
    )
    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_a_size_of_zero_or_below_is_refused_by_name(self, cache_dir: Path, name: str, value: str):
        with pytest.raises(ConfigError, match=name):
            load(an_environment(cache_dir, **{name: value}))

    def test_a_mat_setting_on_a_client_with_no_frame_is_still_read(self, cache_dir: Path):
        """A screen draws a mat too, so the mat is the client's and not the Frame's."""
        with pytest.raises(ConfigError, match="MAT_BOTTOM_WEIGHT"):
            load(an_environment(cache_dir, frame=False, MAT_BOTTOM_WEIGHT="0"))


class TestWhatTheServerKeysMayBe:
    """`config.load` and the settings page judge these by the same functions, so neither accepts what the other refuses."""

    @pytest.mark.parametrize(
        ("url", "said"),
        [
            ("arrt.local:8770", "http://"),
            ("ftp://arrt.local", "http://"),
            ("http://", "with a host"),
            ("http://user:secret@arrt.local:8770", "user name or password"),
            ("http://arrt.local:8770/?a=1", "query"),
            ("http://arrt.local:port", "not an address"),
        ],
    )
    def test_a_server_url_that_is_not_one_is_refused_with_the_reason(self, cache_dir: Path, url: str, said: str):
        with pytest.raises(ConfigError, match=said):
            load(an_environment(cache_dir, frame=False, SERVER_URL=url))

    def test_a_password_in_the_server_url_is_never_echoed(self, cache_dir: Path):
        """`SERVER_URL` is logged at start, which is why userinfo is refused; the refusal must not log it either."""
        with pytest.raises(ConfigError) as raised:
            load(an_environment(cache_dir, frame=False, SERVER_URL="http://user:hunter2-secret@arrt.local"))

        assert "hunter2-secret" not in str(raised.value)

    @pytest.mark.parametrize("url", ["http://127.0.0.1:8770", "https://arrt.example", "http://[::1]:8770/"])
    def test_an_ordinary_address_is_taken(self, cache_dir: Path, url: str):
        assert load(an_environment(cache_dir, frame=False, SERVER_URL=url)).server_url == url.rstrip("/")

    def test_a_token_with_whitespace_is_refused_and_not_echoed(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="CLIENT_TOKEN") as raised:
            load(an_environment(cache_dir, frame=False, CLIENT_TOKEN="half a-token"))

        assert "half a-token" not in str(raised.value)


class TestThePlayersName:
    def test_it_is_read_trimmed_and_named_in_the_startup_line(self, cache_dir: Path):
        settings = load(an_environment(cache_dir, frame=False, PLAYER_NAME="  Hall  "))

        assert settings.player_name == "Hall"
        assert settings.startup_lines()["player_name"] == "Hall"

    def test_unset_it_is_said_to_be(self, cache_dir: Path):
        assert "PLAYER_NAME is not set" in str(load(an_environment(cache_dir, frame=False)).startup_lines()["player_name"])

    def test_one_too_long_is_refused(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="PLAYER_NAME"):
            load(an_environment(cache_dir, frame=False, PLAYER_NAME="x" * 65))

    def test_the_page_is_shown_what_the_environment_pins_but_never_the_token(self, cache_dir: Path):
        """Cleaned as `load` cleans them (no trailing slash, no surrounding space), so the page
        shows the value the Player actually uses, not the one typed into `.env`."""
        ui = load_ui(an_environment(cache_dir, frame=False, PLAYER_NAME="  Hall "))

        assert ui.pinned_values == {"SERVER_URL": "http://127.0.0.1:8770", "PLAYER_NAME": "Hall"}
        assert "the-clients-token" not in repr(ui)


# -- slideshow mode ----------------------------------------------------------------------

#: A cookie of the shape Reddit's is (a JWT), whose last part stands in for the secret.
# prawduct:allow prawduct/credential-shape -- a made-up cookie in a JWT's shape; it signs nothing and Reddit never saw it
COOKIE = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4MTAwMDAwMDB9.the-secret-part"
LISTING = "https://www.reddit.com/r/EarthPorn/top/?t=month"


def a_slideshow(cache_dir: Path, **overrides: str) -> dict[str, str]:
    """Slideshow mode, with no server keys at all."""
    return {
        "CACHE_DIR": str(cache_dir),
        "PLAYER_MODE": "slideshow",
        "SLIDESHOW_URL": LISTING,
        "REDDIT_SESSION": COOKIE,
        **overrides,
    }


class TestSlideshowMode:
    def test_a_slideshow_starts_without_any_server_key(self, cache_dir: Path):
        settings = load(a_slideshow(cache_dir))

        assert isinstance(settings, SlideshowSettings)
        assert (settings.listing_url, settings.reddit_session) == (LISTING, COOKIE)

    @pytest.mark.parametrize("missing", ["SLIDESHOW_URL", "REDDIT_SESSION"])
    def test_a_slideshow_without_its_listing_or_cookie_is_setup_mode_naming_it(self, cache_dir: Path, missing: str):
        environment = a_slideshow(cache_dir, **{missing: ""})

        with pytest.raises(SetupRequired) as raised:
            load(environment)

        assert raised.value.missing == (missing,)
        assert raised.value.mode == "slideshow"

    def test_arrt_mode_still_needs_its_server_keys_and_ignores_the_slideshows(self, cache_dir: Path):
        environment = a_slideshow(cache_dir, PLAYER_MODE="arrt")

        with pytest.raises(SetupRequired) as raised:
            load(environment)

        assert raised.value.missing == ("SERVER_URL", "CLIENT_TOKEN")
        assert raised.value.mode == "arrt"

    def test_no_mode_is_arrt_mode(self, cache_dir: Path):
        environment = a_slideshow(cache_dir, PLAYER_MODE="")

        with pytest.raises(SetupRequired, match="SERVER_URL"):
            load(environment)

    def test_the_mode_can_come_from_the_page(self, cache_dir: Path):
        store.write(
            cache_dir, store.Stored(values={"PLAYER_MODE": "slideshow", "SLIDESHOW_URL": LISTING, "REDDIT_SESSION": COOKIE})
        )

        assert isinstance(load({"CACHE_DIR": str(cache_dir)}), SlideshowSettings)

    @pytest.mark.parametrize(
        ("overrides", "said"),
        [
            ({"PLAYER_MODE": "both"}, "PLAYER_MODE must be arrt or slideshow"),
            ({"SLIDESHOW_URL": "https://imgur.com/r/pics"}, "reddit.com address"),
            ({"SLIDESHOW_URL": "https://www.reddit.com/"}, "names no listing"),
            ({"REDDIT_SESSION": "two words"}, "REDDIT_SESSION"),
            ({"REDDIT_SESSION": "a;b"}, "REDDIT_SESSION"),
            ({"SLIDESHOW_OUTPUT": "frame"}, "HDMI connector"),
            ({"SLIDESHOW_MIN_SECONDS": "4"}, "at least 5"),
            ({"SLIDESHOW_MAX_SECONDS": "3601"}, "at most 3600"),
            ({"SLIDESHOW_MIN_SECONDS": "90"}, "not be more than the maximum"),
            ({"SLIDESHOW_MIN_SECONDS": "soon"}, "numbers of seconds"),
            ({"SLIDESHOW_MAX_SECONDS": "nan"}, "numbers of seconds"),
        ],
    )
    def test_a_slideshow_value_that_is_set_and_wrong_refuses_with_the_reason(self, cache_dir: Path, overrides, said: str):
        with pytest.raises(ConfigError, match=said):
            load(a_slideshow(cache_dir, **overrides))

    def test_unset_dwell_and_output_take_their_defaults(self, cache_dir: Path):
        settings = load(a_slideshow(cache_dir))

        assert (settings.limits.minimum, settings.limits.maximum, settings.output) == (30.0, 60.0, "hdmi-a-1")

    def test_set_dwell_and_output_are_taken(self, cache_dir: Path):
        settings = load(
            a_slideshow(cache_dir, SLIDESHOW_MIN_SECONDS="12.5", SLIDESHOW_MAX_SECONDS="45", SLIDESHOW_OUTPUT="hdmi-a-2")
        )

        assert (settings.limits.minimum, settings.limits.maximum, settings.output) == (12.5, 45.0, "hdmi-a-2")

    def test_a_cookie_pasted_with_its_name_is_taken_without_it(self, cache_dir: Path):
        settings = load(a_slideshow(cache_dir, REDDIT_SESSION=f" reddit_session={COOKIE} "))

        assert settings.reddit_session == COOKIE

    def test_the_slideshows_files_are_beside_the_walls_under_names_no_wall_can_take(self, cache_dir: Path):
        settings = load(a_slideshow(cache_dir))

        for path in (settings.media_root, settings.listing_path, settings.runtime_dir):
            assert path.parent == cache_dir
            assert path.name.startswith(".")

    def test_the_cookie_is_in_no_repr_and_no_startup_line(self, cache_dir: Path):
        settings = load(a_slideshow(cache_dir))

        assert "the-secret-part" not in repr(settings)
        assert "the-secret-part" not in repr(settings.startup_lines())

    def test_the_page_shows_a_pinned_slideshow_key_but_never_the_cookie(self, cache_dir: Path):
        ui = load_ui(a_slideshow(cache_dir))

        assert {"PLAYER_MODE", "SLIDESHOW_URL", "REDDIT_SESSION"} <= ui.pinned
        assert ui.pinned_values["SLIDESHOW_URL"] == LISTING
        assert "REDDIT_SESSION" not in ui.pinned_values
        assert ui.pinned_session_expiry == 1_810_000_000.0

    def test_a_slideshow_with_a_location_follows_the_sun_on_its_screen(self, cache_dir: Path):
        location = {name: FRAME[name] for name in ("LATITUDE", "LONGITUDE", "LOCATION_NAME")}
        settings = load(a_slideshow(cache_dir, SCREEN_MIN_BRIGHTNESS="12", SCREEN_MAX_BRIGHTNESS="71", **location))

        brightness = settings.screen_brightness
        assert brightness.active
        assert (brightness.minimum, brightness.maximum) == (12, 71)
        assert settings.startup_lines()["screen_brightness"] == "12 to 71 with the sun at Bozeman, every 300 s"

    def test_a_slideshow_with_no_location_leaves_its_screen_alone_and_says_why(self, cache_dir: Path):
        settings = load(a_slideshow(cache_dir))

        assert not settings.screen_brightness.active
        assert "LATITUDE, LONGITUDE and LOCATION_NAME are not set" in settings.startup_lines()["screen_brightness"]

    def test_a_slideshow_refuses_half_a_location_by_name(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="LONGITUDE"):
            load(a_slideshow(cache_dir, LATITUDE="45.68", LOCATION_NAME="Bozeman"))

    def test_a_layout_1_settings_file_still_reads(self, cache_dir: Path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        store.path_in(cache_dir).write_text('{"version": 1, "values": {"PLAYER_NAME": "Hall"}}', encoding="utf-8")

        assert store.read(cache_dir).values == {"PLAYER_NAME": "Hall"}
        store.write(cache_dir, store.read(cache_dir))
        assert '"version": 2' in store.path_in(cache_dir).read_text(encoding="utf-8")


class TestAScreensBrightness:
    """An HDMI screen follows the sun (#34): on with a location, off and said without one."""

    def test_with_no_location_it_is_off_and_the_start_line_says_why(self, cache_dir: Path):
        settings = load(an_environment(cache_dir, frame=False))

        assert settings.screen_brightness.location is None
        assert "LATITUDE, LONGITUDE and LOCATION_NAME are not set" in settings.startup_lines()["screen_brightness"]

    def test_a_location_with_no_frame_turns_it_on_at_the_owners_levels(self, cache_dir: Path):
        location = {name: FRAME[name] for name in ("LATITUDE", "LONGITUDE", "LOCATION_NAME")}
        settings = load(an_environment(cache_dir, frame=False, **location))

        brightness = settings.screen_brightness
        assert settings.frame is None
        assert brightness.location is not None
        assert (brightness.location.latitude, brightness.location.longitude) == (45.68, -111.04)
        assert brightness.location.name == "Bozeman"
        # Owner, 2026-11-13: night 5, day 50.
        assert (brightness.minimum, brightness.maximum) == (5, 50)
        assert settings.startup_lines()["screen_brightness"] == "5 to 50 with the sun at Bozeman, every 300 s"

    def test_the_levels_and_the_interval_are_read_from_the_environment(self, cache_dir: Path):
        settings = load(
            an_environment(cache_dir, SCREEN_MIN_BRIGHTNESS="12", SCREEN_MAX_BRIGHTNESS="71", BRIGHTNESS_INTERVAL_SECONDS="91")
        )

        brightness = settings.screen_brightness
        assert (brightness.minimum, brightness.maximum, brightness.interval_seconds) == (12, 71, 91.0)

    @pytest.mark.parametrize("missing", ["LATITUDE", "LONGITUDE", "LOCATION_NAME"])
    def test_half_a_location_is_refused_by_name_even_with_no_frame(self, cache_dir: Path, missing: str):
        location = {name: FRAME[name] for name in ("LATITUDE", "LONGITUDE", "LOCATION_NAME") if name != missing}

        with pytest.raises(ConfigError, match=missing):
            load(an_environment(cache_dir, frame=False, **location))

    @pytest.mark.parametrize(
        ("night", "day"),
        [("51", "50"), ("-1", "50")],
    )
    def test_levels_that_cannot_be_a_curve_are_refused(self, cache_dir: Path, night: str, day: str):
        with pytest.raises(ConfigError, match="SCREEN_MIN_BRIGHTNESS"):
            load(an_environment(cache_dir, SCREEN_MIN_BRIGHTNESS=night, SCREEN_MAX_BRIGHTNESS=day))

    def test_the_same_level_for_night_and_day_is_allowed(self, cache_dir: Path):
        settings = load(an_environment(cache_dir, SCREEN_MIN_BRIGHTNESS="30", SCREEN_MAX_BRIGHTNESS="30"))

        assert (settings.screen_brightness.minimum, settings.screen_brightness.maximum) == (30, 30)

    def test_the_switch_turns_it_off_even_with_a_location(self, cache_dir: Path):
        """A client with a Frame must set a location for the Frame, so the screens need a switch of their own."""
        settings = load(an_environment(cache_dir, SCREEN_BRIGHTNESS="off"))

        assert settings.frame is not None
        assert settings.screen_brightness.location is not None
        assert not settings.screen_brightness.active
        assert settings.startup_lines()["screen_brightness"] == "off: SCREEN_BRIGHTNESS is off"

    def test_a_switch_that_is_neither_on_nor_off_is_refused(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="SCREEN_BRIGHTNESS"):
            load(an_environment(cache_dir, SCREEN_BRIGHTNESS="dim"))

    def test_levels_are_not_held_against_a_player_with_brightness_off(self, cache_dir: Path):
        """Turning the feature on must not stop a Player that started before it existed."""
        load(an_environment(cache_dir, frame=False, SCREEN_MIN_BRIGHTNESS="60", SCREEN_MAX_BRIGHTNESS="50"))
        load(an_environment(cache_dir, SCREEN_BRIGHTNESS="off", SCREEN_MIN_BRIGHTNESS="60", SCREEN_MAX_BRIGHTNESS="50"))

    def test_an_interval_the_frame_accepts_is_accepted_for_the_screens(self, cache_dir: Path):
        """One key, read one way: what a Frame accepts the screens accept, and what one refuses both refuse.

        Zero was once accepted here, for a Frame that started on it before the
        screens read the key; it is refused now for both, since it is an interval
        that never waits (`TestIntervalsAndTimeouts`)."""
        settings = load(an_environment(cache_dir, BRIGHTNESS_INTERVAL_SECONDS="0.5"))

        assert settings.frame is not None
        assert settings.frame.brightness_interval_seconds == 0.5
        assert settings.screen_brightness.interval_seconds == 0.5
        for frame in (True, False):
            with pytest.raises(ConfigError, match="BRIGHTNESS_INTERVAL_SECONDS"):
                load(an_environment(cache_dir, frame=frame, BRIGHTNESS_INTERVAL_SECONDS="0"))

    def test_the_frame_and_the_screens_read_one_location(self, cache_dir: Path):
        settings = load(an_environment(cache_dir, LOCATION_REGION="Montana"))

        assert settings.frame is not None
        assert settings.frame.location == settings.screen_brightness.location
        assert settings.frame.location.region == "Montana"

    def test_a_frame_with_no_location_at_all_is_refused_by_name(self, cache_dir: Path):
        environment = an_environment(cache_dir)
        for name in ("LATITUDE", "LONGITUDE", "LOCATION_NAME"):
            del environment[name]

        with pytest.raises(ConfigError, match="LATITUDE, LONGITUDE and LOCATION_NAME"):
            load(environment)


# -- the Frame's address, which the page manages too ----------------------------------

#: Addresses `TV_ADDRESS` refuses, each a mistake somebody could type: a URL, a port, a path, two sets, a
#: space, a bad label, a trailing dot, and IPv6 bare and bracketed. The settings page is held to refuse each
#: in the same words (`tests/test_settings_ui.py`).
REFUSED_TV_ADDRESSES = [
    "http://192.0.2.20",
    "192.0.2.20:8002",
    "tv.example.com/art",
    "192.0.2.20,192.0.2.21",
    "two words",
    "-tv.example.com",
    "tv.example.com.",
    "2001:db8::20",
    "[2001:db8::20]",
]


class TestTheFramesAddress:
    @pytest.mark.parametrize("address", REFUSED_TV_ADDRESSES)
    def test_an_address_the_library_could_not_reach_is_refused_by_name(self, cache_dir: Path, address: str):
        with pytest.raises(ConfigError, match="TV_ADDRESS") as raised:
            load(an_environment(cache_dir, TV_ADDRESS=address))

        assert str(raised.value) == tv_address_problem(address)

    def test_an_address_of_spaces_is_refused_by_name_not_driven(self, cache_dir: Path):
        with pytest.raises(ConfigError, match="only spaces"):
            load(an_environment(cache_dir, TV_ADDRESS="   "))

    @pytest.mark.parametrize("address", ["192.0.2.20", "tv.example.com", "TV.Example.com", "frame"])
    def test_an_ipv4_address_or_a_host_name_is_the_frames(self, cache_dir: Path, address: str):
        assert load(an_environment(cache_dir, TV_ADDRESS=f" {address} ")).frame.tv_address == address

    def test_the_page_supplies_the_address_when_the_environment_does_not(self, cache_dir: Path):
        store.write(cache_dir, store.Stored(values={"TV_ADDRESS": "192.0.2.20"}))
        environment = an_environment(cache_dir)
        del environment["TV_ADDRESS"]

        assert load(environment).frame.tv_address == "192.0.2.20"

    def test_the_environment_wins_over_the_pages_address(self, cache_dir: Path):
        store.write(cache_dir, store.Stored(values={"TV_ADDRESS": "192.0.2.20"}))

        assert load(an_environment(cache_dir, TV_ADDRESS="192.0.2.30")).frame.tv_address == "192.0.2.30"

    def test_an_empty_address_in_the_environment_does_not_pin_over_the_pages(self, cache_dir: Path):
        """`.env.example` ships `TV_ADDRESS=`, so a `.env` copied from it leaves the page in charge."""
        store.write(cache_dir, store.Stored(values={"TV_ADDRESS": "192.0.2.20"}))

        assert load(an_environment(cache_dir, TV_ADDRESS="")).frame.tv_address == "192.0.2.20"

    @pytest.mark.parametrize("address", REFUSED_TV_ADDRESSES[:3])
    def test_an_address_from_the_pages_file_is_judged_as_one_from_the_environment(self, cache_dir: Path, address: str):
        store.write(cache_dir, store.Stored(values={"TV_ADDRESS": address}))
        environment = an_environment(cache_dir)
        del environment["TV_ADDRESS"]

        with pytest.raises(ConfigError) as raised:
            load(environment)

        assert str(raised.value) == tv_address_problem(address)

    def test_the_page_finds_the_pairing_token_where_the_frame_keeps_it(self, cache_dir: Path, tmp_path: Path):
        for environment in (an_environment(cache_dir), an_environment(cache_dir, TV_TOKEN_FILE=str(tmp_path / "kept"))):
            assert load_ui(environment).tv_token_file == load(environment).frame.tv_token_file

    def test_a_token_path_the_frame_would_refuse_gives_the_page_none_and_stops_nothing(self, cache_dir: Path):
        ui = load_ui(an_environment(cache_dir, frame=False, TV_TOKEN_FILE="relative/token"))

        assert ui.tv_token_file is None


class TestTheSettingsFilesLayout:
    """Version 3 holds the Frame's address; a file without it stays version 2, which the release before reads."""

    def test_a_file_without_the_frames_address_is_written_as_layout_2(self, cache_dir: Path):
        store.write(cache_dir, store.Stored(values={"SERVER_URL": "http://arrt.example:8770", "PLAYER_MODE": "arrt"}))

        assert '"version": 2' in store.path_in(cache_dir).read_text(encoding="utf-8")

    def test_a_file_with_the_frames_address_is_written_as_layout_3_and_read_back(self, cache_dir: Path):
        store.write(cache_dir, store.Stored(values={"TV_ADDRESS": "192.0.2.20"}))

        assert '"version": 3' in store.path_in(cache_dir).read_text(encoding="utf-8")
        assert store.read(cache_dir).values == {"TV_ADDRESS": "192.0.2.20"}

    def test_a_layout_2_file_the_previous_release_wrote_still_reads(self, cache_dir: Path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        store.path_in(cache_dir).write_text(
            '{"version": 2, "values": {"PLAYER_MODE": "slideshow", "SLIDESHOW_URL": "x"}, "auth": {"password": "p"}}',
            encoding="utf-8",
        )

        assert store.read(cache_dir) == store.Stored(values={"PLAYER_MODE": "slideshow", "SLIDESHOW_URL": "x"}, password="p")

    def test_the_layout_2_file_holds_only_keys_layout_2_knows(self, cache_dir: Path):
        """What lets the release before this one read it: no key of version 3's in a version 2 file."""
        every_other_key = {key: "x" for key in store.MANAGED_KEYS if key not in store.FRAME_KEYS}

        assert store.layout_for(every_other_key) == 2
        assert all(store.layout_for({**every_other_key, key: "x"}) == 3 for key in store.FRAME_KEYS)


class TestTheOneValidator:
    """`resolve` is `load` with the page's file swapped for the values the page is about to write."""

    def test_it_gives_what_load_gives_from_the_written_file(self, cache_dir: Path):
        values = {"TV_ADDRESS": "192.0.2.20", "PLAYER_NAME": "Hall"}
        environment = an_environment(cache_dir)
        del environment["TV_ADDRESS"]

        judged = resolve(environment, values)
        store.write(cache_dir, store.Stored(values=values))

        assert judged == load(environment)

    def test_it_never_reads_the_file_on_disk(self, cache_dir: Path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        store.path_in(cache_dir).write_text('{"version": 99}', encoding="utf-8")

        assert resolve(an_environment(cache_dir), {}).frame is not None

    def test_it_refuses_in_loads_words(self, cache_dir: Path):
        environment = an_environment(cache_dir)
        for name in ("LATITUDE", "LONGITUDE", "LOCATION_NAME"):
            del environment[name]
        store.write(cache_dir, store.Stored(values={"PLAYER_NAME": "Hall"}))

        with pytest.raises(ConfigError) as from_load:
            load(environment)
        with pytest.raises(ConfigError) as from_resolve:
            resolve(environment, {"PLAYER_NAME": "Hall"})

        assert str(from_resolve.value) == str(from_load.value)


#: Two values `load` accepts for each key the page manages, so changing one is a change `load` could see.
TWO_VALUES = {
    "SERVER_URL": ("http://192.0.2.1:8770", "http://192.0.2.2:8770"),
    "CLIENT_TOKEN": ("token-one", "token-two"),
    "PLAYER_NAME": ("Hall", "Office"),
    "PLAYER_MODE": None,  # what switches between the two; read in both
    "TV_ADDRESS": ("192.0.2.20", "192.0.2.21"),
    "SLIDESHOW_URL": (LISTING, "https://www.reddit.com/r/EarthPorn/top/?t=year"),
    "REDDIT_SESSION": (COOKIE, COOKIE.replace("the-secret-part", "another-secret")),
    "SLIDESHOW_MIN_SECONDS": ("20", "25"),
    "SLIDESHOW_MAX_SECONDS": ("60", "90"),
    "SLIDESHOW_OUTPUT": ("hdmi-a-1", "hdmi-a-2"),
}


def _unread(mode: str) -> list[str]:
    return [key for key in store.MANAGED_KEYS if key not in PAGE_KEYS_READ[mode]]


class TestTheKeysEachModeReads:
    """What lets the page say a change waits for the other mode: outside a mode's set, it changes nothing loaded."""

    def test_every_key_the_page_manages_has_two_values_here(self):
        assert set(TWO_VALUES) == set(store.MANAGED_KEYS)

    def test_every_key_the_page_manages_is_read_by_some_mode(self):
        assert set().union(*PAGE_KEYS_READ.values()) == set(store.MANAGED_KEYS)

    @pytest.mark.parametrize(("mode", "key"), [(mode, key) for mode in sorted(PAGE_KEYS_READ) for key in _unread(mode)])
    def test_a_key_outside_a_modes_set_changes_nothing_that_mode_loads(self, cache_dir: Path, mode: str, key: str):
        base = a_slideshow(cache_dir) if mode == "slideshow" else an_environment(cache_dir)
        first, second = TWO_VALUES[key]

        assert load({**base, key: first}) == load({**base, key: second})

    def test_the_parametrisation_is_not_empty(self):
        assert _unread("arrt")
        assert _unread("slideshow")


# -- the names kept under CACHE_DIR, and the wall ids they forbid (review O-3) ------------

#: The attribute or variable names that hold `CACHE_DIR` where a path is built on it.
_CACHE_ROOTS = {"cache_dir", "_root"}


def _string_constants(trees: dict[str, ast.Module]) -> dict[str, dict[str, str]]:
    """Each module's names for text: its own module-level `NAME = "…"`, then what it imports from the package."""
    own = {
        dotted: {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        for dotted, tree in trees.items()
    }
    names = {dotted: dict(table) for dotted, table in own.items()}
    for dotted, tree in trees.items():
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in own:
                for alias in node.names:
                    if alias.name in own[node.module]:
                        names[dotted][alias.asname or alias.name] = own[node.module][alias.name]
    return names


def _names_kept_under_the_cache() -> dict[str, str]:
    """Every name the Player's code puts directly under `CACHE_DIR`, with where it is built.

    Read from the code that makes the paths (`<cache_dir> / NAME` and `<cache_dir> / f"{PREFIX}…"`), each
    constant resolved from its module's source, never copied here and never imported (some modules load
    drivers a test machine may not have).
    """
    package = Path(config.__file__).parent
    trees = {
        ".".join(("arrt_player", *source.relative_to(package).with_suffix("").parts)).removesuffix(".__init__"): ast.parse(
            source.read_text(encoding="utf-8")
        )
        for source in sorted(package.rglob("*.py"))
    }
    constants = _string_constants(trees)
    found: dict[str, str] = {}
    for dotted, tree in trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            root = node.left.attr if isinstance(node.left, ast.Attribute) else getattr(node.left, "id", None)
            if root not in _CACHE_ROOTS:
                continue
            right = node.right
            if isinstance(right, ast.JoinedStr) and right.values:
                right = right.values[0].value if isinstance(right.values[0], ast.FormattedValue) else right.values[0]
            if isinstance(right, ast.Name) and right.id in constants[dotted]:
                found[constants[dotted][right.id]] = f"{dotted}.{right.id}"
            elif isinstance(right, ast.Constant) and isinstance(right.value, str):
                found[right.value] = f"{dotted}: a literal"
    return found


class TestTheNamesUnderTheCache:
    def test_the_scan_finds_the_names_it_was_written_for(self):
        """Not vacuous: plain and dotted, from four modules, the label cache and the removal prefix among them."""
        found = _names_kept_under_the_cache()

        assert {
            config.TV_TOKEN_FILENAME,
            config.CLIENT_DOCUMENT_FILENAME,
            store.SETTINGS_FILENAME,
            pull.LABEL_CACHE_DIRNAME,
            pull.CLIENT_ETAG_FILENAME,
        } <= set(found)
        assert any(name.startswith(".removing") for name in found)

    def test_every_name_kept_under_the_cache_is_reserved_or_no_possible_wall_id(self):
        """A plain name kept there and not in `CACHE_DIR_NAMES` is a wall id that would collide with it."""
        colliding = {
            name: where
            for name, where in _names_kept_under_the_cache().items()
            if name not in CACHE_DIR_NAMES and PLAIN_COMPONENT.fullmatch(f"{name}x")
        }

        assert colliding == {}


# -- one host helper and one setting name for the page and the configuration (review O-10) --


class TestThePageAndTheConfigurationReadNamesAlike:
    @pytest.mark.parametrize("listed", ["Wall.LAN.", "wall.example.com", "WALL.EXAMPLE.COM.", "192.0.2.10", "::1"])
    def test_a_name_listed_is_the_name_a_request_carrying_it_is_reduced_to(self, cache_dir: Path, listed: str):
        """With a port and without, as a browser sends either: both go through the one helper."""
        ui = load_ui(an_environment(cache_dir, frame=False, UI_ALLOWED_HOSTS=listed))
        bracketed = f"[{listed}]" if ":" in listed else listed

        assert {settings_server.hostname(host) for host in (bracketed, f"{bracketed}:8771")} <= ui.allowed_hosts

    def test_the_setting_is_named_in_one_place(self):
        literal = '"UI_ALLOWED_HOSTS"'
        sources = {module.__name__: Path(module.__file__).read_text(encoding="utf-8") for module in (config, settings_server)}

        assert {name: text.count(literal) for name, text in sources.items()} == {config.__name__: 1, settings_server.__name__: 0}
        assert settings_server.ALLOWED_HOSTS_SETTING is config.ALLOWED_HOSTS_SETTING
