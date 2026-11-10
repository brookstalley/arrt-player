"""The settings page: a first-run password, a login, and the settings themselves.

**The one module in the Player that listens on a socket.** It takes only
`aiohttp.web` from aiohttp, and never a client: the plane-isolation guard allows
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
passwords are throttled; and nothing secret is ever written into a page, a log
line or a header. It is plain HTTP, as the Arrt server is: the password crosses
the LAN in the clear, which is the trust boundary Arrt's `security-model.md`
already accepts.

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
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
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

from arrt_player.client import OutputReport, hdmi_outputs, player_name
from arrt_player.config import (
    ARRT_MODE,
    DEFAULT_SLIDESHOW_MAX_SECONDS,
    DEFAULT_SLIDESHOW_MIN_SECONDS,
    DEFAULT_SLIDESHOW_OUTPUT,
    SLIDESHOW_MODE,
    UiSettings,
    client_token_problem,
    dwell_problem,
    player_mode_problem,
    player_name_problem,
    server_url_problem,
    slideshow_output_problem,
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

    def stored(self) -> store.Stored:
        return store.read(self.ui.cache_dir)


STATE: Final = AppKey("state", PageState)


def build(state: PageState) -> Application:
    """The page's application, ready for a runner or a test client."""
    app = Application(middlewares=[_headers, _same_origin_posts])
    app[STATE] = state
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
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = STATIC_CACHE_CONTROL
        kind = STATIC_TYPES.get(Path(request.path).suffix)
        if kind is not None:
            response.headers["Content-Type"] = kind
    return response


@middleware
async def _same_origin_posts(request: Request, handler: Handler) -> StreamResponse:
    """Refuse a POST whose `Origin` is not this page. A browser sends one on every form POST."""
    if request.method == "POST" and request.headers.get("Origin") != f"{request.scheme}://{request.host}":
        return _page("Refused", "<p>This form must be submitted from the settings page itself.</p>", status=403)
    return await handler(request)


# -- pages -----------------------------------------------------------------------


def _state(request: Request) -> PageState:
    return request.app[STATE]


def _signed_in(request: Request, stored: store.Stored) -> bool:
    return auth.session_valid(request.cookies.get(auth.COOKIE_NAME), stored.session_secret, now=_state(request).now())


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


def _locked(state: PageState) -> frozenset[str]:
    """The keys the page may not change: those the environment pins, and the pair with either."""
    pinned = state.ui.pinned
    return pinned | _SERVER_PAIR if pinned & _SERVER_PAIR else pinned


def _shown(state: PageState, stored: store.Stored, name: str) -> str:
    """A key's value as the Player will use it: the environment's when pinned, else the page's."""
    return state.ui.pinned_values.get(name, "") if name in state.ui.pinned else stored.values.get(name, "")


async def _save(request: Request) -> Response:
    """Save the form, then restart onto it. Nothing changed means nothing written and no restart."""
    state = _state(request)
    stored = state.stored()
    if stored.password is None or not _signed_in(request, stored):
        return _redirect("/login")
    form = await request.post()
    locked = _locked(state)
    url = str(form.get("server_url", "")).strip().rstrip("/")
    token = str(form.get("client_token", "")).strip()
    name = str(form.get("player_name", "")).strip()

    entries = _slideshow_entries(form, locked)
    session = "" if "REDDIT_SESSION" in locked else clean_session(str(form.get("reddit_session", "")))

    problems = _problems(locked, url=url, token=token, name=name)
    problems += _slideshow_problems(entries, session, lambda key: _shown(state, stored, key))
    if problems:
        return _settings_page(state, stored, problem=" ".join(problems), status=400)
    values = _next_values(stored.values, locked, url=url, token=token, name=name)
    for key, value in entries.items():
        _put(values, key, value)
    if session:
        values["REDDIT_SESSION"] = session
    if values == dict(stored.values):
        return _settings_page(state, stored, notice="Nothing changed.")

    if not _wrote(state, stored.with_values(values)):
        return _settings_page(state, stored, problem=_WRITE_FAILED, status=500)
    log.info(
        "the settings page saved new settings; restarting onto them",
        extra={"event": "settings_page.saved", "changed": sorted(_changed(stored.values, values))},
    )
    # After the answer has gone out, so the browser sees the page say so.
    asyncio.get_running_loop().call_later(_RESTART_DELAY_SECONDS, state.restart)
    return _page(
        "Saved",
        "<p>Saved. The Player is restarting onto the new settings; under systemd it is back in a few seconds. "
        'Started by hand, start it again.</p><p><a href="/">Back to the settings</a></p>',
    )


def _problems(locked: frozenset[str], *, url: str, token: str, name: str) -> list[str]:
    """What is wrong with the form, judged as `config.load` judges the same values. A locked key is not read."""
    found = []
    if "SERVER_URL" not in locked:
        if url and (said := server_url_problem(url)):
            found.append(said)
        if token and (said := client_token_problem(token)):
            found.append(said)
        if token and not url:
            found.append("A token belongs to a server: set the server's address with it.")
    if "PLAYER_NAME" not in locked and (said := player_name_problem(name)):
        found.append(said)
    return found


