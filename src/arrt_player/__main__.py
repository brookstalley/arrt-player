"""Start the Player: `uv run python -m arrt_player`.

The composition root, and the only module that knows a real television exists —
everything above the seam is written against `TvClient`, so this is where the
`samsungtvws` fork is named and where it stays. It is also the only module that
imports the pull, which holds the HTTP client that talks to the Arrt server.

**One process per client, one worker per wall** (`clients.md` § The Player). The
supervisor (`client.Supervisor`) asks the server which walls this client drives
and runs a worker for each on its output: one wall loop (`wall.Wall`) and the
wall's pull, with the Frame's driver for the Frame (`run_frame_wall`) and the
screen's for a screen this host draws on (`run_screen_wall`).

**And one label renderer per mapped label output** (`labels-and-surfaces.md`):
`run_label`, on the panel its label names among this client's panels, which this
module opens once per process (`label_panels`) because they are the client's,
not any wall's. **The panels are held by output name, zero or more of them, as
the displays are** (the owner's ruling of 2026-11-19): the configuration
describes one today (`config.PANEL_OUTPUT`), and nothing here assumes it. The
Frame's identity is read here too, at the client, so a Frame with no wall on it
still says who it is (`FrameIdentity`).

**A dead pull ends its own wall's worker, and the supervisor starts it again.**
A pull that stops leaves its wall rotating its cache, taking no updates and
sending no heartbeat, which is invisible from the wall. So the worker stops with
the pull's error, which the supervisor logs at ERROR and answers with a restart.
That used to be the whole process stopping so systemd restarted it; with several
walls in one process, that would blank every other wall over one wall's fault.

**Shutdown is deliberate rather than abrupt.** systemd sends SIGTERM and then
waits a bounded time before SIGKILL; a process that ignored the first would be
killed with the Frame's websocket open, and the set holds a half-closed art
channel until it times out on its own. The stop event reaches every worker, and
the supervisor waits for each to close what it holds.

**The settings page runs beside all of it, and alone when there is no server
yet.** A Player with no `SERVER_URL` or `CLIENT_TOKEN` starts in setup mode: it
serves only the page (`settings_ui.server`) and says where to go. A Player that
has both runs its walls and the page together, and a page that cannot bind its
port costs the page and nothing else, because a wall never waits on its settings.
Until a password is set the page needs the setup code minted here and printed with
its address; it is new on every start and dies once a password is set.

**A Player with `FILE_OUTPUT_DIR` set drives no hardware at all**: its outputs
are a screen and a label drawn to that directory (`player_outputs`,
`label_panels`, `run_wall`), placed, composed for and drawn by the same
supervisor, schedule and renderer as any other, so a real Player process can be
tested against a real server where there is no screen, television or panel. It
says so once at start, at WARNING, because on a wall it would be a fault.

**systemd is told when the Player is up, and each time it makes progress**
(`notify`), because the unit is `Type=notify` with a watchdog: `READY=1` once
each mode is running, and `WATCHDOG=1` only for progress, never for the loop
merely being alive. In Arrt mode, and with file outputs, progress is the
supervisor finishing a cycle (`PettingSupervisor`); in slideshow mode, the
programme reaching its next step (`petting`); in setup mode, the page still
served by a loop that turns. A start refused by configuration sends nothing
and exits 2. What a stuck Player then meets is in the unit's comment.

**In slideshow mode there is no server, supervisor or pull** (`run_slideshow`).
One programme plays a Reddit listing on one HDMI screen through mpv and
captions it on the panel, beside the same page. Its parts are composed in
`slideshow_parts`, which is where the host resolvers are wired to both ends: the
source, so a post only a resolver can play is listed, and the cache, so the
resolver is asked first when the post is downloaded. This module and the
programme are the only places outside `sources/` that import its network client.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from functools import partial
from typing import Any

from arrt_player import logs, notify
from arrt_player.client import (
    FRAME_KIND,
    FrameIdentity,
    HeardReport,
    LabelAssignment,
    OutputReport,
    Supervisor,
    WorkerStop,
    client_outputs,
    is_retired,
    settings_url,
)
from arrt_player.config import (
    ARRT_MODE,
    FILE_SCREEN_OUTPUT,
    PANEL_OUTPUT,
    SLIDESHOW_MODE,
    ClientSettings,
    ConfigError,
    FileOutputSettings,
    FrameSettings,
    PanelSettings,
    ScreenBrightnessSettings,
    Settings,
    SetupRequired,
    SlideshowSettings,
    UiSettings,
    WallSettings,
    load,
    load_ui,
)
from arrt_player.ddc import Ddc, DdcBus, I2cTransport, bus_for
from arrt_player.displays import file_output
from arrt_player.displays.frame import frame_wall
from arrt_player.displays.screen import ScreenOutput, go_dark, screen_wall
from arrt_player.displays.screen_brightness import ScreenBrightness
from arrt_player.episodes import ReportOnce
from arrt_player.kms import KmsOutput
from arrt_player.label_renderer import LabelPanel, LabelRenderer
from arrt_player.manifest import Watcher
from arrt_player.mpv import MpvOutput, renderer_problems
from arrt_player.panel import Geometry, LabelSurface, SurfaceUnavailable
from arrt_player.panel.legibility import TypeScale, ViewingConditionsUnknown, margin_for, type_scale_for
from arrt_player.programmes.slideshow import Screen, Slideshow, Wait, wait_for_stop
from arrt_player.programmes.slideshow_status import SlideshowStatus
from arrt_player.pull import LABEL_CACHE_DIRNAME, ClientPull, LabelPull, Pull
from arrt_player.settings_ui import auth, server, store
from arrt_player.sources.cache import MediaCache
from arrt_player.sources.fetch import Fetcher
from arrt_player.sources.reddit import RedditSource
from arrt_player.sources.resolvers import Resolvers
from arrt_player.sources.resolvers.imgur import ImgurResolver
from arrt_player.sources.resolvers.redgifs import RedgifsResolver
from arrt_player.state import DisplayState, StateSchemaTooNew
from arrt_player.tv.samsung import SamsungTv
from arrt_player.wall import Clock

log = logging.getLogger(__name__)


def label_surface(settings: PanelSettings) -> LabelSurface | None:
    """This device's label surface, None when it has none, raising when it has a broken one.

    **The three outcomes are three different things and are reported
    differently.** No `EPD_DEVICE` means this device draws no label, which
    Arrt's `architecture.md` § Direction makes a supported deployment rather than a
    fault — `None`, and nothing said. A configured panel that will not open is a
    device that is *meant* to have a label and does not, which is worth saying
    out loud — hence a raise rather than a second `None`, because two roads to
    the same return value is how a broken panel came to look identical to a
    deployment that never had one.

    **The rasterizer is imported here rather than at the top of this module.** It
    pulls in Pango through PyGObject, which is installed only where a label is
    actually drawn, and importing it unconditionally would make a text stack a
    requirement for every device including the ones with no panel at all.

    **The type scale is derived here** because this is where `.env` is read and
    where a device is decided to have a panel; everything below the seam takes its
    physical facts as arguments, which is what lets the next device be a
    configuration rather than a rewrite.
    """
    if not settings.epd_device:
        return None

    # **Before the driver import, deliberately.** Both are reasons this device
    # gets no label, but only one of them is a value somebody typed: a deployment
    # that has not stated its viewing distance should be told that, not told its
    # text stack is missing — which on a dev machine it also is.
    try:
        scale = type_scale_for(
            width_px=settings.epd_panel_width_px,
            height_px=settings.epd_panel_height_px,
            diagonal_inches=settings.epd_panel_diagonal_inches,
            viewing_distance_inches=settings.epd_viewing_distance_inches,
        )
    except ViewingConditionsUnknown as exc:
        # Converted rather than propagated, the same move the text stack's
        # ImportError gets below: this function's promise to its caller is that a
        # device without a usable label surface raises one type, and the caller
        # answers all of them by saying so once and rotating the wall anyway.
        raise SurfaceUnavailable(str(exc)) from exc

    geometry = label_geometry(settings, scale)
    # **The derived numbers, said out loud once, and before the imports below.**
    # The startup line carries the two inputs; without these an operator asking
    # "why is this label dropping three lines" has a formula in an artifact and no
    # way to see what the wall actually computed. Whether the border was derived
    # or overridden is named for the same reason — nothing else distinguishes the
    # two. It sits above the driver import so that a device with a missing text
    # stack still reports what it would have set, which is the case where somebody
    # is already reading the journal.
    log.info(
        "label type derives to %d px over a %d px floor, with a %d px border (%s)",
        scale.primary_px,
        scale.floor_px,
        geometry.margin_px,
        "EPD_MARGIN_PX" if settings.epd_margin_px is not None else "derived",
        extra={"event": "panel.type_scale"},
    )

    try:
        from arrt_player.panel.epaper import EpaperSurface, open_panel
        from arrt_player.panel.pango import PangoRasterizer
    except ImportError as exc:
        # The text stack being absent lands in the same place as the panel being
        # absent, and it is a provisioning mistake somebody can fix while the wall
        # keeps working — so it is converted rather than allowed to crash a daemon
        # whose television is perfectly healthy.
        raise SurfaceUnavailable(f"the label's text stack is not installed ({exc}); try `uv sync --group raster`") from exc

    return EpaperSurface(
        epd=open_panel(settings.epd_device),
        rasterizer=PangoRasterizer(),
        geometry=geometry,
        type_scale=scale,
        rotate_degrees=settings.epd_rotate_degrees,
        output=PANEL_OUTPUT,
    )


def label_geometry(settings: PanelSettings, scale: TypeScale) -> Geometry:
    """This panel's usable area, with a border derived from the type on it.

    **Separate from `label_surface` because it is the only part of that function
    reachable without hardware.** Everything else there needs a driver and a text
    stack; this is a decision, and a decision covered by no test is one a
    refactor can invert silently — which for this one means a label that quietly
    goes back to a border chosen against type sizes nobody had measured.

    The margin derives unless the deployment states one. It cannot be picked
    independently: a border trades directly against how many lines survive the
    drop rule, so choosing it apart from the floor that decides how many lines
    there are leaves two numbers in tension that nobody compared. The override is
    for the surface whose border is a physical fact rather than a typographic
    choice — a device drawing its label into the mat area around an artwork does
    not get to choose where the picture ends.
    """
    return Geometry(
        width_px=settings.epd_panel_width_px,
        height_px=settings.epd_panel_height_px,
        margin_px=settings.epd_margin_px if settings.epd_margin_px is not None else margin_for(scale),
    )


def label_panel(settings: PanelSettings) -> LabelPanel | None:
    """This client's label output, or None for a client with no panel configured.

    **A panel that will not open is reported, not fatal**, and not dropped
    either: it is a `LabelPanel` with no surface, which the client heartbeat
    reports as a label output that is not connected. Without it a
    configured-but-broken panel would read on the server exactly like a client
    that never had one.
    """
    if not settings.epd_device:
        return None
    size = (settings.epd_panel_width_px, settings.epd_panel_height_px)
    try:
        surface = label_surface(settings)
    except SurfaceUnavailable as exc:
        log.warning(
            "this device has a panel configured (%s) and no label will be drawn (%s); the walls are unaffected",
            settings.epd_device,
            exc,
            extra={"event": "panel.unavailable", "output": PANEL_OUTPUT},
        )
        return LabelPanel(name=PANEL_OUTPUT, surface=None, error=str(exc), size=size)
    return LabelPanel(name=PANEL_OUTPUT, surface=surface, error=None, size=size)


def label_panels(settings: ClientSettings) -> dict[str, LabelPanel]:
    """Every label panel this client holds, by output name: the e-paper one, the file label, or none.

    **A mapping, not a panel**, so that what holds, reports, draws on and closes
    the panels never assumes there is one: the configuration's limit
    (`config.PANEL_OUTPUT`) is the configuration's, and a second panel would be
    a second entry here and nothing more. The file label (`FILE_OUTPUT_LABEL`)
    is one such entry; the configuration never gives a Player both it and a panel.
    """
    panels: dict[str, LabelPanel] = {}
    panel = label_panel(settings.panel)
    if panel is not None:
        panels[panel.name] = panel
    if settings.files is not None and settings.files.label:
        drawn = file_output.file_label_panel(settings.files)
        panels[drawn.name] = drawn
    return panels


def player_outputs(settings: ClientSettings, *, frame_identity: str | None = None) -> list[OutputReport]:
    """Every output this client reports and drives: its file screen alone when it draws to files, else its hardware's.

    **Instead of the hardware, never beside it.** With `FILE_OUTPUT_DIR` set no
    HDMI connector is listed (and the configuration refuses a Frame beside it),
    so the server can place a wall only where this Player will draw it.
    """
    if settings.files is not None:
        return [file_output.output_report(settings.files)]
    return client_outputs(settings, frame_identity=frame_identity)


def frame_tv(settings: FrameSettings) -> SamsungTv:
    """The Frame's television client, as every reader of it is built."""
    return SamsungTv(
        host=settings.tv_address,
        port=settings.tv_port,
        token_file=settings.tv_token_file,
        client_name=settings.tv_client_name,
        connect_timeout_seconds=settings.tv_connect_timeout_seconds,
        upload_timeout_seconds=settings.upload_timeout_seconds,
        select_confirm_seconds=settings.select_confirm_seconds,
    )


