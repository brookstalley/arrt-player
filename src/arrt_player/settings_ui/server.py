"""The settings page: a first-run password, a login, and the settings themselves.

**The one module in the Player that listens on a socket.** It takes only
`aiohttp.web` from aiohttp, and never a client: the isolation guard allows
this file `aiohttp.web` and nothing else, so the Player's one channel to the
server stays the pull (`tests/test_plane_isolation.py`).

**Whoever reaches it first must not get to set the password.** Until one is set,
the page accepts a password only with the setup code minted at start, which the
entry point prints with the page's address. The code is new on every start and
dies when a password is set.

**What it defends against, in proportion.** The realistic attacker is something
else on the LAN (`security-model.md`). So every page but the setup and login
forms needs a session; a session is a cookie signed under a secret stored with
the settings, HttpOnly and `SameSite=Strict`; every POST must carry an `Origin`
naming this page, which with `SameSite=Strict` closes cross-site form posts; wrong
passwords are throttled, per address; and nothing secret is ever written into a
page, a log line or a header.

**It answers only to its own names, which closes DNS rebinding.** A web page open
in a browser on the LAN can point its own domain at this Player's address after
loading, and from then on its requests reach this page as same-origin requests:
its `Origin` and its `Host` both name the rebound domain, so comparing the one
with the other passes it. What gives it away is the name. So a request whose
`Host` is not one of this page's names is refused before any page is served
(`_known_hosts`). Its names are an IP address, which a rebinding page's requests
cannot carry, `localhost`, this machine's own name and `<name>.local`, the
address it is bound to, and whatever `UI_ALLOWED_HOSTS` lists. **The ways a
fresh Player is reached stay open by default**: the address it prints at start
is an IP address, or this machine's own name when it has none to print. A name
only a router's DNS gives it (`<name>.lan`, say) is refused until it is listed,
and the refusal says so.

It is plain HTTP, as the Arrt server is: the password crosses the LAN in the
clear, which is the trust boundary Arrt's `security-model.md` already accepts.

**It also sets the mode, and in slideshow mode the slideshow.** The Reddit cookie
is held as the client token is: taken, never shown back, its expiry read from it
and warned of two weeks ahead. What Reddit last answered is said in words, each
outcome in its own, because each is fixed differently: another listing, a new
cookie, or waiting.
"""

import asyncio
import hashlib
import html
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from aiohttp.web import (
    AppKey,
    Application,
    AppRunner,
    HTTPException,
    Request,
    Response,
    StreamResponse,
    TCPSite,
    middleware,
)

from arrt_player.client import (
    STALE_AFTER_SECONDS,
    ClientDocumentUnreadable,
    HeardReport,
    OutputReport,
    ReportAnswer,
    hdmi_outputs,
    parse_client_document,
    player_name,
)
from arrt_player.config import (
    ALLOWED_HOSTS_SETTING,
    ARRT_MODE,
    CLIENT_DOCUMENT_FILENAME,
    DEFAULT_SLIDESHOW_MAX_SECONDS,
    DEFAULT_SLIDESHOW_MIN_SECONDS,
    DEFAULT_SLIDESHOW_OUTPUT,
    FRAME_OUTPUT,
    PAGE_KEYS_READ,
    SLIDESHOW_MODE,
    ClientSettings,
    ConfigError,
    SetupRequired,
    SlideshowSettings,
    UiSettings,
    client_token_problem,
    dwell_problem,
    is_ip_address,
    normal_host_name,
    player_mode_problem,
    player_name_problem,
    resolve,
    server_url_problem,
    slideshow_output_problem,
    tv_address_problem,
)
from arrt_player.programmes.slideshow_status import SlideshowStatus
from arrt_player.settings_ui import auth, store
from arrt_player.sources import Outcome
from arrt_player.sources.reddit_settings import clean_session, listing_url_problem, session_expiry, session_problem

log = logging.getLogger(__name__)

Handler = Callable[[Request], Awaitable[StreamResponse]]