def _next_values(before: Mapping[str, str], locked: frozenset[str], *, url: str, token: str, name: str) -> dict[str, str]:
    """The page's values after the form. A new server never inherits the old one's token."""
    values = dict(before)
    if "SERVER_URL" not in locked:
        if url != before.get("SERVER_URL", "") or not url:
            values.pop("CLIENT_TOKEN", None)
        _put(values, "SERVER_URL", url)
        if token and url:
            values["CLIENT_TOKEN"] = token
    if "PLAYER_NAME" not in locked:
        _put(values, "PLAYER_NAME", name)
    return values


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
    wait = state.throttle.wait_seconds()
    if wait > 0:
        return _settings_page(state, stored, problem=f"Too many wrong passwords. Try again in {int(wait) + 1} s.", status=429)
    state.throttle.failed()
    if not await asyncio.to_thread(auth.password_matches, current, stored.password):
        log.warning("a wrong current password was offered", extra={"event": "settings_page.password_refused"})
        return _settings_page(state, stored, problem="The current password is not right.", status=403)
    state.throttle.succeeded()
    problem = _password_problem(password, again)
    if problem:
        return _settings_page(state, stored, problem=problem, status=400)
    secret = auth.new_session_secret()
    encoded = await asyncio.to_thread(auth.hash_password, password)
    if not _wrote(state, state.stored().with_auth(password=encoded, session_secret=secret)):
        return _settings_page(state, stored, problem=_WRITE_FAILED, status=500)
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
    wait = state.throttle.wait_seconds()
    if wait > 0:
        return _login_page(problem=f"Too many wrong passwords. Try again in {int(wait) + 1} s.", status=429)
    state.throttle.failed()
    # scrypt is tenths of a second and 16 MiB on a Pi, so it runs on a thread
    # rather than on the loop that drives the walls.
    offered = str(form.get("password", ""))
    matched = await asyncio.to_thread(auth.password_matches, offered, stored.password)
    if stored.session_secret is None or not matched:
        log.warning("a wrong password was offered to the settings page", extra={"event": "settings_page.login_refused"})
        return _login_page(problem="That is not the password.", status=403)
    state.throttle.succeeded()
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
    """The page's own: the Player's name, a line of facts, its toolbar, then Server, This Player and Password."""
    locked = _locked(state)
    local = _shown(state, stored, "PLAYER_NAME")
    name = player_name(state.ui.cache_dir, local)
    token_set = "CLIENT_TOKEN" in state.ui.pinned or bool(stored.values.get("CLIENT_TOKEN"))

    if state.missing:
        shown = "nothing is shown" if state.mode == SLIDESHOW_MODE else "no walls are shown"
        facts = (
            _state_word("warn", "Setup mode")
            + f" · {html.escape(', '.join(state.missing))} not set when the Player started, so {shown}."
        )
    elif state.slideshow is not None:
        facts = _state_word("good", "Running") + " · a Reddit slideshow"
    else:
        facts = _state_word("good", "Running") + " · pulling from " + html.escape(_shown(state, stored, "SERVER_URL"))
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
            "Address",
            "server_url",
            _shown(state, stored, "SERVER_URL"),
            locked="SERVER_URL" in locked,
            pinned="SERVER_URL" in state.ui.pinned,
            hint="For example http://arrt.local:8770. Changing it clears the token unless you enter one with it.",
        )
        + _token_field(locked="CLIENT_TOKEN" in locked, pinned="CLIENT_TOKEN" in state.ui.pinned, token_set=token_set)
        + "</section>"
        + _slideshow_section(state, stored)
        + '<section class="panel"><h2>This Player</h2>'
        + _field(
            "Name",
            "player_name",
            local,
            locked="PLAYER_NAME" in locked,
            pinned="PLAYER_NAME" in state.ui.pinned,
            hint="Used until a server names this Player, and whenever its name cannot be read.",
        )
        + _captioned_act("Save and restart", "Restarts the Player onto what is saved; it is back in a few seconds.")
        + '</section></form><section class="panel"><h2>Password</h2><form method="post" action="/password">'
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
    pinned = "PLAYER_MODE" in state.ui.pinned
    disabled = " disabled" if pinned else ""
    choices = "".join(
        f'<label class="choice"><input type="radio" name="player_mode" value="{value}"'
        f'{" checked" if value == current else ""}{disabled}>{html.escape(words)}</label>'
        for value, words in ((ARRT_MODE, "Walls from an Arrt server"), (SLIDESHOW_MODE, "A Reddit slideshow"))
    )
    note = _PINNED_HINT if pinned else "The Player runs one or the other. Saving restarts it in the mode chosen."
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
            "Listing address",
            "slideshow_url",
            _shown(state, stored, "SLIDESHOW_URL"),
            locked="SLIDESHOW_URL" in pinned,
            pinned="SLIDESHOW_URL" in pinned,
            hint="A subreddit or multireddit as your browser shows it, such as "
            "https://www.reddit.com/r/EarthPorn/top/?t=month. Shown in its order, looped.",
        )
        + _cookie_field(state, stored)
        + _COOKIE_STEPS
        + _field(
            "Shortest time on screen, in seconds",
            "slideshow_min_seconds",
            _shown(state, stored, "SLIDESHOW_MIN_SECONDS"),
            locked="SLIDESHOW_MIN_SECONDS" in pinned,
            pinned="SLIDESHOW_MIN_SECONDS" in pinned,
            hint=f"A still shows this long; a short clip plays again until it has. Empty is {DEFAULT_SLIDESHOW_MIN_SECONDS:g}.",
        )
        + _field(
            "Longest time on screen, in seconds",
            "slideshow_max_seconds",
            _shown(state, stored, "SLIDESHOW_MAX_SECONDS"),
            locked="SLIDESHOW_MAX_SECONDS" in pinned,
            pinned="SLIDESHOW_MAX_SECONDS" in pinned,
            hint=f"A long clip is cut here. Empty is {DEFAULT_SLIDESHOW_MAX_SECONDS:g}.",
        )
        + _output_field(state, stored)
        + "</section>"
    )