async def run_label(
    settings: ClientSettings,
    panels: Mapping[str, LabelPanel],
    assignment: LabelAssignment,
    stop: WorkerStop,
    *,
    clock: Clock | None = None,
) -> None:
    """The supervisor's label worker: one renderer on the panel its label names, until stopped.

    **Only on that panel.** A label naming an output this client holds no panel
    for is said once by name and not drawn, never drawn on another panel: the
    supervisor already refuses one its label outputs do not list, so reaching
    here is the two disagreeing, which a restart would not mend. It waits to be
    stopped rather than ending, which the supervisor would answer with a restart
    and a line every backoff step.
    """
    panel = panels.get(assignment.output)
    if panel is None:
        if not stop.is_set():
            log.error(
                "label %s names label output %s, and this client holds no panel by that name (it holds %s); it is not drawn",
                assignment.label_id,
                assignment.output,
                ", ".join(sorted(panels)) or "none",
                extra={"event": "label.no_panel", "label_id": assignment.label_id, "output": assignment.output},
            )
            await stop.wait()
        return
    # One clock for all three: the pull stamps the server's last answer by `now`,
    # the renderer reads that stamp back against its own `now`, and the offline
    # hold runs on the same clock's monotonic as the pull's stamp throttle.
    clock = clock if clock is not None else Clock.system()
    renderer = LabelRenderer(
        assignment=assignment,
        link=LabelPull(settings, assignment.label_id, now=clock.now, monotonic=clock.monotonic),
        panel=panel,
        now=clock.now,
        monotonic=clock.monotonic,
        poll_seconds=settings.label_poll_seconds,
    )
    with file_output.captioning(assignment):
        await renderer.run(stop)


