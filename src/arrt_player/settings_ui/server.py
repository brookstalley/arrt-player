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
"""

import asyncio
import html
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from aiohttp.web import (
    AppKey,
    Application,
    AppRunner,
    Request,
    Response,
    StreamResponse,
    TCPSite,
    middleware,
)

from arrt_player.client import player_name
from arrt_player.config import (
    UiSettings,
    client_token_problem,
    player_name_problem,
    server_url_problem,
)
from arrt_player.settings_ui import auth, store

log = logging.getLogger(__name__)

Handler = Callable[[Request], Awaitable[StreamResponse]]

#: Sent with every response. No framing, no sniffing, no caching of a page that
#: may carry settings, and nothing loaded from anywhere but here.
SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # `same-origin`, never `no-referrer`: under that policy a browser sends
    # `Origin: null` on a form POST (Fetch, "append a request Origin header"),
    # and `_same_origin_posts` would refuse every form the page serves.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}

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
    #: The server keys missing at start: non-empty means the Player is in setup mode.
    missing: tuple[str, ...] = field(default=())

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
    response = await handler(request)
    response.headers.update(SECURITY_HEADERS)
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

    problems = _problems(locked, url=url, token=token, name=name)
    if problems:
        return _settings_page(state, stored, problem=" ".join(problems), status=400)
    values = _next_values(stored.values, locked, url=url, token=token, name=name)
    if values == dict(stored.values):
        return _settings_page(state, stored, notice="Nothing changed.")

    store.write(state.ui.cache_dir, stored.with_values(values))
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


#: How long a save waits before restarting, so its answer reaches the browser.
_RESTART_DELAY_SECONDS: Final[float] = 0.5


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
    store.write(state.ui.cache_dir, state.stored().with_auth(password=encoded, session_secret=secret))
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
    store.write(state.ui.cache_dir, state.stored().with_auth(password=encoded, session_secret=secret))
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
    locked = _locked(state)
    local = _shown(state, stored, "PLAYER_NAME")
    name = player_name(state.ui.cache_dir, local)
    token_set = "CLIENT_TOKEN" in state.ui.pinned or bool(stored.values.get("CLIENT_TOKEN"))

    if state.missing:
        status_line = (
            f"<p>Setup mode: {html.escape(', '.join(state.missing))} not set when the Player started, "
            "so no walls are shown. Set the server and its token below.</p>"
        )
    else:
        status_line = "<p>Running, and pulling from its server.</p>"
    if name != local:
        status_line += (
            f"<p>The server calls this Player <strong>{html.escape(name)}</strong>; that name wins over the one below.</p>"
        )

    body = (
        status_line
        + _problem(problem)
        + (f'<p role="status">{html.escape(notice)}</p>' if notice else "")
        + '<form method="post" action="/settings"><h2>Server</h2>'
        + _field(
            "Server address",
            "server_url",
            _shown(state, stored, "SERVER_URL"),
            locked="SERVER_URL" in locked,
            pinned="SERVER_URL" in state.ui.pinned,
            hint="For example http://arrt.local:8770. Changing it clears the token unless you enter one with it.",
        )
        + _token_field(locked="CLIENT_TOKEN" in locked, pinned="CLIENT_TOKEN" in state.ui.pinned, token_set=token_set)
        + "<h2>This Player</h2>"
        + _field(
            "Name",
            "player_name",
            local,
            locked="PLAYER_NAME" in locked,
            pinned="PLAYER_NAME" in state.ui.pinned,
            hint="Used until a server names this Player, and whenever its name cannot be read.",
        )
        + '<button type="submit">Save and restart</button></form>'
        + '<form method="post" action="/password"><h2>Password</h2>'
        '<label>Current password <input type="password" name="current" autocomplete="current-password" required></label>'
        '<label>New password <input type="password" name="password" autocomplete="new-password" required></label>'
        '<label>Again <input type="password" name="again" autocomplete="new-password" required></label>'
        '<button type="submit">Change password</button></form>'
        '<form method="post" action="/logout"><button type="submit">Log out</button></form>'
    )
    title = f"Settings · {name}" if name else "Settings"
    return _page(title, body, status=status)


_PINNED_HINT: Final[str] = "Set in the environment or .env, which wins over this page. Remove it there to manage it here."
_PAIR_HINT: Final[str] = "Locked because the server's address and token are managed together, and one is set in .env."


def _field(label: str, name: str, value: str, *, locked: bool, pinned: bool, hint: str) -> str:
    note = _PINNED_HINT if pinned else _PAIR_HINT if locked else hint
    disabled = " disabled" if locked else ""
    return (
        f'<label>{html.escape(label)} <input name="{name}" value="{html.escape(value)}"{disabled}></label>'
        f'<p class="hint">{html.escape(note)}</p>'
    )


def _token_field(*, locked: bool, pinned: bool, token_set: bool) -> str:
    """The token can be written here and never read back: the field is always empty."""
    state = "A token is set." if token_set else "No token is set."
    note = _PINNED_HINT if pinned else _PAIR_HINT if locked else "Leave empty to keep the current one."
    disabled = " disabled" if locked else ""
    return (
        f'<label>Client token <input type="password" name="client_token" value="" autocomplete="off"{disabled}></label>'
        f'<p class="hint">{html.escape(state)} {html.escape(note)}</p>'
    )


def _password_problem(password: str, again: str) -> str | None:
    if len(password) < auth.MINIMUM_PASSWORD_LENGTH:
        return f"Use at least {auth.MINIMUM_PASSWORD_LENGTH} characters."
    if password != again:
        return "The two passwords differ."
    return None


def _bad_code() -> Response:
    return _page(
        "Setup code needed",
        "<p>That setup link is not this start's. Open the link the Player printed when it last started.</p>",
        status=403,
    )


def _setup_page(code: str, *, problem: str | None = None, status: int = 200) -> Response:
    return _page(
        "Set a password",
        _problem(problem) + '<form method="post" action="/setup">'
        f'<input type="hidden" name="code" value="{html.escape(code)}">'
        '<label>Password <input type="password" name="password" autocomplete="new-password" required></label>'
        '<label>Again <input type="password" name="again" autocomplete="new-password" required></label>'
        '<button type="submit">Set password</button></form>',
        status=status,
    )


def _login_page(*, problem: str | None = None, status: int = 200) -> Response:
    return _page(
        "Log in",
        _problem(problem) + '<form method="post" action="/login">'
        '<label>Password <input type="password" name="password" autocomplete="current-password" required></label>'
        '<button type="submit">Log in</button></form>',
        status=status,
    )


def _problem(problem: str | None) -> str:
    return f'<p role="alert" class="problem">{html.escape(problem)}</p>' if problem else ""


_STYLE: Final[str] = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:2rem auto;padding:0 1rem;"
    "color:#1a1a1a;background:#fafafa}"
    "@media (prefers-color-scheme:dark){body{color:#eee;background:#161616}}"
    "label{display:block;margin:.75rem 0}input{display:block;width:100%;font:inherit;padding:.4rem}"
    "button{font:inherit;padding:.4rem 1rem;margin-top:.5rem}.problem{color:#b00020}"
    ".hint{font-size:.875rem;opacity:.75;margin:-.5rem 0 .75rem}h2{font-size:1.1rem;margin-top:2rem}"
    "input:disabled{opacity:.6}"
)


def _page(title: str, body: str, *, status: int = 200) -> Response:
    escaped = html.escape(title)
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escaped} · Arrt Player</title><style>{_STYLE}</style></head>"
        f"<body><h1>{escaped}</h1>{body}</body></html>"
    )
    return Response(text=document, content_type="text/html", status=status)