#: Sent with every response. No framing, no sniffing, no caching of a page that
#: may carry settings, and nothing loaded from anywhere but here.
SECURITY_HEADERS: Final[dict[str, str]] = {
    # Styles and typefaces from this page's own `/static`, and nothing else from
    # anywhere: no inline style, no script.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; font-src 'self'; form-action 'self'; " "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # `same-origin`, never `no-referrer`: under that policy a browser sends
    # `Origin: null` on a form POST (Fetch, "append a request Origin header"),
    # and `_same_origin_posts` would refuse every form the page serves.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}

#: The page's stylesheet and typefaces, shipped inside the package.
STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "static"

#: Each static file's type, said rather than guessed. aiohttp guesses from its
#: own `MimeTypes()` table, which has no `.woff2` on every platform; a typeface
#: served as `application/octet-stream` beside `nosniff` may be refused, and the
#: page then draws in its fallback face looking as if it worked. aiohttp guesses
#: only when the response carries no `Content-Type`, so naming it here wins.
STATIC_TYPES: Final[dict[str, str]] = {".css": "text/css", ".woff2": "font/woff2"}

#: A static file may be kept a day rather than fetched again on every page. That
#: is safe only because the stylesheet's address changes with its content
#: (`stylesheet_href`): a release that changes it is fetched at once, not a day
#: later in a browser still drawing the old layout.
STATIC_CACHE_CONTROL: Final[str] = "public, max-age=86400"


def stylesheet_href(directory: Path = STATIC_DIRECTORY) -> str:
    """The stylesheet's address, carrying a hash of its content, so a changed sheet is a new address."""
    digest = hashlib.sha256((directory / "settings.css").read_bytes()).hexdigest()[:12]
    return f"/static/settings.css?v={digest}"


#: `421 Misdirected Request` for a name this page does not answer to, as Arrt's
#: server answers the same case: the request reached the wrong server as far as
#: this one is concerned.
HOST_REFUSED: Final[int] = 421


def hostname(value: str) -> str:
    """The name in a `Host` header, without its port, lowercased and without a trailing dot.

    An IPv6 address arrives bracketed (`[::1]:8771`), and its colons are part of
    the address, not a port separator.
    """
    return _split_authority(value)[0]


#: The largest number a port can be.
_LARGEST_PORT: Final[int] = 65535


def _split_authority(value: str) -> tuple[str, int | None]:
    """`host[:port]` as (name, port), the port None when absent.

    **A port that is not one makes the name empty**, which no check passes: it
    is ASCII digits naming 1 to 65535, or nothing after the colon. `str.isdigit`
    alone also says yes to digits `int` will not read (`'²'`), which raised in the
    middleware and answered a refusal with a 500.
    """
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        name, rest = (value[1:end], value[end + 1 :]) if end >= 0 else (value[1:], "")
        if rest and not rest.startswith(":"):
            return "", None
        port_text = rest[1:]
    elif value.count(":") == 1:
        name, _, port_text = value.partition(":")
    else:
        name, port_text = value, ""
    if not port_text:
        return normal_host_name(name), None
    if not (port_text.isascii() and port_text.isdigit()) or not 1 <= int(port_text) <= _LARGEST_PORT:
        return "", None
    return normal_host_name(name), int(port_text)


def own_names(bind: str = "") -> frozenset[str]:
    """The names this machine goes by without being told: `localhost`, its host name, and `<name>.local`.

    The host name as `socket.gethostname` gives it, its first label (a Mac's is
    often already `<name>.local`), and that label under `.local`, which is what
    mDNS answers for on a Pi. And the address the page is bound to, when it is
    bound to one: that is what the startup line prints.
    """
    full = hostname(socket.gethostname())
    short = full.split(".", 1)[0]
    names = {"localhost", full, short, f"{short}.local"} if short else {"localhost"}
    if bind not in {"0.0.0.0", "::", ""}:  # noqa: S104 -- recognising the wildcard, not binding it
        names.add(hostname(bind))
    return frozenset(name for name in names if name)


def name_allowed(name: str, allowed: frozenset[str]) -> bool:
    """Whether `name`, already reduced by `hostname`, is one this page answers to. Any IP address is."""
    if not name:
        return False
    return name in allowed or is_ip_address(name)


#: A refused name is said once, and again at most once in this long while it
#: keeps arriving, with the count of refusals since; a name quiet this long is
#: forgotten, its uncounted refusals said as it goes.
REFUSED_HOST_WINDOW_SECONDS: Final[float] = 600.0

#: How many refused names are counted one by one. The names are the caller's
#: to choose, so past this many they are counted together, as one line a window.
REFUSED_HOST_NAMES_KEPT: Final[int] = 32


@dataclass
class _Refusals:
    """One refused name's episode: the refusals not yet said, when it was last said, and when it last arrived."""

    unsaid: int
    said_at: float
    seen_at: float


class RefusedHosts:
    """When a refused name is said in the journal, and with what count.

    **Anybody who can reach the port chooses how often a name is refused**: a
    rebinding page in a browser on the LAN, or anything sending a made-up `Host`.
    Access logging is off, so a line per refusal would let them set the journal's
    rate and rotate out the ERRORs that are the Player's only failure channel
    (`observability-strategy.md`, episodes, not repetition). So each name is said
    when it is first refused; while it keeps arriving, at most once a window,
    with how many refusals the line accounts for; and once it has been quiet a
    window it is forgotten, with a last line if refusals went unsaid. Past
    `REFUSED_HOST_NAMES_KEPT` names, the rest share one count, said the same
    way under no name, so neither the journal nor this table grows with the
    names a caller invents.

    **Every line is written on a refusal, never on a timer**: "said as it is
    forgotten" means at the next refusal of any name after the quiet window.
    So if refusals stop altogether, the last count of each episode is never
    written. That is chosen, not missed: the first refusal of every name is
    always said, so what is lost is how many more came, and a timer would be a
    task of its own in the page for a number nothing acts on.
    """

    def __init__(self, *, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._names: dict[str, _Refusals] = {}
        self._others: _Refusals | None = None

    def refused(self, name: str) -> list[tuple[str | None, int]]:
        """Count a refusal of `name`: the lines owed now, each (name, or None for the rest; refusals it accounts for)."""
        now = self._monotonic()
        lines = self._forget(now)
        if name in self._names or len(self._names) < REFUSED_HOST_NAMES_KEPT:
            refusals = self._names.get(name)
            if refusals is None:
                self._names[name] = _Refusals(unsaid=0, said_at=now, seen_at=now)
                return [*lines, (name, 1)]
            key: str | None = name
        else:
            refusals = self._others
            if refusals is None:
                self._others = _Refusals(unsaid=0, said_at=now, seen_at=now)
                return [*lines, (None, 1)]
            key = None
        refusals.unsaid += 1
        refusals.seen_at = now
        if now - refusals.said_at >= REFUSED_HOST_WINDOW_SECONDS:
            lines.append((key, refusals.unsaid))
            refusals.unsaid, refusals.said_at = 0, now
        return lines

    def _forget(self, now: float) -> list[tuple[str | None, int]]:
        """End the episodes of names quiet for a window, with a line for each that has refusals unsaid."""
        quiet = [key for key, refusals in self._names.items() if now - refusals.seen_at >= REFUSED_HOST_WINDOW_SECONDS]
        ended = [(name, self._names.pop(name).unsaid) for name in quiet]
        lines: list[tuple[str | None, int]] = [(name, unsaid) for name, unsaid in ended if unsaid]
        if self._others is not None and now - self._others.seen_at >= REFUSED_HOST_WINDOW_SECONDS:
            if self._others.unsaid:
                lines.append((None, self._others.unsaid))
            self._others = None
        return lines


#: The port an `Origin` means when it names none.
_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


def origin_allowed(origin: str, *, scheme: str, host: str, listed: frozenset[str]) -> bool:
    """Whether a POST's `Origin` is this page: a listed name on any scheme or port, or the request's own address.

    A listed name passes whatever its scheme and port, because a reverse proxy
    in front of the page changes both. Anything else must be the request's own
    origin: its scheme, and the same name and port as its `Host`, a missing port
    being the scheme's default. `null` and any other shape never pass.
    """
    offered, separator, authority = origin.strip().partition("://")
    if not separator or not authority or "/" in authority or "@" in authority:
        return False
    name, port = _split_authority(authority)
    default = _DEFAULT_PORTS.get(offered.lower())
    if not name or default is None:
        return False
    if name in listed:
        return True
    host_name, host_port = _split_authority(host)
    return offered.lower() == scheme and name == host_name and (port or default) == (host_port or default)


#: An address no packet is sent to (TEST-NET-1, RFC 5737). Connecting a UDP
#: socket to it only asks the kernel which local address would route there.
_ROUTE_PROBE: Final[tuple[str, int]] = ("192.0.2.1", 9)


@dataclass
class PageState:
    """What the page knows beyond its settings file, for the life of the process."""

    ui: UiSettings
    #: The setup code, or None once a password is set. Minted at start.
    setup_code: str | None
    #: Wall time, for cookie expiry. Injected so tests can move it.
    now: Callable[[], float]
    throttle: auth.Throttle
    #: Ends the process so it starts again on what was saved. The entry point
    #: passes a SIGTERM to itself; `Restart=always` brings it back.
    restart: Callable[[], None] = field(default=lambda: None)
    #: The keys missing at start: non-empty means the Player is in setup mode.
    missing: tuple[str, ...] = field(default=())
    #: The mode the Player started in (or, in setup mode, was missing keys for).
    mode: str = ARRT_MODE
    #: A running slideshow's state, for the page to say; None in any other mode.
    slideshow: SlideshowStatus | None = None
    #: The HDMI connectors a slideshow may be put on, read when the page is drawn.
    outputs: Callable[[], list[OutputReport]] = field(default=hdmi_outputs)
    #: How the server last heard this Player (`Supervisor.heard`), read when the
    #: page is drawn; None where no supervisor runs (setup mode, a slideshow),
    #: which in Arrt mode reads as no answer yet.
    heard: Callable[[], HeardReport] | None = None
    #: The names this machine goes by, read once at start (`own_names`).
    #: Injectable so a test can give the machine a name.
    machine_names: Callable[[str], frozenset[str]] = field(default=own_names)
    #: When each refused name is said in the journal (`RefusedHosts`).
    refused_hosts: RefusedHosts = field(default_factory=lambda: RefusedHosts(monotonic=time.monotonic))

    def stored(self) -> store.Stored:
        return store.read(self.ui.cache_dir)


STATE: Final = AppKey("state", PageState)
#: Every name the page answers to, fixed when it is built.
NAMES: Final = AppKey("names", frozenset)


def build(state: PageState) -> Application:
    """The page's application, ready for a runner or a test client."""
    app = Application(middlewares=[_headers, _known_hosts, _same_origin_posts])
    app[STATE] = state
    app[NAMES] = state.machine_names(state.ui.bind) | state.ui.allowed_hosts
    app.router.add_get("/", _home)
    app.router.add_get("/setup", _setup_form)
    app.router.add_post("/setup", _setup)
    app.router.add_get("/login", _login_form)
    app.router.add_post("/login", _login)
    app.router.add_post("/logout", _logout)
    app.router.add_post("/settings", _save)
    app.router.add_post("/password", _change_password)
    # The stylesheet and the two typefaces. Served without a session: the login
    # page wears them too, and they hold nothing.
    app.router.add_static("/static/", STATIC_DIRECTORY, follow_symlinks=False, show_index=False)
    return app


async def serve(app: Application, ui: UiSettings, stop: asyncio.Event) -> None:
    """Listen until `stop` is set. Raises `OSError` when the address cannot be bound."""
    runner = AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await TCPSite(runner, ui.bind, ui.port).start()
        await stop.wait()
    finally:
        await runner.cleanup()


def address_to_visit(ui: UiSettings) -> str:
    """The host a person should type: the bound address, or this machine's LAN address when bound to all."""
    if ui.bind not in {"0.0.0.0", "::", ""}:  # noqa: S104 -- recognising the wildcard, not binding it
        return ui.bind
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(_ROUTE_PROBE)
            return str(probe.getsockname()[0])
    except OSError:
        return socket.gethostname()


def _authority(ui: UiSettings) -> str:
    host = address_to_visit(ui)
    # An IPv6 literal needs brackets in a URL, or the port reads as part of it.
    return f"[{host}]:{ui.port}" if ":" in host else f"{host}:{ui.port}"


def setup_url(ui: UiSettings, code: str) -> str:
    return f"http://{_authority(ui)}/setup?code={code}"


def home_url(ui: UiSettings) -> str:
    return f"http://{_authority(ui)}/"


# -- middleware ------------------------------------------------------------------


@middleware
async def _headers(request: Request, handler: Handler) -> StreamResponse:
    try:
        response = await handler(request)
    except HTTPException as raised:
        # aiohttp's own 404 and 405 are raised, not returned; they carry the
        # headers too, so that "every response" is true of them as well.
        raised.headers.update(SECURITY_HEADERS)
        raise
    response.headers.update(SECURITY_HEADERS)
    # Only a file actually served may be kept: a refusal kept a day would go on
    # refusing after the reason for it was fixed.
    if request.path.startswith("/static/") and response.status == 200:  # noqa: PLR2004 -- HTTP's OK
        response.headers["Cache-Control"] = STATIC_CACHE_CONTROL
        kind = STATIC_TYPES.get(Path(request.path).suffix)
        if kind is not None:
            response.headers["Content-Type"] = kind
    return response


@middleware
async def _known_hosts(request: Request, handler: Handler) -> StreamResponse:
    """Refuse, before any page, a request by a name this page does not answer to.

    `request.host` is the `Host` header as sent, port and all, or, when there is
    none, the local address the request arrived on, which is an IP address.
    """
    name = hostname(request.host)
    if not name_allowed(name, request.app[NAMES]):
        _say_refused(_state(request).refused_hosts.refused(name))
        return _page(
            "Not this page's name",
            f"<p>This Player's settings page does not answer to the name {html.escape(repr(name))}. Open it by "
            "its address, as the Player printed when it started, or by this machine's own name. If that name "
            f"is how you reach it, add it to {ALLOWED_HOSTS_SETTING} in .env and restart.</p>",
            status=HOST_REFUSED,
        )
    return await handler(request)


def _say_refused(lines: list[tuple[str | None, int]]) -> None:
    for name, refused in lines:
        named = repr(name) if name is not None else f"other than the {REFUSED_HOST_NAMES_KEPT} it counts by name"
        log.warning(
            "the settings page refused %d request(s) for the name %s; it is not one of this page's names",
            refused,
            named,
            extra={"event": "settings_page.host_refused", "host": name, "refused": refused, "setting": ALLOWED_HOSTS_SETTING},
        )


@middleware
async def _same_origin_posts(request: Request, handler: Handler) -> StreamResponse:
    """Refuse a POST whose `Origin` is not this page. A browser sends one on every form POST."""
    if request.method == "POST" and not origin_allowed(
        request.headers.get("Origin", ""), scheme=request.scheme, host=request.host, listed=_state(request).ui.allowed_hosts
    ):
        return _page("Refused", "<p>This form must be submitted from the settings page itself.</p>", status=403)
    return await handler(request)


def _source(request: Request) -> str:
    """The address a request came from, for the throttle. Empty when the transport has none to give."""
    return request.remote or ""


# -- pages -----------------------------------------------------------------------


def _state(request: Request) -> PageState:
    return request.app[STATE]


def _signed_in(request: Request, stored: store.Stored) -> bool:
    """Whether the request carries a valid session; one that does gives its address standing (`auth.Throttle.vouch`).

    The standing a sign-in gave is in memory, and a save restarts the Player, so
    a signed-in visit is what restores it for the owner's browser.
    """
    state = _state(request)
    if not auth.session_valid(request.cookies.get(auth.COOKIE_NAME), stored.session_secret, now=state.now()):
        return False
    state.throttle.vouch(_source(request))
    return True


async def _home(request: Request) -> Response:
    state = _state(request)
    stored = state.stored()
    if stored.password is None:
        return _page(
            "Set a password",
            "<p>This Player has no password yet. Open the link it printed when it started, "
            "which carries a one-time setup code.</p>",
        )
    if not _signed_in(request, stored):
        return _redirect("/login")
    return _settings_page(state, stored)


#: The keys that move together. Changing the server while keeping a token would
#: hand that token to whoever runs the new server, so when the environment pins
#: either, the page locks both.
_SERVER_PAIR: Final[frozenset[str]] = frozenset({"SERVER_URL", "CLIENT_TOKEN"})


def _server_name(state: PageState) -> str:
    """What the server calls this Player, from its cached client document; empty before it is paired."""
    return player_name(state.ui.cache_dir, "")


def _locked(state: PageState) -> frozenset[str]:
    """The keys the page may not change: those the environment pins, the pair with either, and a name the server gives."""
    pinned = state.ui.pinned
    locked = pinned | _SERVER_PAIR if pinned & _SERVER_PAIR else pinned
    # Once the server names this Player its name wins, so the page shows that
    # one read-only and leaves the Player's own, the fallback, as it was saved.
    return locked | {"PLAYER_NAME"} if _server_name(state) else locked


def _shown(state: PageState, stored: store.Stored, name: str) -> str:
    """A key's value as the Player will use it: the environment's when pinned, else the page's."""
    return state.ui.pinned_values.get(name, "") if name in state.ui.pinned else stored.values.get(name, "")


def _in_use(state: PageState, values: Mapping[str, str], name: str) -> str:
    """A key's value as the next start will read it, the page's file holding `values`. See `config.resolve`."""
    return state.ui.pinned_values.get(name, "") if name in state.ui.pinned else values.get(name, "")


async def _save(request: Request) -> Response:
    """Save the form, then say what changed and what happens next. Nothing changed means nothing written.

    **Judged as the next start will judge it** (`config.resolve`), so the page
    never writes a file the Player would refuse to start on; a field is also
    judged on its own first, so one form says every field's problem at once.
    **A new television address forgets the old set's pairing** before the file
    is written: the token belongs to the set that issued it, and is never sent
    to another address. **It restarts the Player only when what it runs on
    changes**: in setup mode, or when `config.resolve` gives the file after the
    save something other than it gives the file before.
    """
    state = _state(request)
    stored = state.stored()
    if stored.password is None or not _signed_in(request, stored):
        return _redirect("/login")
    values, problems = _form_values(state, stored, await request.post())
    if problems:
        return _settings_page(state, stored, problem=" ".join(problems), status=400)
    if values == dict(stored.values):
        return _settings_page(state, stored, notice=_NOTHING_CHANGED)
    return _commit(state, stored, values)


def _form_values(state: PageState, stored: store.Stored, form: Mapping[str, object]) -> tuple[dict[str, str], list[str]]:
    """The page's values after the form, and what is wrong with the form; the values mean nothing if anything is."""
    locked = _locked(state)
    url = str(form.get("server_url", "")).strip().rstrip("/")
    token = str(form.get("client_token", "")).strip()
    # A field the form does not carry is left as it is, as the slideshow's are:
    # the page draws the name read-only once the server names the Player.
    name = _entry(form, "player_name", "PLAYER_NAME", locked)
    television = _entry(form, "tv_address", "TV_ADDRESS", locked)

    entries = _slideshow_entries(form, locked)
    session = "" if "REDDIT_SESSION" in locked else clean_session(str(form.get("reddit_session", "")))

    problems = _problems(locked, url=url, token=token, name=name, television=television)
    problems += _slideshow_problems(entries, session, lambda key: _shown(state, stored, key))
    values = _next_values(stored.values, locked, url=url, token=token, name=name, television=television)
    for key, value in entries.items():
        _put(values, key, value)
    if session:
        values["REDDIT_SESSION"] = session
    return values, problems


def _commit(state: PageState, stored: store.Stored, values: dict[str, str]) -> Response:
    """Judge, forget the old set's pairing if the address moves, write, restart if the running mode reads a change."""
    try:
        outcome: ClientSettings | SlideshowSettings | SetupRequired = resolve(_environment(state), values)
    except SetupRequired as missing:
        outcome = missing
    except ConfigError as exc:
        return _settings_page(state, stored, problem=f"{_WOULD_NOT_START} {exc}", status=400)

    moved = stored.values.get("TV_ADDRESS", "") != values.get("TV_ADDRESS", "")
    forgotten = _forget_pairing(state) if moved else False
    if forgotten is None:
        return _settings_page(state, stored, problem=_PAIRING_NOT_FORGOTTEN, status=500)
    if not _wrote(state, stored.with_values(values)):
        return _settings_page(state, stored, problem=_WRITE_FAILED_AFTER_FORGETTING if forgotten else _WRITE_FAILED, status=500)

    changed = _changed(stored.values, values)
    restarting = bool(state.missing) or _judged(state, stored.values) != outcome
    log.info(
        "the settings page saved new settings; %s",
        "restarting onto them" if restarting else "nothing restarts, since the running mode reads none of them",
        extra={
            "event": "settings_page.saved",
            "changed": sorted(changed),
            "restarting": restarting,
            "pairing_forgotten": forgotten,
        },
    )
    if restarting:
        # After the answer has gone out, so the browser sees the page say so.
        asyncio.get_running_loop().call_later(_RESTART_DELAY_SECONDS, state.restart)
    said = _what_the_save_did(state, stored.values, values, restarting=restarting, outcome=outcome, forgotten=forgotten)
    return _page("Saved", "".join(f"<p>{html.escape(sentence)}</p>" for sentence in said) + _BACK)


def _judged(state: PageState, values: Mapping[str, str]) -> object:
    """What the Player runs on with its file holding `values`, or the refusal, which equals nothing else."""
    try:
        return resolve(_environment(state), values)
    except (ConfigError, SetupRequired) as refused:
        return refused


def _running_mode(state: PageState) -> str:
    """The mode the Player runs in, or in setup mode lacks keys for.

    **A running slideshow is known by its status**, not by `mode`: the entry
    point hands the page a slideshow's status and leaves `mode` at its default.
    Read from `mode` alone, a slideshow's own settings would be taken for keys
    it does not read, and saved without the restart that applies them.
    """
    return SLIDESHOW_MODE if state.slideshow is not None else state.mode


def _entry(form: Mapping[str, object], field_name: str, key: str, locked: frozenset[str]) -> str | None:
    """A field's value, or None when the form does not carry it or the page may not change its key."""
    return None if key in locked or field_name not in form else str(form[field_name]).strip()


def _environment(state: PageState) -> dict[str, str]:
    """The environment the next start will read, as `config.resolve` takes it."""
    return {**state.ui.environment, "CACHE_DIR": str(state.ui.cache_dir)}


def _problems(locked: frozenset[str], *, url: str, token: str, name: str | None, television: str | None) -> list[str]:
    """What is wrong with the form, judged as `config.load` judges the same values. A locked key is not read."""
    found = []
    if "SERVER_URL" not in locked:
        if url and (said := server_url_problem(url)):
            found.append(said)
        if token and (said := client_token_problem(token)):
            found.append(said)
        if token and not url:
            found.append("A token belongs to a server: set the server's address with it.")
    if name is not None and (said := player_name_problem(name)):
        found.append(said)
    if television is not None and (said := tv_address_problem(television)):
        found.append(said)
    return found


def _next_values(
    before: Mapping[str, str],
    locked: frozenset[str],
    *,
    url: str,
    token: str,
    name: str | None,
    television: str | None,
) -> dict[str, str]:
    """The page's values after the form. A new server never inherits the old one's token."""
    values = dict(before)
    if "SERVER_URL" not in locked:
        if url != before.get("SERVER_URL", "") or not url:
            values.pop("CLIENT_TOKEN", None)
        _put(values, "SERVER_URL", url)
        if token and url:
            values["CLIENT_TOKEN"] = token
    if name is not None:
        _put(values, "PLAYER_NAME", name)
    if television is not None:
        _put(values, "TV_ADDRESS", television)
    return values


def _forget_pairing(state: PageState) -> bool | None:
    """Remove the television's pairing token: True if one was removed, False if there was none, None if it would not go.

    **Removed, never emptied**: a missing file is what a first pairing starts
    from, while the library reads an empty one differently, in a way never tried
    against a set (`tv.samsung.SamsungTv._keep_the_token_private`). A path the
    Frame would refuse (`UiSettings.tv_token_file` None) holds no token in use.
    """
    path = state.ui.tv_token_file
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.error(  # noqa: TRY400 -- a permission or a read-only disk is the fact; a stack adds nothing
            "the settings page could not remove the television's pairing token at %s (%s), so it saved nothing",
            path,
            type(exc).__name__,
            extra={"event": "settings_page.pairing_not_forgotten", "tv_token_file": str(path)},
        )
        return None
    log.info(
        "the settings page forgot the television's pairing, because its address changed",
        extra={"event": "settings_page.pairing_forgotten", "tv_token_file": str(path)},
    )
    return True


def _what_the_save_did(
    state: PageState,
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    restarting: bool,
    outcome: ClientSettings | SlideshowSettings | SetupRequired,
    forgotten: bool,
) -> list[str]:
    """In sentences: what changed, what restarts, which mode it is in, and whether a television will ask to pair."""
    changed = _changed_in_meaning(before, after)
    labels = [label for key, label in _LABELS.items() if key in changed]
    said = [f"Saved: {_listed(labels)}." if labels else "Saved, though nothing it does changes."]
    mode, running = _in_use(state, after, "PLAYER_MODE") or ARRT_MODE, _running_mode(state)
    if restarting:
        start = "The Player is restarting onto them"
        if mode != running:
            start = f"The Player is restarting as {_MODE_NAMES[mode]}"
        walls = _wall_names(state) if running == ARRT_MODE and not state.missing else []
        if walls:
            start += f", and with it every wall it shows: {_listed(walls)}"
        said.append(f"{start}. Under systemd it is back in a few seconds; started by hand, start it again.")
        if isinstance(outcome, SetupRequired):
            verb = "is" if len(outcome.missing) == 1 else "are"
            missing = [_LABELS.get(key, key) for key in outcome.missing]
            said.append(f"Until {_listed(missing)} {verb} set, it serves only this page.")
    waiting = changed - PAGE_KEYS_READ[mode]
    if waiting and mode == running:
        said.append(("" if restarting else "Nothing restarts. ") + _STAYS[mode])

    old, new = before.get("TV_ADDRESS", ""), after.get("TV_ADDRESS", "")
    if old != new:
        if new:
            runs_now = restarting and isinstance(outcome, ClientSettings) and outcome.frame is not None
            when = "as the Player starts again" if runs_now else "the next time the Player runs its Frame"
            said.append(
                f"The television at {new} will be asked to pair {when}: expect a prompt on that set, even over a "
                "programme, and accept it there. Until then this Player shows nothing new on it."
            )
            said.append(_PICTURES_AT_A_NEW_ADDRESS.format(new=new, old=f"the television at {old}" if old else _AN_EARLIER_SET))
        else:
            said.append(
                "This Player no longer drives a Frame: a wall placed on its Samsung Frame waits for an address. "
                "The pictures it put on the television stay there."
            )
        if forgotten:
            said.append(
                f"The pairing with the television at {old} is forgotten."
                if old
                else "The pairing kept from an earlier television is forgotten."
            )
    return said


#: What an empty value means, for the keys whose field always posts one (a
#: choice and a menu): a form saved untouched writes them, and that is no change.
_MEANT_WHEN_EMPTY: Final[dict[str, str]] = {"PLAYER_MODE": ARRT_MODE, "SLIDESHOW_OUTPUT": DEFAULT_SLIDESHOW_OUTPUT}


def _changed_in_meaning(before: Mapping[str, str], after: Mapping[str, str]) -> set[str]:
    """The keys whose value means something else after the save: `_changed`, with an empty choice its default."""
    return {
        name
        for name in _changed(before, after)
        if (before.get(name) or _MEANT_WHEN_EMPTY.get(name, "")) != (after.get(name) or _MEANT_WHEN_EMPTY.get(name, ""))
    }


def _wall_names(state: PageState) -> list[str]:
    """The walls the cached client document places on this Player, by name; none when it cannot be read."""
    try:
        document = parse_client_document((state.ui.cache_dir / CLIENT_DOCUMENT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ClientDocumentUnreadable):
        return []
    return [wall.name for wall in document.walls]


def _listed(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


#: The words the page labels the two pairing fields with, **and the only place
#: they are written**. The server's dialog that issues a token must say the same,
#: so the contract owns them (`vocabulary()`'s `pairing`). They are quoted here
#: rather than read from the package because `arrt-contract` is a development
#: dependency the Player does not install, and
#: `tests/test_player_contract.py` holds the quote to the package.
PAIRING_LABELS: Final[Mapping[str, str]] = MappingProxyType({"SERVER_URL": "Server address", "CLIENT_TOKEN": "Token"})

#: Each key the page manages, by the label of its field, in the order the page
#: draws them. What a save says it saved is said in these words.
_LABELS: Final[dict[str, str]] = {
    "PLAYER_MODE": "Mode",
    "SERVER_URL": PAIRING_LABELS["SERVER_URL"],
    "CLIENT_TOKEN": PAIRING_LABELS["CLIENT_TOKEN"],
    "TV_ADDRESS": "Television address",
    "SLIDESHOW_URL": "Listing address",
    "REDDIT_SESSION": "Reddit cookie",
    "SLIDESHOW_MIN_SECONDS": "Shortest time on screen",
    "SLIDESHOW_MAX_SECONDS": "Longest time on screen",
    "SLIDESHOW_OUTPUT": "Screen",
    "PLAYER_NAME": "Name",
}

#: Each mode as the Mode section's choices name it.
_MODE_CHOICES: Final[dict[str, str]] = {ARRT_MODE: "Walls from an Arrt server", SLIDESHOW_MODE: "A Reddit slideshow"}

#: Each mode as a save says the Player restarts into it.
_MODE_NAMES: Final[dict[str, str]] = {ARRT_MODE: "a Player of walls from its Arrt server", SLIDESHOW_MODE: "a Reddit slideshow"}

#: What a save says when it changed what the running mode does not read (#46).
_STAYS: Final[dict[str, str]] = {
    ARRT_MODE: (
        "The Player stays in Arrt mode, showing walls from its server: the slideshow's settings wait until the "
        f"mode is {_MODE_CHOICES[SLIDESHOW_MODE]}."
    ),
    SLIDESHOW_MODE: (
        "The Player stays a Reddit slideshow: the server's and the television's settings wait until the mode is "
        f"{_MODE_CHOICES[ARRT_MODE]}."
    ),
}

#: Said when a save would change nothing.
_NOTHING_CHANGED: Final[str] = "Nothing changed, so nothing was saved and nothing restarts."

#: Said before `config.load`'s own words, when the Player would not start on the form.
_WOULD_NOT_START: Final[str] = "Not saved: with these settings the Player would not start."

#: Said when the old set's token could not be removed, so the new address is not saved.
_PAIRING_NOT_FORGOTTEN: Final[str] = (
    "Not saved: the pairing with the television at the old address could not be forgotten, and the set at a new "
    "address must not be sent it. Nothing changed. The journal says why."
)

_BACK: Final[str] = '<p><a href="/">Back to the settings</a></p>'


#: The slideshow's form fields, by the key each sets. The cookie is apart: it is never shown back.
_SLIDESHOW_FIELDS: Final[dict[str, str]] = {
    "PLAYER_MODE": "player_mode",
    "SLIDESHOW_URL": "slideshow_url",
    "SLIDESHOW_MIN_SECONDS": "slideshow_min_seconds",
    "SLIDESHOW_MAX_SECONDS": "slideshow_max_seconds",
    "SLIDESHOW_OUTPUT": "slideshow_output",
}


def _slideshow_entries(form: Mapping[str, object], locked: frozenset[str]) -> dict[str, str]:
    """The slideshow fields the form carries and the page may change. A field not on the form is left as it is."""
    return {key: str(form[name]).strip() for key, name in _SLIDESHOW_FIELDS.items() if name in form and key not in locked}


def _slideshow_problems(entries: Mapping[str, str], session: str, current: Callable[[str], str]) -> list[str]:
    """What is wrong with the slideshow's fields, judged as `config.load` judges them. Empty is a default, not a problem."""
    found = []
    if said := player_mode_problem(entries.get("PLAYER_MODE", "")):
        found.append(said)
    if (url := entries.get("SLIDESHOW_URL")) and (said := listing_url_problem(url)):
        found.append(said)
    if session and (said := session_problem(session)):
        found.append(said)
    if {"SLIDESHOW_MIN_SECONDS", "SLIDESHOW_MAX_SECONDS"} & set(entries):
        # Judged together, and against what the other one will be: a new minimum
        # above the maximum already saved is as wrong as both typed at once.
        low, high = (entries.get(key, current(key)) for key in ("SLIDESHOW_MIN_SECONDS", "SLIDESHOW_MAX_SECONDS"))
        if said := dwell_problem(low, high):
            found.append(said)
    if said := slideshow_output_problem(entries.get("SLIDESHOW_OUTPUT", "")):
        found.append(said)
    return found


#: How long a save waits before restarting, so its answer reaches the browser.
_RESTART_DELAY_SECONDS: Final[float] = 0.5


#: Said when the settings file could not be written: nothing changed, and why to look.
_WRITE_FAILED: Final[str] = "The settings could not be saved, so nothing changed. The journal says why."

#: Said when the file could not be written after the old set's token was removed:
#: the address is as it was, and the set there will ask to pair again.
_WRITE_FAILED_AFTER_FORGETTING: Final[str] = (
    "The settings could not be saved, so the television's address is as it was; but the pairing with that set "
    "was already forgotten, so it will ask to pair again. The journal says why."
)


def _wrote(state: PageState, stored: store.Stored) -> bool:
    """Write the settings file; False, logged, when the disk refuses. Nothing is restarted on a False."""
    try:
        store.write(state.ui.cache_dir, stored)
    except OSError as exc:
        log.error(  # noqa: TRY400 -- a full disk or a permission is the fact; a stack adds nothing
            "the settings page could not write %s: %s",
            store.path_in(state.ui.cache_dir),
            exc,
            extra={"event": "settings_page.write_failed"},
        )
        return False
    return True


def _put(values: dict[str, str], name: str, value: str) -> None:
    if value:
        values[name] = value
    else:
        values.pop(name, None)


def _changed(before: Mapping[str, str], after: Mapping[str, str]) -> set[str]:
    """The keys whose value differs. Names only: a value may be the token."""
    return {name for name in set(before) | set(after) if before.get(name) != after.get(name)}


async def _change_password(request: Request) -> Response:
    """A new password, given the current one. A new session secret with it ends every other session."""
    state = _state(request)
    stored = state.stored()
    if stored.password is None or not _signed_in(request, stored):
        return _redirect("/login")
    form = await request.post()
    current, password, again = (str(form.get(name, "")) for name in ("current", "password", "again"))
    # The current password is a guess like any login's, so it is throttled like one.
    source = _source(request)
    wait = state.throttle.wait_seconds(source)
    if wait > 0:
        return _settings_page(state, stored, problem=f"Too many wrong passwords. Try again in {int(wait) + 1} s.", status=429)
    state.throttle.failed(source)
    if not await asyncio.to_thread(auth.password_matches, current, stored.password):
        log.warning("a wrong current password was offered", extra={"event": "settings_page.password_refused", "remote": source})
        return _settings_page(state, stored, problem="The current password is not right.", status=403)
    state.throttle.succeeded(source)
    problem = _password_problem(password, again)
    if problem:
        return _settings_page(state, stored, problem=problem, status=400)
    secret = auth.new_session_secret()
    encoded = await asyncio.to_thread(auth.hash_password, password)
    if not _wrote(state, state.stored().with_auth(password=encoded, session_secret=secret)):
        return _settings_page(state, stored, problem=_WRITE_FAILED, status=500)
    # Every other session has ended, and so does every address's standing.
    state.throttle.forget_standing()
    state.throttle.vouch(source)
    log.info("the settings page's password is changed", extra={"event": "settings_page.password_changed"})
    return _signed_in_redirect(secret, state)


async def _setup_form(request: Request) -> Response:
    state = _state(request)
    if state.stored().password is not None:
        return _redirect("/login")
    code = request.query.get("code", "")
    if state.setup_code is None or not auth.code_matches(code, state.setup_code):
        return _bad_code()
    return _setup_page(code)


async def _setup(request: Request) -> Response:
    state = _state(request)
    stored = state.stored()
    if stored.password is not None:
        return _redirect("/login")
    form = await request.post()
    code, password, again = (str(form.get(name, "")) for name in ("code", "password", "again"))
    if state.setup_code is None or not auth.code_matches(code, state.setup_code):
        return _bad_code()
    problem = _password_problem(password, again)
    if problem:
        return _setup_page(code, problem=problem, status=400)

    # Spent before the hash's await, so a second request carrying the same code
    # cannot pass the check above while this one is hashing.
    state.setup_code = None
    secret = auth.new_session_secret()
    encoded = await asyncio.to_thread(auth.hash_password, password)
    if not _wrote(state, state.stored().with_auth(password=encoded, session_secret=secret)):
        # Given back, since no password was set: without it every retry would be
        # told the link is not this start's until somebody restarted the Player.
        state.setup_code = code
        return _setup_page(code, problem=_WRITE_FAILED, status=500)
    state.throttle.forget_standing()
    state.throttle.vouch(_source(request))
    log.info("the settings page's password is set", extra={"event": "settings_page.password_set"})
    return _signed_in_redirect(secret, state)


async def _login_form(request: Request) -> Response:
    if _state(request).stored().password is None:
        return _redirect("/")
    return _login_page()


async def _login(request: Request) -> Response:
    state = _state(request)
    stored = state.stored()
    if stored.password is None:
        return _redirect("/")
    form = await request.post()
    # Checked and charged with no await between, so concurrent attempts cannot
    # all pass the check before any is counted: each is a failure until proven
    # otherwise, and a right password clears the charge below.
    source = _source(request)
    wait = state.throttle.wait_seconds(source)
    if wait > 0:
        return _login_page(problem=f"Too many wrong passwords. Try again in {int(wait) + 1} s.", status=429)
    state.throttle.failed(source)
    # scrypt is tenths of a second and 16 MiB on a Pi, so it runs on a thread
    # rather than on the loop that drives the walls.
    offered = str(form.get("password", ""))
    matched = await asyncio.to_thread(auth.password_matches, offered, stored.password)
    if stored.session_secret is None or not matched:
        log.warning(
            "a wrong password was offered to the settings page",
            extra={"event": "settings_page.login_refused", "remote": source},
        )
        return _login_page(problem="That is not the password.", status=403)
    state.throttle.succeeded(source)
    return _signed_in_redirect(stored.session_secret, state)


async def _logout(_request: Request) -> Response:
    response = _redirect("/login")
    response.del_cookie(auth.COOKIE_NAME, path="/")
    return response


# -- rendering -------------------------------------------------------------------


def _redirect(location: str) -> Response:
    """A 303, returned rather than raised, so the middleware's headers reach it as they reach every page."""
    return Response(status=303, headers={"Location": location})


def _signed_in_redirect(secret: str, state: PageState) -> Response:
    response = _redirect("/")
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.session_cookie(secret, now=state.now()),
        max_age=auth.SESSION_SECONDS,
        path="/",
        httponly=True,
        samesite="Strict",
    )
    return response


def _settings_page(
    state: PageState,
    stored: store.Stored,
    *,
    problem: str | None = None,
    notice: str | None = None,
    status: int = 200,
) -> Response:
    """The page's own: the Player's name, a line of facts, its toolbar, then every setting, one save, and Password."""
    locked = _locked(state)
    local = _shown(state, stored, "PLAYER_NAME")
    name = _server_name(state) or local
    token_set = "CLIENT_TOKEN" in state.ui.pinned or bool(stored.values.get("CLIENT_TOKEN"))

    if state.missing:
        shown = "nothing is shown" if state.mode == SLIDESHOW_MODE else "no walls are shown"
        # A Player started without its token is one the server has not yet
        # issued one, a step of setting it up, said in the server's words. Only
        # Arrt mode requires the token, so only it can be missing it.
        facts = (
            _state_word(_PLAYER_STATE_LOOK["no_token"], PLAYER_STATE_WORDS["no_token"])
            if "CLIENT_TOKEN" in state.missing
            else _state_word("warn", "Setup mode")
        ) + f" · {html.escape(', '.join(state.missing))} not set when the Player started, so {shown}."
    elif state.slideshow is not None:
        facts = _state_word("good", "Running") + " · a Reddit slideshow"
    else:
        heard = state.heard() if state.heard is not None else HeardReport(answer=None)
        facts = _player_state_word(heard) + " · pulling from " + html.escape(_shown(state, stored, "SERVER_URL"))
    if name != local:
        facts += f" · Its server calls it {html.escape(name)}, and that name wins over its own."

    body = (
        f'<p class="page-facts">{facts}</p>'
        '<div class="page-toolbar"><form method="post" action="/logout">'
        '<button class="action quiet" type="submit">Log out</button></form></div>'
        + _notes(problem=problem, notice=notice)
        + '<form method="post" action="/settings">'
        + _mode_section(state, stored)
        + '<section class="panel"><h2>Server</h2>'
        + _field(
            PAIRING_LABELS["SERVER_URL"],
            "server_url",
            _shown(state, stored, "SERVER_URL"),
            locked="SERVER_URL" in locked,
            pinned=_pinned_note(state, stored, "SERVER_URL"),
            hint="For example http://arrt.local:8770. Changing it clears the token unless you enter one with it. "
            "In .env, SERVER_URL.",
        )
        + _token_field(locked="CLIENT_TOKEN" in locked, pinned=_pinned_note(state, stored, "CLIENT_TOKEN"), token_set=token_set)
        + "</section>"
        + _frame_section(state, stored)
        + _slideshow_section(state, stored)
        + '<section class="panel"><h2>This Player</h2>'
        + _name_field(state, stored, local=local)
        + "</section>"
        + _captioned_act("Save settings", _SAVE_CAPTION)
        + '</form><section class="panel"><h2>Password</h2><form method="post" action="/password">'
        + _password_input("current", "Current password", "current-password")
        + _password_input("password", "New password", "new-password", hint=_PASSWORD_HINT)
        + _password_input("again", "New password again", "new-password")
        + '<button class="action" type="submit">Change password</button></form></section>'
    )
    title = f"Settings · {name}" if name else "Settings"
    return _page(title, body, heading=name or "This Player", status=status)


#: How far ahead of the cookie's expiry the page warns (*Inferred*: whether
#: Reddit renews the cookie is not measured, so two weeks to fetch a new one).
EXPIRY_WARNING_S: Final[float] = 14 * 24 * 3600

#: What each of Reddit's answers means to the person at the page, and what fixes it.
_OUTCOME_WORDS: Final[dict[Outcome, tuple[str, str, str]]] = {
    Outcome.LISTED: ("good", "Listed", "Reddit gave the listing."),
    Outcome.REFUSED: (
        "warn",
        "Listing refused",
        (
            "Reddit refuses this listing even with a good cookie. Try its top listing instead, such as …/top/?t=month; "
            "new, hot and rising are refused."
        ),
    ),
    Outcome.COOKIE_REJECTED: (
        "warn",
        "Cookie not accepted",
        "Reddit no longer accepts the cookie. Paste a new one from a browser where you are logged in.",
    ),
    Outcome.RATE_LIMITED: ("warn", "Slowed down", "Reddit asked the Player to wait; it asks again later."),
    Outcome.UNREACHABLE: ("warn", "Unreachable", "Reddit could not be reached."),
}


def _mode_section(state: PageState, stored: store.Stored) -> str:
    """The mode: an Arrt server's walls, or a Reddit slideshow. One or the other, never both."""
    current = _shown(state, stored, "PLAYER_MODE") or ARRT_MODE
    pinned = _pinned_note(state, stored, "PLAYER_MODE")
    disabled = " disabled" if pinned else ""
    choices = "".join(
        f'<label class="choice"><input type="radio" name="player_mode" value="{value}"'
        f'{" checked" if value == current else ""}{disabled}>{html.escape(words)}</label>'
        for value, words in _MODE_CHOICES.items()
    )
    note = pinned or (
        "The Player runs one or the other. Saving a new choice restarts it in that mode; the settings of the other "
        "mode are kept for when it is chosen."
    )
    return (
        '<section class="panel"><h2>Mode</h2><fieldset class="choices" aria-describedby="player_mode-hint">'
        f"<legend>What this Player shows</legend>{choices}</fieldset>"
        f'<p class="hint" id="player_mode-hint">{html.escape(note)}</p></section>'
    )


def _slideshow_section(state: PageState, stored: store.Stored) -> str:
    """The slideshow's listing, cookie, dwell and screen, and what Reddit last said."""
    pinned = state.ui.pinned
    return (
        '<section class="panel"><h2>Slideshow</h2>'
        + _slideshow_status(state.slideshow, now=state.now())
        + _field(
            _LABELS["SLIDESHOW_URL"],
            "slideshow_url",
            _shown(state, stored, "SLIDESHOW_URL"),
            locked="SLIDESHOW_URL" in pinned,
            pinned=_pinned_note(state, stored, "SLIDESHOW_URL"),
            hint="A subreddit or multireddit as your browser shows it, such as "
            "https://www.reddit.com/r/EarthPorn/top/?t=month. Shown in its order, looped.",
        )
        + _cookie_field(state, stored)
        + _COOKIE_STEPS
        + _field(
            f"{_LABELS['SLIDESHOW_MIN_SECONDS']}, in seconds",
            "slideshow_min_seconds",
            _shown(state, stored, "SLIDESHOW_MIN_SECONDS"),
            locked="SLIDESHOW_MIN_SECONDS" in pinned,
            pinned=_pinned_note(state, stored, "SLIDESHOW_MIN_SECONDS"),
            hint=f"A still shows this long; a short clip plays again until it has. Empty is {DEFAULT_SLIDESHOW_MIN_SECONDS:g}.",
        )
        + _field(
            f"{_LABELS['SLIDESHOW_MAX_SECONDS']}, in seconds",
            "slideshow_max_seconds",
            _shown(state, stored, "SLIDESHOW_MAX_SECONDS"),
            locked="SLIDESHOW_MAX_SECONDS" in pinned,
            pinned=_pinned_note(state, stored, "SLIDESHOW_MAX_SECONDS"),
            hint=f"A long clip is cut here. Empty is {DEFAULT_SLIDESHOW_MAX_SECONDS:g}.",
        )
        + _output_field(state, stored)
        + "</section>"
    )


def _slideshow_status(status: SlideshowStatus | None, *, now: float) -> str:
    """What Reddit last answered and what the cache holds, in words; nothing outside a running slideshow."""
    if status is None:
        return ""
    cannot_draw = "".join(
        f'<p class="note error" role="alert">The slideshow cannot draw: {html.escape(problem)}.</p>'
        for problem in status.renderer_problems
    )
    return cannot_draw + _answer_words(status, now=now)


def _answer_words(status: SlideshowStatus, *, now: float) -> str:
    """What Reddit last answered and what the cache holds, in words."""
    if status.outcome is None:
        said = "Reddit has not answered yet."
        if status.from_saved:
            said += f" Playing the {status.items} items saved from the last listing."
        return f'<p class="page-facts" role="status">{_state_word("warn", "Waiting")} · {html.escape(said)}</p>'
    kind, word, meaning = _OUTCOME_WORDS[status.outcome]
    said = meaning
    if status.answered_at is not None:
        said += f" Asked {_ago(now - status.answered_at)}."
    said += _listing_words(status)
    if status.downloaded:
        by_tier = ", ".join(f"{tier} {count}" for tier, count in status.tiers.most_common())
        said += f" Downloaded since start: {status.downloaded} ({by_tier})."
    if status.failed:
        said += f" {status.failed} would not download last time; each is tried again in half an hour."
    if status.cap_reached:
        said += " The cache is full, so later items wait."
    if status.network_down:
        said += " Downloads stopped: the network seems down."
    if status.showing:
        said += f" On screen: {status.showing}"
    return f'<p class="page-facts" role="status">{_state_word(kind, word)} · {html.escape(said)}</p>'


def _listing_words(status: SlideshowStatus) -> str:
    """How many items there are to show, and the sites whose posts gave none."""
    if status.outcome is not Outcome.LISTED:
        return f" Playing on from the {status.items} items already listed." if status.items else ""
    said = f" {status.items} items to show."
    if status.unresolved:
        hosts = ", ".join(f"{domain} {count}" for domain, count in status.unresolved.most_common(5))
        said += f" Posts with nothing playable, by site: {hosts}."
    return said


def _ago(seconds: float) -> str:
    if seconds < 90:  # noqa: PLR2004 -- under a minute and a half reads as seconds
        return f"{max(seconds, 0):.0f} s ago"
    return f"{seconds / 60:.0f} min ago"


def _cookie_field(state: PageState, stored: store.Stored) -> str:
    """The cookie can be written here and never read back. Its expiry is said, and warned of two weeks ahead."""
    pinned = _pinned_note(state, stored, "REDDIT_SESSION")
    if pinned:
        expiry, have = state.ui.pinned_session_expiry, True
    else:
        value = stored.values.get("REDDIT_SESSION")
        expiry, have = (session_expiry(value) if value else None), bool(value)
    said, warn = _expiry_words(have=have, expiry=expiry, now=state.now())
    # Plainly when there is none (#46): an empty field keeps a cookie only if one is kept.
    note = pinned or ("Leave empty to keep the current one." if have else _NO_COOKIE)
    disabled = " disabled" if pinned else ""
    shown = (_state_word("warn", "Expiring") + " " if warn else "") + html.escape(f"{said} {note}")
    return (
        f'<div class="field"><label for="reddit_session">{html.escape(_LABELS["REDDIT_SESSION"])} (reddit_session)</label>'
        '<input class="secret" id="reddit_session" type="password" name="reddit_session" value="" autocomplete="off"'
        f'{disabled} aria-describedby="reddit_session-hint">'
        f'<p class="hint" id="reddit_session-hint">{shown}</p></div>'
    )


def _expiry_words(*, have: bool, expiry: float | None, now: float) -> tuple[str, bool]:
    """What to say about the cookie's expiry, and whether it is a warning."""
    if not have:
        return "No cookie is set.", False
    if expiry is None:
        return "A cookie is set. It does not say when it expires.", False
    day = time.strftime("%Y-%m-%d", time.gmtime(expiry))
    if expiry <= now:
        return f"A cookie is set, and it expired on {day}. Paste a new one.", True
    if expiry - now <= EXPIRY_WARNING_S:
        days = max(1, round((expiry - now) / 86400))
        return f"A cookie is set, and it expires on {day}, in {days} day{'s' if days != 1 else ''}. Paste a new one soon.", True
    return f"A cookie is set. It expires on {day}.", False


#: How to copy the cookie, for a person who is technical but has not done it before.
_COOKIE_STEPS: Final[str] = (
    '<details class="steps"><summary>How to copy the cookie</summary><ol>'
    "<li>In a desktop browser, log in at www.reddit.com.</li>"
    "<li>Open the developer tools (F12, or ⌥⌘I on a Mac).</li>"
    "<li>Chrome or Edge: Application › Cookies › https://www.reddit.com. "
    "Firefox: Storage › Cookies. Safari: Storage › Cookies (turn on the Develop menu first).</li>"
    "<li>Find <code>reddit_session</code> and copy its Value, the long text beginning eyJ.</li>"
    "<li>Paste it above and save. Close the tab rather than logging out: logging out may end the cookie "
    "for the Player too.</li></ol></details>"
)


def _output_field(state: PageState, stored: store.Stored) -> str:
    """The HDMI connector the slideshow draws on, from those this machine has, with whether a screen is on each."""
    current = _shown(state, stored, "SLIDESHOW_OUTPUT") or DEFAULT_SLIDESHOW_OUTPUT
    reports = {report.name: report for report in state.outputs()}
    names = sorted(set(reports) | {current})
    options = "".join(
        f'<option value="{html.escape(name)}"{" selected" if name == current else ""}>'
        f"{html.escape(_output_words(name, reports.get(name)))}</option>"
        for name in names
    )
    pinned = _pinned_note(state, stored, "SLIDESHOW_OUTPUT")
    note = pinned or _ONE_SCREEN
    disabled = " disabled" if pinned else ""
    return (
        f'<div class="field"><label for="slideshow_output">{html.escape(_LABELS["SLIDESHOW_OUTPUT"])}</label>'
        f'<select id="slideshow_output" name="slideshow_output"{disabled} aria-describedby="slideshow_output-hint">'
        f'{options}</select><p class="hint" id="slideshow_output-hint">{html.escape(note)}</p></div>'
    )


#: Why the slideshow takes one screen.
_ONE_SCREEN: Final[str] = "One screen: both of a Pi's HDMI ports are on one display device, which one program drives."


#: A connector's name as the kernel's DRM names it, with the port's number.
_CONNECTOR: Final = re.compile(r"hdmi-[a-z]-(\d+)")


def screen_name(output: str) -> str:
    """An output as a person names it, by the server's rule: *HDMI 1* for `hdmi-a-1`, *Samsung Frame* for the Frame.

    Arrt's browser interface names screens the same way (its `core/outputs.js`,
    `screenName`), so the two pages never call one screen by two names. The
    connector's own name stays in the form's value, which is what is saved.
    """
    if output == FRAME_OUTPUT:
        return "Samsung Frame"
    connector = _CONNECTOR.fullmatch(output)
    return f"HDMI {connector.group(1)}" if connector else output


def _output_words(name: str, report: OutputReport | None) -> str:
    """A connector as the picker offers it: its name, then its state in the server's word, or the screen on it.

    A connector this machine does not list is `no_screen` and one with nothing
    detected on it `dark`, as the screen loop reports either of a wall's screen
    (`ScreenState`). A screen that is there is described rather than given a
    state: the page cannot see what it is showing.
    """
    shown = screen_name(name)
    if report is None:
        return f"{shown} · {DISPLAY_STATE_WORDS['no_screen']}"
    if not report.connected:
        return f"{shown} · {DISPLAY_STATE_WORDS['dark']}"
    if report.screen is not None:
        return f"{shown} · a screen at {report.screen[0]}×{report.screen[1]}"
    return f"{shown} · a screen"


#: The household's word for each state a Player can be in, `vocabulary()`'s
#: `player_states`, which the server's Players page says of this Player too.
#: Quoted, because the Player does not install the package;
#: `tests/test_settings_ui.py` holds every word to it.
PLAYER_STATE_WORDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "no_token": "No token yet",
        "awaiting_report": "Waiting for its first report",
        "reporting": "Reporting",
        "not_heard_from": "Not heard from since {date}",
        "report_unreadable": "Report unreadable",
    }
)

#: The words for the two display states the page can say of a connector,
#: `vocabulary()`'s `display_states`, quoted and held to it as the Player's are.
DISPLAY_STATE_WORDS: Final[Mapping[str, str]] = MappingProxyType({"dark": "Screen off", "no_screen": "No screen"})

#: Each Player state's glyph and colour, as the server's Players page draws it:
#: a step not yet taken in the text's colour, never a fault's.
_PLAYER_STATE_LOOK: Final[Mapping[str, str]] = MappingProxyType(
    {"no_token": "none", "awaiting_report": "waiting", "reporting": "good", "not_heard_from": "warn", "report_unreadable": "warn"}
)

#: What the page says when the server refuses this Player's token: a state the
#: server cannot put a word to, since it cannot tell which Player sent it.
NOT_ADMITTED_WORDS: Final[str] = "The server does not accept this Player's token"

#: What the page says when no report has been taken since the Player started and
#: the server has answered none as refused: the server judges it by a report
#: from before the start, which this process does not know.
NOTHING_TAKEN_WORDS: Final[str] = "No report taken since the Player started"


def player_state(heard: HeardReport) -> str | None:
    """This Player's state as the server would judge it from what it last answered, or None where it cannot be said.

    The server's rule (`player_states`' meanings) on what the Player knows of
    itself: a report refused as unreadable is `report_unreadable`; a report
    taken within the staleness threshold is `reporting`, and one taken longer
    ago `not_heard_from`; no answer yet is `awaiting_report`. Two cases have no
    state the Player can claim: a refused token, and nothing taken since it
    started while the server may hold an older report.
    """
    if heard.answer is ReportAnswer.TOKEN_REFUSED:
        return None
    if heard.answer is ReportAnswer.UNREADABLE:
        return "report_unreadable"
    if heard.accepted_age_seconds is not None:
        return "not_heard_from" if heard.accepted_age_seconds > STALE_AFTER_SECONDS else "reporting"
    if heard.answer is None:
        return "awaiting_report"
    return None


def _player_state_word(heard: HeardReport) -> str:
    """The Player's state as glyph and word: the server's word where there is one, the page's own otherwise."""
    state = player_state(heard)
    if state is None:
        words = NOT_ADMITTED_WORDS if heard.answer is ReportAnswer.TOKEN_REFUSED else NOTHING_TAKEN_WORDS
        return _state_word("warn", words)
    word = PLAYER_STATE_WORDS[state]
    if heard.accepted_at is not None:
        word = word.replace("{date}", f"{heard.accepted_at:%Y-%m-%d %H:%M %Z}".strip())
    return _state_word(_PLAYER_STATE_LOOK[state], word)


_PINNED_HINT: Final[str] = "Set in the environment or .env, which wins over this page. Remove it there to manage it here."

#: Added to `_PINNED_HINT` when the page's own file also holds the key: the one
#: precedence rule (`config._beneath_the_environment`) said where it bites.
_OVERRIDDEN: Final[str] = "This page holds {held} for it too, which is not used while .env sets it."
_PAIR_HINT: Final[str] = "Locked because the server's address and token are managed together, and one is set in .env."
_PASSWORD_HINT: Final[str] = f"At least {auth.MINIMUM_PASSWORD_LENGTH} characters."

#: Glyphs as Arrt's client draws them (its `core/glyphs.js`): one meaning each.
_GLYPHS: Final[dict[str, str]] = {"good": "\u25cf", "warn": "\u25b2", "none": "\u25cb", "waiting": "\u25cc"}


def _pinned_note(state: PageState, stored: store.Stored, key: str) -> str:
    """Why a key is read-only, when the environment pins it, and what of the page's it overrides; empty otherwise."""
    if key not in state.ui.pinned:
        return ""
    held = stored.values.get(key, "")
    if not held or (key not in store.SECRET_KEYS and held == state.ui.pinned_values.get(key)):
        return _PINNED_HINT
    shown = "a value" if key in store.SECRET_KEYS else repr(held)
    return f"{_PINNED_HINT} {_OVERRIDDEN.format(held=shown)}"


def _frame_section(state: PageState, stored: store.Stored) -> str:
    """The Samsung Frame's address, and before any save what changing it does at the set."""
    pinned = _pinned_note(state, stored, "TV_ADDRESS")
    files = bool((state.ui.environment.get("FILE_OUTPUT_DIR") or "").strip())
    hint = (
        "This Player draws to files (FILE_OUTPUT_DIR in .env), so it drives no television, and an address is refused."
        if files
        else "The set's address on this network, such as 192.0.2.20. Empty, this Player drives no Frame. "
        "Read in Arrt mode. In .env, TV_ADDRESS; its port, its token's file and its panel stay there."
    )
    return (
        f'<section class="panel"><h2>{html.escape(screen_name(FRAME_OUTPUT))}</h2>'
        + _field(
            _LABELS["TV_ADDRESS"],
            "tv_address",
            _shown(state, stored, "TV_ADDRESS"),
            locked="TV_ADDRESS" in state.ui.pinned,
            pinned=pinned,
            hint=hint,
        )
        # Only where the page can change it: a pinned address is changed in `.env`,
        # which forgets no pairing.
        + ("" if pinned else f'<p class="note" id="tv_address-pairing">{html.escape(_PAIRING_NOTE)}</p>')
        + "</section>"
    )


#: Said beside the Frame's address before any save: what a new address costs at the set.
_PAIRING_NOTE: Final[str] = (
    "Saving a new address forgets this Player's pairing with the set at the old one, and the television at the "
    "new address is asked to pair: expect a prompt on that set, even over a programme, to be accepted there. "
    "The Player knows each television by its own id, not its address. The same television at a new address keeps "
    "the pictures this Player put on it, and nothing is uploaded again. On another television the Player starts "
    "afresh: it uploads every work to it and leaves that television's own pictures alone, and the old television "
    "keeps this Player's pictures until the Player is pointed at it again or they are removed there."
)

#: Said by a save that gives the Frame a new address: what happens to the pictures, on each television.
#: The Player tells the sets apart by their own ids (`displays/frame.py`), never by the address.
_PICTURES_AT_A_NEW_ADDRESS: Final[str] = (
    "If the set at {new} is the same television, the pictures this Player put on it are kept and nothing is "
    "uploaded again. If it is another, the Player uploads every work to it and leaves that television's own pictures "
    "alone, and {old} keeps the pictures this Player put on it until the Player is pointed at it again or they are "
    "removed there."
)

#: How a save names the television an address held before the page knew one.
_AN_EARLIER_SET: Final[str] = "a television this Player drove before"


def _name_field(state: PageState, stored: store.Stored, *, local: str) -> str:
    """The Player's name: its own until a server names it, then the server's, read-only, with where to rename it."""
    pinned = _pinned_note(state, stored, "PLAYER_NAME")
    given = _server_name(state)
    if given and not pinned:
        own = f" Its own name, used whenever the server's cannot be read, is {local}." if local else ""
        return _field(
            _LABELS["PLAYER_NAME"],
            "player_name",
            given,
            locked=True,
            pinned="",
            hint="",
            locked_hint=f"{_NAMED_BY_THE_SERVER}{own}",
        )
    return _field(
        _LABELS["PLAYER_NAME"],
        "player_name",
        local,
        locked="PLAYER_NAME" in state.ui.pinned,
        pinned=pinned,
        hint="Used until a server names this Player, and whenever its name cannot be read.",
    )


#: Where a Player the server names is renamed, in the server's own words for its screen.
_NAMED_BY_THE_SERVER: Final[str] = (
    "Named by its server, whose name wins. To rename it, rename it on the server, under Settings › Players."
)

#: Under the one save, which is the whole form's (#46): it saves every section.
_SAVE_CAPTION: Final[str] = (
    "Saves every section above. The Player restarts only if what it runs on changed, and the page then says what "
    "was saved and what happens next."
)

#: Said in the cookie's field when none is kept, rather than "keep the current one" (#46).
_NO_COOKIE: Final[str] = "A slideshow cannot play without one: paste it here."


def _state_word(kind: str, word: str) -> str:
    """A state as glyph, word and colour together: never colour alone."""
    return (
        f'<span class="state state-{kind}"><span class="glyph" aria-hidden="true">{_GLYPHS[kind]}</span>'
        f"{html.escape(word)}</span>"
    )


def _field(label: str, name: str, value: str, *, locked: bool, pinned: str, hint: str, locked_hint: str | None = None) -> str:
    """A text field: read-only when locked, with why. `pinned` is the environment's note, empty when it does not pin it."""
    note = pinned or ((_PAIR_HINT if locked_hint is None else locked_hint) if locked else hint)
    disabled = " disabled" if locked else ""
    return (
        f'<div class="field"><label for="{name}">{html.escape(label)}</label>'
        f'<input id="{name}" name="{name}" value="{html.escape(value)}"{disabled} aria-describedby="{name}-hint">'
        f'<p class="hint" id="{name}-hint">{html.escape(note)}</p></div>'
    )


def _token_field(*, locked: bool, pinned: str, token_set: bool) -> str:
    """The token can be written here and never read back: the field is always empty."""
    said = "A token is set." if token_set else "No token is set."
    # "Keep the current one" only when there is one, as the cookie's field says.
    note = pinned or (_PAIR_HINT if locked else "Leave empty to keep the current one." if token_set else "")
    disabled = " disabled" if locked else ""
    return (
        f'<div class="field"><label for="client_token">{html.escape(PAIRING_LABELS["CLIENT_TOKEN"])}</label>'
        '<input class="secret" id="client_token" type="password" name="client_token" value="" autocomplete="off"'
        f'{disabled} aria-describedby="client_token-hint">'
        f'<p class="hint" id="client_token-hint">{html.escape(" ".join(part for part in (said, note) if part))} '
        f"{html.escape(_ISSUED_BY_THE_SERVER)}</p></div>"
    )


#: Where the token comes from, in the server's words, and its name in `.env`.
_ISSUED_BY_THE_SERVER: Final[str] = "The server shows it once, when this Player is added there. In .env, CLIENT_TOKEN."


def _password_input(name: str, label: str, autocomplete: str, *, hint: str | None = None) -> str:
    described = f' aria-describedby="{name}-hint"' if hint else ""
    shown_hint = f'<p class="hint" id="{name}-hint">{html.escape(hint)}</p>' if hint else ""
    return (
        f'<div class="field"><label for="{name}">{html.escape(label)}</label>'
        f'<input id="{name}" type="password" name="{name}" autocomplete="{autocomplete}" required{described}>'
        f"{shown_hint}</div>"
    )


def _captioned_act(label: str, caption: str) -> str:
    """The one filled act in its region, with a line under it saying what pressing it starts."""
    return (
        '<span class="captioned"><button class="action" type="submit" aria-describedby="act-caption">'
        f'{html.escape(label)}</button><span class="act-caption" id="act-caption">{html.escape(caption)}</span></span>'
    )


def _notes(*, problem: str | None = None, notice: str | None = None) -> str:
    """A problem in words and weight as well as colour, announced; a notice, announced politely."""
    said = ""
    if problem:
        said += f'<p class="note error" role="alert">{html.escape(problem)}</p>'
    if notice:
        said += f'<p class="note" role="status">{html.escape(notice)}</p>'
    return said


def _password_problem(password: str, again: str) -> str | None:
    if len(password) < auth.MINIMUM_PASSWORD_LENGTH:
        return f"Use at least {auth.MINIMUM_PASSWORD_LENGTH} characters."
    if password != again:
        return "The two passwords differ."
    return None


def _bad_code() -> Response:
    return _page(
        "Setup link needed",
        "<p>That setup link is not this start's. Open the link the Player printed when it last started; "
        "it carries a code that is new each time.</p>",
        status=403,
    )


def _setup_page(code: str, *, problem: str | None = None, status: int = 200) -> Response:
    return _page(
        "Set a password",
        '<p class="page-facts">This password guards this Player&#x27;s settings, its server and its token.</p>'
        + _notes(problem=problem)
        + '<form method="post" action="/setup">'
        f'<input type="hidden" name="code" value="{html.escape(code)}">'
        + _password_input("password", "Password", "new-password", hint=_PASSWORD_HINT)
        + _password_input("again", "Password again", "new-password")
        + '<button class="action" type="submit">Set password</button></form>',
        status=status,
    )


def _login_page(*, problem: str | None = None, status: int = 200) -> Response:
    return _page(
        "Log in",
        _notes(problem=problem)
        + '<form method="post" action="/login">'
        + _password_input("password", "Password", "current-password")
        + '<button class="action" type="submit">Log in</button></form>',
        status=status,
    )


def _page(title: str, body: str, *, heading: str | None = None, status: int = 200) -> Response:
    """A whole page: the product's name in the masthead, then the page's own name and body."""
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} · Arrt Player</title>"
        f'<link rel="stylesheet" href="{stylesheet_href()}"></head>'
        '<body><header class="masthead"><div class="column">'
        '<span class="brand">Arrt<span class="brand-part">Player</span></span></div></header>'
        f'<main class="column"><h1>{html.escape(heading or title)}</h1>{body}</main></body></html>'
    )
    return Response(text=document, content_type="text/html", status=status)