class LabelCacheSweep:
    """Removes the cached documents of labels the current client document does not map (`Supervisor`'s `forget_labels`).

    **Every file under `LABEL_CACHE_DIRNAME` that no mapped label would read is
    removed**: a retired label's, one moved to another client's panel, one the
    server deleted, and an atomic write's leftover. A label the document maps
    keeps its file whichever of this client's outputs it is on, and whether or
    not this client can draw it. The supervisor calls this only once a document
    has been read, so an unreadable or absent one removes nothing.

    **Never raises**: a cache that cannot be swept costs disk, and is said once
    per episode.
    """

    def __init__(self, settings: ClientSettings) -> None:
        self._settings = settings
        self._directory = settings.cache_dir / LABEL_CACHE_DIRNAME
        self._unsweepable = ReportOnce()

    def __call__(self, mapped: Collection[str]) -> None:
        kept = {path for label_id in mapped if (path := LabelPull(self._settings, label_id).cache_path) is not None}
        try:
            found = [entry for entry in self._directory.iterdir() if entry not in kept and entry.is_file()]
            for entry in found:
                entry.unlink(missing_ok=True)
                log.info(
                    "removed %s: no label this client is given reads it",
                    entry.name,
                    extra={"event": "label.cache_forgotten", "path": str(entry)},
                )
        except FileNotFoundError:
            # Nothing has been cached yet.
            return
        except OSError as exc:
            if self._unsweepable.begin():
                log.warning(
                    "the cached documents of labels this client no longer draws cannot be removed from %s (%s)",
                    self._directory,
                    exc,
                    extra={"event": "label.cache_unsweepable"},
                )
            return
        if self._unsweepable.end():
            log.info("the label cache can be swept again", extra={"event": "label.cache_sweepable"})


