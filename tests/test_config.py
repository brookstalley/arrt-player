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

from pathlib import Path

import pytest

from arrt_player.config import (
    DEFAULT_UI_BIND,
    DEFAULT_UI_PORT,
    RETIRED_SETTINGS,
    ConfigError,
    SetupRequired,
    SlideshowSettings,
    WallIdUnusable,
    load,
    load_ui,
)
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
        ui = load_ui(an_environment(cache_dir, frame=False, PLAYER_NAME="", TV_ADDRESS="10.0.0.1"))

        assert ui.pinned == {"SERVER_URL", "CLIENT_TOKEN"}

    def test_an_untrustworthy_file_refuses_to_start_and_names_it(self, cache_dir: Path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        store.path_in(cache_dir).write_text('{"version": 99}', encoding="utf-8")

        for resolve in (load, load_ui):
            with pytest.raises(ConfigError, match=r"\.settings\.json"):
                resolve(an_environment(cache_dir, frame=False))


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
        every other test in this plane builds its settings directly — so a misspelt
        key or a wrong default here would leave `epd_device` empty on a Pi that has
        a panel, `label_surface` would return None, and the heartbeat would report
        a device with no panel. That is the exact distinction this plane was built
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

    @pytest.mark.parametrize("usable", ["3f2a9c1e-8b7d-4e6f-a1b2-c3d4e5f60718", "living-room", "w_1", "wall.v2"])
    def test_ids_the_server_mints_are_accepted(self, cache_dir: Path, usable: str):
        assert load(an_environment(cache_dir)).wall(usable).wall_dir == cache_dir / usable


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
        """One key, read one way: a Frame on `BRIGHTNESS_INTERVAL_SECONDS=0` started before this existed."""
        settings = load(an_environment(cache_dir, BRIGHTNESS_INTERVAL_SECONDS="0"))

        assert settings.frame is not None
        assert settings.frame.brightness_interval_seconds == 0
        assert settings.screen_brightness.interval_seconds == 0

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