def _slideshow_status(status: SlideshowStatus | None, *, now: float) -> str:
    """What Reddit last answered and what the cache holds, in words; nothing outside a running slideshow."""
    if status is None:
        return ""
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
    pinned = "REDDIT_SESSION" in state.ui.pinned
    if pinned:
        expiry, have = state.ui.pinned_session_expiry, True
    else:
        value = stored.values.get("REDDIT_SESSION")
        expiry, have = (session_expiry(value) if value else None), bool(value)
    said, warn = _expiry_words(have=have, expiry=expiry, now=state.now())
    note = _PINNED_HINT if pinned else "Leave empty to keep the current one."
    disabled = " disabled" if pinned else ""
    shown = (_state_word("warn", "Expiring") + " " if warn else "") + html.escape(f"{said} {note}")
    return (
        '<div class="field"><label for="reddit_session">Reddit cookie (reddit_session)</label>'
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
    pinned = "SLIDESHOW_OUTPUT" in state.ui.pinned
    note = _PINNED_HINT if pinned else _ONE_SCREEN
    disabled = " disabled" if pinned else ""
    return (
        '<div class="field"><label for="slideshow_output">Screen</label>'
        f'<select id="slideshow_output" name="slideshow_output"{disabled} aria-describedby="slideshow_output-hint">'
        f'{options}</select><p class="hint" id="slideshow_output-hint">{html.escape(note)}</p></div>'
    )


#: Why the slideshow takes one screen.
_ONE_SCREEN: Final[str] = "One screen: both of a Pi's HDMI ports are on one display device, which one program drives."


def _output_words(name: str, report: OutputReport | None) -> str:
    if report is None:
        return f"{name} (not found on this machine)"
    if not report.connected:
        return f"{name} (no screen connected)"
    if report.screen is not None:
        return f"{name} (a screen at {report.screen[0]}×{report.screen[1]})"
    return f"{name} (a screen)"


_PINNED_HINT: Final[str] = "Set in the environment or .env, which wins over this page. Remove it there to manage it here."
_PAIR_HINT: Final[str] = "Locked because the server's address and token are managed together, and one is set in .env."
_PASSWORD_HINT: Final[str] = f"At least {auth.MINIMUM_PASSWORD_LENGTH} characters."

#: Glyphs as Arrt's client draws them (its `core/glyphs.js`): one meaning each.
_GLYPHS: Final[dict[str, str]] = {"good": "\u25cf", "warn": "\u25b2"}


def _state_word(kind: str, word: str) -> str:
    """A state as glyph, word and colour together: never colour alone."""
    return (
        f'<span class="state state-{kind}"><span class="glyph" aria-hidden="true">{_GLYPHS[kind]}</span>'
        f"{html.escape(word)}</span>"
    )


def _field(label: str, name: str, value: str, *, locked: bool, pinned: bool, hint: str) -> str:
    note = _PINNED_HINT if pinned else _PAIR_HINT if locked else hint
    disabled = " disabled" if locked else ""
    return (
        f'<div class="field"><label for="{name}">{html.escape(label)}</label>'
        f'<input id="{name}" name="{name}" value="{html.escape(value)}"{disabled} aria-describedby="{name}-hint">'
        f'<p class="hint" id="{name}-hint">{html.escape(note)}</p></div>'
    )


def _token_field(*, locked: bool, pinned: bool, token_set: bool) -> str:
    """The token can be written here and never read back: the field is always empty."""
    said = "A token is set." if token_set else "No token is set."
    note = _PINNED_HINT if pinned else _PAIR_HINT if locked else "Leave empty to keep the current one."
    disabled = " disabled" if locked else ""
    return (
        '<div class="field"><label for="client_token">Client token</label>'
        '<input class="secret" id="client_token" type="password" name="client_token" value="" autocomplete="off"'
        f'{disabled} aria-describedby="client_token-hint">'
        f'<p class="hint" id="client_token-hint">{html.escape(said)} {html.escape(note)}</p></div>'
    )


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