async def run_frame_wall(settings: Settings, stop: asyncio.Event, *, clock: Clock | None = None) -> None:
    """One wall on the Frame: the wall's loop on the Frame and the wall's pull, until stopped.

    The Frame draws no label: the panel is a label output of the client,
    mapped to a wall by the server and drawn by `run_label`.

    **Called already stopped, it returns at once**: the supervisor's last call
    for a wall taken away between restarts, and the Frame has nothing to leave.
    """
    if stop.is_set():
        return
    settings.wall_dir.mkdir(parents=True, exist_ok=True)
    watcher = Watcher(settings.manifest_path)
    tv = frame_tv(settings)

    # One clock for both, because the Frame measures an upload's retry wait
    # against a timestamp the store wrote. Two sources here would be two answers
    # to the same question, and the store's is the one that has to survive a
    # restart.
    clock = clock if clock is not None else Clock.system()
    # **A store from a newer Player parks this wall, and only this wall.** Opening
    # it would guess at a shape this code does not know, and restarting cannot
    # change the answer — the fix is a rollout — so the worker says so once and
    # waits to be stopped, rather than crash-looping a traceback per backoff step
    # or ending the process and blanking every other wall this client drives.
    try:
        state = DisplayState(settings.state_path, now=clock.now)
    except StateSchemaTooNew as exc:
        log.error(  # noqa: TRY400 -- the fix is a rollout, not a frame
            "wall %s is not shown: %s",
            settings.wall_id,
            exc,
            extra={"event": "daemon.state_too_new", "wall_id": settings.wall_id},
        )
        await stop.wait()
        return
    with state:
        frame = frame_wall(
            settings=settings,
            tv=tv,
            state=state,
            watcher=watcher,
            clock=clock,
        )
        await _beside_its_pull(settings, stop, frame.run)


async def run_screen_wall(
    wall: WallSettings,
    output: str,
    stop: asyncio.Event,
    *,
    clock: Clock | None = None,
    brightness: ScreenBrightnessSettings | None = None,
    output_for: Callable[[WallSettings, str], ScreenOutput] | None = None,
) -> None:
    """One wall on a screen this host draws on: the wall's loop on the screen and the wall's pull, until stopped.

    **Retired, it leaves the screen black** once the loop and the pull have
    ended, so no draw of its own can land after the black, and the next wall on
    the connector starts only once this worker has finished. Called already
    stopped, which is the supervisor's last call for a wall taken away between
    restarts, it does only that.

    `output_for` builds what the wall draws on: an HDMI connector's
    (`screen_output`) unless the caller names another.
    """
    build = output_for if output_for is not None else screen_output
    if stop.is_set():
        if is_retired(stop):
            await go_dark(build(wall, output), wall_id=wall.wall_id, output_name=output)
        return
    wall.wall_dir.mkdir(parents=True, exist_ok=True)
    watcher = Watcher(wall.manifest_path)
    clock = clock if clock is not None else Clock.system()
    drawn_on = build(wall, output)
    screen = screen_wall(
        wall=wall,
        output=drawn_on,
        watcher=watcher,
        clock=clock,
        brightness=screen_brightness(wall, output, brightness, clock),
    )
    try:
        await _beside_its_pull(wall, stop, screen.run)
    finally:
        if is_retired(stop):
            await go_dark(drawn_on, wall_id=wall.wall_id, output_name=output)


def screen_ddc(output: str) -> DdcBus:
    """The DDC/CI bus of the screen on an HDMI connector, found from the connector."""
    return Ddc(I2cTransport(bus_for(output)))


def screen_brightness(
    wall: WallSettings,
    output: str,
    settings: ScreenBrightnessSettings | None,
    clock: Clock,
) -> ScreenBrightness | None:
    """The screen's brightness on the sun over its connector's DDC/CI bus, or None with no location to follow."""
    return _brightness_on(output, settings, clock, wall_id=wall.wall_id)


def slideshow_brightness(settings: SlideshowSettings, clock: Clock) -> ScreenBrightness | None:
    """The slideshow screen's brightness, as a wall's screen has it. Its journal lines carry `wall_id` "slideshow"."""
    return _brightness_on(settings.output, settings.screen_brightness, clock, wall_id=SLIDESHOW_MODE)


def _brightness_on(
    output: str,
    settings: ScreenBrightnessSettings | None,
    clock: Clock,
    *,
    wall_id: str,
) -> ScreenBrightness | None:
    if settings is None or not settings.active or settings.location is None:
        return None
    return ScreenBrightness(
        settings=settings,
        location=settings.location,
        output=output,
        wall_id=wall_id,
        bus=lambda: screen_ddc(output),
        clock=clock,
    )


def screen_output(
    wall: WallSettings,  # noqa: ARG001 -- every output builder takes the wall; a screen needs only its connector
    output: str,
) -> ScreenOutput:
    """What a wall on an HDMI connector draws on: the connector, through kernel mode setting."""
    return KmsOutput(output)


def file_screen_output(settings: FileOutputSettings, wall: WallSettings, output: str) -> ScreenOutput:
    """What a wall on the file screen draws on: a directory, named for the output, under `FILE_OUTPUT_DIR`."""
    return file_output.FileScreenOutput(settings, wall_id=wall.wall_id, name=output)


async def run_wall(settings: ClientSettings, wall: WallSettings, output: OutputReport, stop: asyncio.Event) -> None:
    """The supervisor's worker: the wall on the Frame for the Frame, on a screen this host draws for anything else.

    The file screen is a screen like a connector's, drawn to a directory, and
    with no brightness to follow: it has no DDC/CI bus.
    """
    if output.kind == FRAME_KIND:
        await run_frame_wall(settings.frame_wall(wall.wall_id), stop)
    elif settings.files is not None and output.name == FILE_SCREEN_OUTPUT:
        await run_screen_wall(wall, output.name, stop, output_for=partial(file_screen_output, settings.files))
    else:
        await run_screen_wall(wall, output.name, stop, brightness=settings.screen_brightness)


async def _beside_its_pull(wall: WallSettings, stop: asyncio.Event, loop: Callable[[asyncio.Event], Awaitable[None]]) -> None:
    """Run a wall's loop with its pull beside it, stopped together.

    **Their own stop event, not the supervisor's.** A dead pull must end this
    worker, and setting the supervisor's event to do that would read to it as
    "this wall was taken away" — the one case it does not restart.
    """
    ending = asyncio.Event()
    relay = asyncio.create_task(_relay(stop, ending), name=f"stop:{wall.wall_id}")
    pulling = asyncio.create_task(Pull(wall).run(ending), name=f"pull:{wall.wall_id}")
    pulling.add_done_callback(lambda task: _pull_ended(task, ending, wall.wall_id))
    try:
        await loop(ending)
    finally:
        ending.set()
        relay.cancel()
        # Re-raises a pull that crashed, so the worker ends failed and the
        # supervisor starts the wall and its pull again together.
        await pulling


async def _relay(stop: asyncio.Event, ending: asyncio.Event) -> None:
    await stop.wait()
    ending.set()


def _pull_ended(task: asyncio.Task[None], ending: asyncio.Event, wall_id: str) -> None:
    """Say at once that the pull died, and end the wall's worker so it starts again.

    A dead pull leaves the wall rotating its cache and taking no updates and
    sending no heartbeat, which is invisible from the wall and would stay
    invisible until somebody stopped the process.
    """
    if task.cancelled() or task.exception() is None:
        return
    log.error(
        "the pull for wall %s stopped on an error; stopping the wall's worker so it starts again",
        wall_id,
        exc_info=task.exception(),
        extra={"event": "pull.crashed", "wall_id": wall_id},
    )
    ending.set()


class PettingSupervisor(Supervisor):
    """The supervisor, petting systemd's watchdog each time it finishes a cycle.

    **A finished cycle is Arrt mode's progress**: the outputs reported, the
    client document asked for and matched, the walls' directories swept. Each
    step is bounded (the Frame's identity by its budget, each request by the
    pull's session timeout), so a cycle that does not finish is a supervisor or
    a loop that is stuck. A wall's own worker is not watched here: one stuck
    wall is that wall's fault, and a restart would blank every other wall.
    """

    def __init__(
        self,
        *,
        watchdog: notify.Watchdog,
        **kwargs: Any,  # noqa: ANN401 -- passed through whole to Supervisor's own keywords
    ) -> None:
        super().__init__(**kwargs)
        self._watchdog = watchdog

    async def cycle(self) -> None:
        await super().cycle()
        self._watchdog.progress()


def petting(watchdog: notify.Watchdog, wait: Wait = wait_for_stop) -> Wait:
    """The slideshow's wait, petting the watchdog as each wait begins.

    **The programme waits only between steps of its playback**: after a look at
    the cache found nothing ready, after a load the screen answered, and after
    each check of what is up. Each wait therefore follows a step that finished,
    and a step that never finishes (a screen whose renderer stopped answering,
    a brightness exchange that never returns) stops the pets. The listing and
    the fill run on tasks of their own and never wait here.
    """

    async def waiting(stop: asyncio.Event, seconds: float) -> bool:
        watchdog.progress()
        return await wait(stop, seconds)

    return waiting


def settings_page(
    ui: UiSettings,
    *,
    missing: tuple[str, ...] = (),
    mode: str = ARRT_MODE,
    slideshow: SlideshowStatus | None = None,
    heard: Callable[[], HeardReport] | None = None,
) -> server.PageState:
    """The page's state for this start, with a setup code when no password is set yet.

    `heard` is the running supervisor's account of how the server last heard
    this Player, which the page says as the Player's state.
    """
    code = auth.new_setup_code() if store.read(ui.cache_dir).password is None else None
    return server.PageState(
        ui=ui,
        setup_code=code,
        now=time.time,
        throttle=auth.Throttle(monotonic=time.monotonic),
        restart=restart_this_process,
        missing=missing,
        mode=mode,
        slideshow=slideshow,
        heard=heard,
    )


def slideshow_parts(settings: SlideshowSettings, fetcher: Fetcher) -> tuple[RedditSource, MediaCache]:
    """The slideshow's source and cache, with the host resolvers wired to both.

    **Both ends, or the resolvers do nothing useful.** Given only to the source,
    a post that only a resolver can play is listed and has nothing to download;
    given only to the cache, such a post is never listed, and every other post
    still downloads Reddit's copy rather than the host's better one.
    """
    resolvers = Resolvers([RedgifsResolver(fetcher), ImgurResolver()])
    source = RedditSource(settings.listing_url, fetcher, claims=resolvers.claims)
    cache = MediaCache(settings.media_root, fetcher, candidates=resolvers.candidates)
    return source, cache


def slideshow_screen(settings: SlideshowSettings) -> Screen:
    """The screen a slideshow draws on: its HDMI connector, through an mpv this process owns."""
    return MpvOutput(settings.output, runtime_dir=settings.runtime_dir, media_root=settings.media_root)


async def run_slideshow(
    settings: SlideshowSettings,
    ui: UiSettings,
    stop: asyncio.Event,
    *,
    fetcher: Fetcher | None = None,
    screen: Callable[[SlideshowSettings], Screen] = slideshow_screen,
    problems: Callable[[], list[str]] = renderer_problems,
    clock: Clock | None = None,
    watchdog: notify.Watchdog | None = None,
) -> int:
    """Slideshow mode: the programme on its screen and the label panel, beside the settings page, until stopped.

    `READY=1` goes once the page is started and the programme is built, as it
    starts to play; each step of its playback pets `watchdog` (`petting`).
    """
    watchdog = watchdog if watchdog is not None else notify.Watchdog.silent()
    log.info("starting a slideshow", extra={"event": "slideshow.starting", **settings.startup_lines()})
    status = SlideshowStatus(renderer_problems=tuple(problems()))
    for problem in status.renderer_problems:
        # Said and played on: the page and the label still work, and a fix
        # (installing mpv, adding the group) is picked up at the next start.
        log.error(
            "the slideshow cannot draw: %s",
            problem,
            extra={"event": "slideshow.renderer_unavailable", "reason": problem},
        )
    panel = label_panel(settings.panel)
    page = asyncio.create_task(run_settings_page(settings_page(ui, slideshow=status), stop), name="settings-page")
    try:
        async with fetcher if fetcher is not None else Fetcher(cookie=settings.reddit_session) as client:
            source, cache = slideshow_parts(settings, client)
            programme = Slideshow(
                source=source,
                cache=cache,
                screen=screen(settings),
                limits=settings.limits,
                captioner=panel,
                brightness=slideshow_brightness(settings, clock if clock is not None else Clock.system()),
                listing_path=settings.listing_path,
                status=status,
                wait=petting(watchdog),
            )
            watchdog.ready()
            await programme.run(stop)
    finally:
        if panel is not None:
            panel.close()
        stop.set()
        await page
    return 0


def restart_this_process() -> None:
    """SIGTERM to this process: the same orderly stop systemd sends, after which `Restart=always` starts it again."""
    os.kill(os.getpid(), signal.SIGTERM)


def where_to_go(state: server.PageState) -> str:
    """The line printed at start, naming the page's address and, before a password is set, the setup code."""
    if state.setup_code is not None:
        return f"go to {server.setup_url(state.ui, state.setup_code)} to configure"
    return f"go to {server.home_url(state.ui)} to configure"


async def run_settings_page(state: server.PageState, stop: asyncio.Event) -> bool:
    """Serve the page until `stop`. False when its address could not be bound, which is logged."""
    # Printed rather than only logged: the reader is a person who has just started
    # the Player, at a terminal or in `journalctl`. The setup code in it is the
    # one value this process prints that grants anything, and it is spent the
    # moment a password is set.
    print(where_to_go(state), flush=True)  # noqa: T201 -- the operator is told where the settings page is
    log.info(
        "settings page listening",
        extra={"event": "settings_page.listening", **state.ui.startup_lines(), "setup_mode": bool(state.missing)},
    )
    try:
        await server.serve(server.build(state), state.ui, stop)
    except OSError as exc:
        log.error(  # noqa: TRY400 -- an address in use is a deployment fact, not a bug with a useful stack
            "the settings page cannot listen on %s:%s: %s",
            state.ui.bind,
            state.ui.port,
            exc,
            extra={"event": "settings_page.unavailable"},
        )
        return False
    return True


async def _run() -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stop.set)

    ui = load_ui()
    # Before the settings are read, so that its one line about the socket comes
    # first, and nothing is sent for a start refused by configuration below.
    watchdog = notify.Watchdog.from_environment()

    try:
        settings = load()
    except SetupRequired as exc:
        log.warning(
            "%s mode is missing %s, so only the settings page runs",
            exc.mode,
            ", ".join(exc.missing),
            extra={"event": "daemon.setup_mode", "missing": list(exc.missing), "mode": exc.mode},
        )
        page = asyncio.create_task(
            run_settings_page(settings_page(ui, missing=exc.missing, mode=exc.mode), stop), name="settings-page"
        )
        watchdog.ready()
        await watchdog.while_running(page)
        return 0 if await page else 1

    if isinstance(settings, SlideshowSettings):
        return await run_slideshow(settings, ui, stop, watchdog=watchdog)

    if settings.files is not None:
        # Once, and loud: on a wall this would be a Player showing nothing.
        log.warning(
            "this Player draws its walls and labels to files in %s, and drives no screen, television or panel",
            settings.files.directory,
            extra={"event": "files.drawing", **settings.files.file_lines()},
        )
    clock = Clock.system()
    identity = (
        FrameIdentity(frame_tv(settings.frame).read_identity, monotonic=clock.monotonic) if settings.frame is not None else None
    )
    panels = label_panels(settings)
    link = ClientPull(settings, monotonic=clock.monotonic)
    supervisor = PettingSupervisor(
        watchdog=watchdog,
        settings=settings,
        link=link,
        worker=partial(run_wall, settings),
        outputs=lambda: player_outputs(settings, frame_identity=identity.value if identity is not None else None),
        now=clock.now,
        monotonic=clock.monotonic,
        label_worker=partial(run_label, settings, panels, clock=clock) if panels else None,
        label_outputs=lambda: [panel.report() for panel in panels.values()],
        forget_labels=LabelCacheSweep(settings),
        frame_identity=identity,
        settings_url=lambda: settings_url(ui, link.local_address),
    )
    page = asyncio.create_task(run_settings_page(settings_page(ui, heard=supervisor.heard), stop), name="settings-page")
    # Once the settings are read and the panels open, before the first poll: a
    # server down at boot delays no start, and the walls start from the cache.
    watchdog.ready()
    try:
        await supervisor.run(stop)
    finally:
        # After every renderer has stopped. A draw that ran past its budget is
        # left running, so one may still be in the driver: `close` waits for it,
        # for at most the label budget. On e-paper this is the panel's power-down.
        for panel in panels.values():
            panel.close()
        stop.set()
        await page
    return 0


def main() -> int:
    logs.configure()
    try:
        return asyncio.run(_run())
    except ConfigError as exc:
        # Named on stderr as well as logged, because the most likely reader of
        # this failure is a person who has just run the command by hand and a
        # JSON line is the harder of the two to read at a terminal.
        #
        # No traceback: it says a *deployment value* is wrong, and the fix is in
        # `.env`. A stack through `load()` points at this codebase, which is the
        # one place the problem is not.
        log.error("%s", exc, extra={"event": "daemon.misconfigured"})  # noqa: TRY400 -- the fix is in .env, not in a frame
        print(f"Arrt Player cannot start: {exc}", file=sys.stderr)  # noqa: T201 -- the operator is at a terminal
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
