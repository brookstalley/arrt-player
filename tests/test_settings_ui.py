"""The settings page: its file, its password and cookie, its pages, and setup mode.

The page sits on the LAN and guards the client token, so most of what is tested
here is what it refuses: a setup without the code, a code used twice, a POST
from another origin, a cookie signed under another secret, a run of wrong
passwords. And what it never says: no password, hash, secret or cookie reaches a
log record.
"""

import asyncio
import base64
import html
import json
import logging
import os
import signal
import socket
import stat
from collections import Counter
from pathlib import Path
from typing import ClassVar

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from arrt_player import __main__ as entry
from arrt_player.client import OutputReport, player_name
from arrt_player.config import CLIENT_DOCUMENT_FILENAME, ConfigError, SetupRequired, UiSettings
from arrt_player.programmes.slideshow_status import SlideshowStatus
from arrt_player.settings_ui import auth, store
from arrt_player.settings_ui import server as server_module
from arrt_player.settings_ui.server import SECURITY_HEADERS, PageState, build
from arrt_player.sources import Outcome

CODE = "the-setup-code"
PASSWORD = "correct horse battery"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def loopback(cache_dir: Path, port: int = 0) -> UiSettings:
    return UiSettings(cache_dir=cache_dir, bind="127.0.0.1", port=port, pinned=frozenset())


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# -- the file ------------------------------------------------------------------------


class TestTheFile:
    def test_what_is_written_is_what_is_read(self, cache_dir: Path):
        written = store.Stored(values={"SERVER_URL": "http://arrt.example"}, password="scrypt$x", session_secret="ab")

        store.write(cache_dir, written)

        assert store.read(cache_dir) == written

    def test_no_file_is_nothing_set(self, cache_dir: Path):
        assert store.read(cache_dir) == store.Stored()

    def test_it_is_readable_by_its_owner_only(self, cache_dir: Path):
        """It holds the client token."""
        store.write(cache_dir, store.Stored(values={"CLIENT_TOKEN": "secret"}))

        assert stat.S_IMODE(store.path_in(cache_dir).stat().st_mode) == store.FILE_MODE

    def test_a_rewrite_leaves_no_temporary_file(self, cache_dir: Path):
        store.write(cache_dir, store.Stored())
        store.write(cache_dir, store.Stored(values={"PLAYER_NAME": "hall"}))

        assert sorted(path.name for path in cache_dir.iterdir()) == [store.SETTINGS_FILENAME]

    @pytest.mark.parametrize(
        ("content", "named"),
        [
            ("not json", "not JSON"),
            ("[]", "object"),
            ('{"version": 3}', "version 3"),
            ('{"version": true}', "version True"),
            ('{"version": 1, "values": {"CACHE_DIR": "/elsewhere"}}', "CACHE_DIR"),
            ('{"version": 1, "values": {"SERVER_URL": 8770}}', "not text"),
            ('{"version": 1, "auth": {"password": 1}}', "not text"),
        ],
    )
    def test_a_file_it_cannot_trust_is_refused_with_the_reason(self, cache_dir: Path, content: str, named: str):
        cache_dir.mkdir(parents=True, exist_ok=True)
        store.path_in(cache_dir).write_text(content, encoding="utf-8")

        with pytest.raises(store.SettingsUnreadable, match=named):
            store.read(cache_dir)

    def test_its_credentials_are_in_no_repr(self):
        shown = repr(store.Stored(password="scrypt$the-hash", session_secret="the-secret"))

        assert "the-hash" not in shown
        assert "the-secret" not in shown


# -- password, cookie, throttle --------------------------------------------------------


class TestThePassword:
    def test_the_right_password_matches_and_a_wrong_one_does_not(self):
        encoded = auth.hash_password(PASSWORD)

        assert auth.password_matches(PASSWORD, encoded)
        assert not auth.password_matches(PASSWORD + "!", encoded)

    def test_it_is_stored_as_a_salted_hash(self):
        first, second = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)

        assert PASSWORD not in first
        assert first != second, "two hashes of one password were equal, so there is no salt"

    @pytest.mark.parametrize("encoded", ["", "plain", "bcrypt$1$2$3$aa$bb", "scrypt$x$8$1$aa$bb", "scrypt$16384$8$1$zz$bb"])
    def test_a_malformed_encoding_matches_nothing(self, encoded: str):
        assert not auth.password_matches(PASSWORD, encoded)


class TestTheCookie:
    SECRET = "00" * 32
    OTHER = "11" * 32

    def test_a_fresh_cookie_is_valid(self):
        assert auth.session_valid(auth.session_cookie(self.SECRET, now=1000), self.SECRET, now=1001)

    def test_it_expires(self):
        cookie = auth.session_cookie(self.SECRET, now=1000)

        assert auth.session_valid(cookie, self.SECRET, now=1000 + auth.SESSION_SECONDS - 1)
        assert not auth.session_valid(cookie, self.SECRET, now=1000 + auth.SESSION_SECONDS)

    def test_one_signed_under_another_secret_is_refused(self):
        """Which is how changing the password ends every session."""
        assert not auth.session_valid(auth.session_cookie(self.OTHER, now=1000), self.SECRET, now=1001)

    def test_an_extended_expiry_is_refused(self):
        expiry, _, signature = auth.session_cookie(self.SECRET, now=1000).partition(".")

        assert not auth.session_valid(f"{int(expiry) + 10**6}.{signature}", self.SECRET, now=1001)

    @pytest.mark.parametrize("cookie", [None, "", "nonsense", "12.", ".abc"])
    def test_garbage_is_refused(self, cookie: str | None):
        assert not auth.session_valid(cookie, self.SECRET, now=1001)

    def test_no_secret_means_no_session(self):
        assert not auth.session_valid(auth.session_cookie(self.SECRET, now=1000), None, now=1001)


class TestTheThrottle:
    def test_each_failure_doubles_the_wait_up_to_a_minute_and_success_clears_it(self):
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        waits = []
        for _ in range(9):
            throttle.failed()
            waits.append(throttle.wait_seconds())

        assert waits == [1, 2, 4, 8, 16, 32, 60, 60, 60]
        clock.now += 61
        assert throttle.wait_seconds() == 0
        throttle.succeeded()
        throttle.failed()
        assert throttle.wait_seconds() == 1


# -- the pages -------------------------------------------------------------------------


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def state(cache_dir: Path, clock: Clock) -> PageState:
    return PageState(
        ui=loopback(cache_dir),
        setup_code=CODE,
        now=clock,
        throttle=auth.Throttle(monotonic=clock),
        missing=("SERVER_URL", "CLIENT_TOKEN"),
    )


@pytest.fixture
async def page(state: PageState):
    client = TestClient(TestServer(build(state)))
    await client.start_server()
    yield client
    await client.close()


def origin(client: TestClient) -> dict[str, str]:
    return {"Origin": str(client.make_url("")).rstrip("/")}


async def post(client: TestClient, path: str, data: dict[str, str], **headers: str):
    return await client.post(path, data=data, headers={**origin(client), **headers}, allow_redirects=False)


async def set_up(client: TestClient) -> None:
    response = await post(client, "/setup", {"code": CODE, "password": PASSWORD, "again": PASSWORD})
    assert response.status == 303


class TestTheFirstRun:
    async def test_without_a_password_the_page_sends_you_to_the_printed_link(self, page: TestClient):
        response = await page.get("/")

        assert response.status == 200
        assert "printed when it started" in await response.text()

    @pytest.mark.parametrize("query", ["", "?code=", "?code=guess"])
    async def test_setup_needs_this_starts_code(self, page: TestClient, query: str):
        assert (await page.get(f"/setup{query}")).status == 403

    async def test_the_right_code_offers_the_form(self, page: TestClient):
        response = await page.get(f"/setup?code={CODE}")

        assert response.status == 200
        assert 'name="password"' in await response.text()

    async def test_a_wrong_code_sets_nothing(self, page: TestClient, cache_dir: Path):
        response = await post(page, "/setup", {"code": "guess", "password": PASSWORD, "again": PASSWORD})

        assert response.status == 403
        assert store.read(cache_dir).password is None

    @pytest.mark.parametrize(
        ("password", "again", "said"),
        [("short", "short", "at least"), (PASSWORD, PASSWORD + "x", "differ")],
    )
    async def test_an_unusable_password_is_refused_with_the_reason(self, page, cache_dir, password, again, said):
        response = await post(page, "/setup", {"code": CODE, "password": password, "again": again})

        assert response.status == 400
        assert said in await response.text()
        assert store.read(cache_dir).password is None

    @pytest.mark.parametrize(("password", "status"), [("7chars!", 400), ("8chars!!", 303)])
    async def test_the_floor_is_eight_characters(self, page, password, status):
        """Owner, 2026-11-10: eight, no more."""
        response = await post(page, "/setup", {"code": CODE, "password": password, "again": password})

        assert response.status == status

    async def test_setting_a_password_signs_you_in_and_spends_the_code(self, page, cache_dir, state):
        response = await post(page, "/setup", {"code": CODE, "password": PASSWORD, "again": PASSWORD})

        assert response.status == 303
        stored = store.read(cache_dir)
        assert auth.password_matches(PASSWORD, stored.password)
        assert state.setup_code is None
        assert (await page.get("/", allow_redirects=False)).status == 200, "the new password did not sign its setter in"

        again = await post(page, "/setup", {"code": CODE, "password": "a different one", "again": "a different one"})
        assert again.status == 303
        assert again.headers["Location"] == "/login"
        assert auth.password_matches(PASSWORD, store.read(cache_dir).password), "a second setup replaced the password"

    async def test_the_settings_already_on_file_survive_the_setup(self, page, cache_dir):
        store.write(cache_dir, store.Stored(values={"PLAYER_NAME": "hall"}))

        await set_up(page)

        assert store.read(cache_dir).values == {"PLAYER_NAME": "hall"}


class TestSigningIn:
    async def test_a_page_needs_a_session(self, page):
        await set_up(page)
        page.session.cookie_jar.clear()

        response = await page.get("/", allow_redirects=False)

        assert response.status == 303
        assert response.headers["Location"] == "/login"

    async def test_signed_in_the_page_says_it_is_in_setup_mode_and_what_is_missing(self, page):
        await set_up(page)

        text = await (await page.get("/")).text()

        assert "Setup mode" in text
        assert "SERVER_URL, CLIENT_TOKEN" in text

    async def test_the_right_password_signs_in_and_logging_out_signs_out(self, page):
        await set_up(page)
        page.session.cookie_jar.clear()

        assert (await post(page, "/login", {"password": PASSWORD})).status == 303
        assert (await page.get("/", allow_redirects=False)).status == 200
        assert (await post(page, "/logout", {})).status == 303
        assert (await page.get("/", allow_redirects=False)).status == 303

    async def test_wrong_passwords_are_throttled_and_the_right_one_waits_too(self, page, clock):
        await set_up(page)
        page.session.cookie_jar.clear()

        assert (await post(page, "/login", {"password": "wrong"})).status == 403
        throttled = await post(page, "/login", {"password": PASSWORD})
        assert throttled.status == 429, "a throttled login was heard, so the throttle throttles nothing"

        clock.now += 1.5
        assert (await post(page, "/login", {"password": PASSWORD})).status == 303

    async def test_concurrent_wrong_passwords_share_one_window(self, page):
        """Bodies sent together must not all pass the check before any is counted."""
        await set_up(page)
        page.session.cookie_jar.clear()

        answers = await asyncio.gather(*(post(page, "/login", {"password": f"wrong {n}"}) for n in range(4)))

        assert sorted(answer.status for answer in answers) == [403, 429, 429, 429]

    async def test_a_cookie_signed_under_another_secret_is_refused(self, page, cache_dir, clock):
        await set_up(page)
        page.session.cookie_jar.clear()
        forged = auth.session_cookie("22" * 32, now=clock())

        response = await page.get("/", cookies={auth.COOKIE_NAME: forged}, allow_redirects=False)

        assert response.status == 303

    async def test_the_cookie_is_http_only_and_strictly_same_site(self, page):
        response = await post(page, "/setup", {"code": CODE, "password": PASSWORD, "again": PASSWORD})

        cookie = response.cookies[auth.COOKIE_NAME]
        assert cookie["httponly"]
        assert cookie["samesite"] == "Strict"


class TestWhatThePageRefuses:
    @pytest.mark.parametrize("headers", [{"Origin": "http://elsewhere.example"}, {"Origin": "null"}])
    async def test_a_post_from_another_origin_is_refused_and_does_nothing(self, page, cache_dir, headers):
        response = await page.post(
            "/setup", data={"code": CODE, "password": PASSWORD, "again": PASSWORD}, headers=headers, allow_redirects=False
        )

        assert response.status == 403
        assert store.read(cache_dir).password is None

    async def test_a_post_with_no_origin_is_refused(self, page, cache_dir):
        response = await page.post("/setup", data={"code": CODE, "password": PASSWORD, "again": PASSWORD})

        assert response.status == 403
        assert store.read(cache_dir).password is None

    async def test_the_pages_referrer_policy_lets_a_browser_send_its_origin(self, page):
        """Under `no-referrer` a browser sends `Origin: null` on a form POST (Fetch, "append a request
        Origin header"), and the origin check would refuse every form the page serves. Only these
        policies leave a same-origin POST its real Origin."""
        policy = (await page.get("/login")).headers["Referrer-Policy"]

        assert policy in {"same-origin", "strict-origin", "strict-origin-when-cross-origin", "origin"}

    @pytest.mark.parametrize("path", ["/", f"/setup?code={CODE}", "/setup", "/login"])
    async def test_every_answer_carries_the_security_headers(self, page, path):
        response = await page.get(path, allow_redirects=False)

        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, f"{path} answered without {name}"


async def test_no_credential_reaches_a_log_record(page, cache_dir, caplog):
    """Driven through setup, a refused login, a login and a logout, with every logger at DEBUG."""
    caplog.set_level(logging.DEBUG)
    await set_up(page)
    page.session.cookie_jar.clear()
    await post(page, "/login", {"password": "a wrong one"})
    await asyncio.sleep(0)
    stored = store.read(cache_dir)
    secrets = [PASSWORD, "a wrong one", stored.password, stored.session_secret]

    rendered = [str(vars(record)) for record in caplog.records]
    assert rendered, "nothing was logged, so this checks nothing"
    leaked = [secret for secret in secrets if any(secret in line for line in rendered)]
    assert leaked == []


# -- setup mode at the entry point -------------------------------------------------------


@pytest.fixture
def _quiet_logging(monkeypatch):
    monkeypatch.setattr(entry.logs, "configure", lambda: None)


async def test_without_a_server_the_player_serves_only_its_page_and_says_where(monkeypatch, cache_dir, capsys):
    """Owner, 2026-11-10: setup mode, and a printed line naming where to go, with the code it needs."""
    port = free_port()
    monkeypatch.setattr(entry, "load_ui", lambda: loopback(cache_dir, port))

    def no_server():
        raise SetupRequired(("SERVER_URL", "CLIENT_TOKEN"))

    monkeypatch.setattr(entry, "load", no_server)
    monkeypatch.setattr(entry, "Supervisor", _refuse("setup mode started the supervisor"))

    running = asyncio.create_task(entry._run())
    printed = await _printed(capsys)

    assert printed.startswith(f"go to http://127.0.0.1:{port}/setup?code=")
    assert printed.endswith(" to configure")
    async with TestClientFor(port) as client:
        code = printed.split("code=", 1)[1].split()[0]
        assert (await client.get(f"/setup?code={code}")).status == 200

    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(running, timeout=5) == 0


async def test_once_a_password_is_set_the_printed_line_carries_no_code(cache_dir):
    store.write(cache_dir, store.Stored(password=auth.hash_password(PASSWORD), session_secret="00" * 32))

    state = entry.settings_page(loopback(cache_dir, 8771))

    assert state.setup_code is None
    assert entry.where_to_go(state) == "go to http://127.0.0.1:8771/ to configure"


@pytest.mark.parametrize(("bind", "expected"), [("127.0.0.1", "http://127.0.0.1:8771/"), ("::1", "http://[::1]:8771/")])
def test_the_printed_address_is_one_a_browser_can_parse(cache_dir, bind, expected):
    ui = UiSettings(cache_dir=cache_dir, bind=bind, port=8771, pinned=frozenset())

    assert entry.server.home_url(ui) == expected


async def test_a_page_that_cannot_listen_says_so_and_returns(cache_dir, caplog):
    """Beside the walls this costs the page and nothing else: the caller carries on."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]

        served = await entry.run_settings_page(entry.settings_page(loopback(cache_dir, port)), asyncio.Event())

    assert served is False
    assert any(getattr(record, "event", None) == "settings_page.unavailable" for record in caplog.records)


async def test_setup_mode_that_cannot_listen_exits_non_zero(monkeypatch, cache_dir, capsys):
    """With nothing else to do, a page that cannot listen leaves systemd to try again."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        monkeypatch.setattr(entry, "load_ui", lambda: loopback(cache_dir, taken.getsockname()[1]))

        def no_server():
            raise SetupRequired(("SERVER_URL",))

        monkeypatch.setattr(entry, "load", no_server)

        assert await asyncio.wait_for(entry._run(), timeout=5) == 1


@pytest.mark.usefixtures("_quiet_logging")
def test_a_missing_cache_is_still_a_refusal(monkeypatch, capsys):
    def no_cache():
        raise ConfigError("CACHE_DIR is not set. Copy .env.example to .env and fill it in.")

    monkeypatch.setattr(entry, "load_ui", no_cache)

    assert entry.main() == 2
    assert "CACHE_DIR" in capsys.readouterr().err


def _refuse(why: str):
    def refuse(*_args, **_kwargs):
        raise AssertionError(why)

    return refuse


async def _printed(capsys) -> str:
    for _ in range(100):
        out = capsys.readouterr().out.strip()
        if out:
            return out
        await asyncio.sleep(0.02)
    raise AssertionError("nothing was printed")


class TestClientFor:
    """A client for a page already listening on `port`, waiting for it to answer."""

    def __init__(self, port: int) -> None:
        self.port = port

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(base_url=f"http://127.0.0.1:{self.port}")
        for _ in range(100):
            try:
                async with self.session.get("/", allow_redirects=False):
                    return self.session
            except aiohttp.ClientConnectionError:
                await asyncio.sleep(0.05)
        raise AssertionError("the page never answered")

    async def __aexit__(self, *_exc) -> None:
        await self.session.close()


# -- the settings form ------------------------------------------------------------------

TOKEN = "the-clients-secret-token"


class Restarts:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


@pytest.fixture
def restarts(state: PageState, monkeypatch) -> Restarts:
    recorder = Restarts()
    state.restart = recorder
    monkeypatch.setattr(server_module, "_RESTART_DELAY_SECONDS", 0)
    return recorder


@pytest.fixture
async def signed_in(page: TestClient) -> TestClient:
    await set_up(page)
    return page


def pin(state: PageState, **values: str) -> None:
    """The environment setting these keys, as `load_ui` would have resolved it."""
    state.ui = UiSettings(
        cache_dir=state.ui.cache_dir,
        bind=state.ui.bind,
        port=state.ui.port,
        pinned=frozenset(values),
        pinned_values={name: value for name, value in values.items() if name != "CLIENT_TOKEN"},
    )


async def save(client: TestClient, **fields: str):
    form = {"server_url": "", "client_token": "", "player_name": "", **fields}
    return await post(client, "/settings", form)


async def settled() -> None:
    """Let the restart scheduled with `call_later(0, …)` run."""
    await asyncio.sleep(0.01)


class TestSavingTheServer:
    async def test_a_server_and_its_token_are_saved_and_the_player_restarts_onto_them(self, signed_in, cache_dir, restarts):
        response = await save(signed_in, server_url="http://arrt.example:8770/", client_token=TOKEN)
        await settled()

        assert response.status == 200
        assert store.read(cache_dir).values == {"SERVER_URL": "http://arrt.example:8770", "CLIENT_TOKEN": TOKEN}
        assert restarts.count == 1

    async def test_a_new_server_never_inherits_the_old_ones_token(self, signed_in, cache_dir, restarts):
        await save(signed_in, server_url="http://old.example:8770", client_token=TOKEN)

        await save(signed_in, server_url="http://new.example:8770")

        assert store.read(cache_dir).values == {"SERVER_URL": "http://new.example:8770"}

    async def test_a_new_server_with_its_own_token_keeps_the_new_token(self, signed_in, cache_dir, restarts):
        await save(signed_in, server_url="http://old.example:8770", client_token=TOKEN)

        await save(signed_in, server_url="http://new.example:8770", client_token="the-new-token")

        assert store.read(cache_dir).values["CLIENT_TOKEN"] == "the-new-token"

    async def test_the_same_server_with_the_token_left_empty_keeps_the_token(self, signed_in, cache_dir, restarts):
        await save(signed_in, server_url="http://arrt.example:8770", client_token=TOKEN)

        await save(signed_in, server_url="http://arrt.example:8770", player_name="Hall")

        assert store.read(cache_dir).values["CLIENT_TOKEN"] == TOKEN

    async def test_clearing_the_server_clears_its_token(self, signed_in, cache_dir, restarts):
        await save(signed_in, server_url="http://arrt.example:8770", client_token=TOKEN)

        await save(signed_in, server_url="")

        assert store.read(cache_dir).values == {}

    @pytest.mark.parametrize(
        ("fields", "said"),
        [
            ({"server_url": "arrt.example:8770", "client_token": TOKEN}, "http://"),
            ({"server_url": "http://user:pw@arrt.example", "client_token": TOKEN}, "user name or password"),
            ({"server_url": "http://arrt.example:port", "client_token": TOKEN}, "not an address"),
            ({"server_url": "http://arrt.example", "client_token": "two words"}, "no spaces"),
            ({"client_token": TOKEN}, "belongs to a server"),
            ({"player_name": "x" * 65}, "at most 64"),
            ({"player_name": "bell\x07"}, "control characters"),
        ],
    )
    async def test_a_value_config_would_refuse_is_refused_here_with_the_reason(
        self, signed_in, cache_dir, restarts, fields, said
    ):
        response = await save(signed_in, **fields)
        await settled()

        assert response.status == 400
        assert said in await response.text()
        assert store.read(cache_dir).values == {}
        assert restarts.count == 0

    async def test_nothing_changed_writes_nothing_and_does_not_restart(self, signed_in, cache_dir, restarts):
        await save(signed_in, player_name="Hall")
        await settled()
        written = store.path_in(cache_dir).stat().st_mtime_ns

        response = await save(signed_in, player_name="Hall")
        await settled()

        assert "Nothing changed" in await response.text()
        assert restarts.count == 1, "an unchanged form restarted the Player"
        assert store.path_in(cache_dir).stat().st_mtime_ns == written

    async def test_a_save_needs_a_session(self, signed_in, cache_dir, restarts):
        signed_in.session.cookie_jar.clear()

        response = await save(signed_in, player_name="Hall")

        assert response.status == 303
        assert store.read(cache_dir).values == {}


class TestWhatTheEnvironmentPins:
    async def test_a_pinned_key_is_shown_read_only_and_a_post_cannot_change_it(self, signed_in, state, cache_dir, restarts):
        pin(state, PLAYER_NAME="From env")

        text = await (await signed_in.get("/")).text()
        await save(signed_in, player_name="From the page")

        assert 'name="player_name" value="From env" disabled' in text
        assert "Set in the environment or .env" in text
        assert "PLAYER_NAME" not in store.read(cache_dir).values

    async def test_a_pinned_token_locks_the_server_address_too(self, signed_in, state, cache_dir, restarts):
        """Otherwise the page could point the environment's token at a host of its choosing."""
        pin(state, CLIENT_TOKEN=TOKEN)

        text = await (await signed_in.get("/")).text()
        response = await save(signed_in, server_url="http://elsewhere.example:8770")
        await settled()

        assert 'name="server_url" value="" disabled' in text
        assert "managed together" in text
        assert store.read(cache_dir).values == {}
        assert restarts.count == 0
        assert response.status == 200

    async def test_a_pinned_server_locks_the_token_too(self, signed_in, state, cache_dir, restarts):
        pin(state, SERVER_URL="http://arrt.example:8770")

        await save(signed_in, client_token="a-token-for-the-pinned-server")

        assert "CLIENT_TOKEN" not in store.read(cache_dir).values


class TestWhatThePageShows:
    async def test_the_token_is_never_shown_only_whether_one_is_set(self, signed_in, cache_dir, restarts):
        await save(signed_in, server_url="http://arrt.example:8770", client_token=TOKEN)

        text = await (await signed_in.get("/")).text()

        assert TOKEN not in text
        assert "A token is set." in text

    async def test_no_token_is_said_too(self, signed_in):
        assert "No token is set." in await (await signed_in.get("/")).text()

    async def test_the_servers_name_for_this_player_wins_over_its_own(self, signed_in, cache_dir, restarts):
        await save(signed_in, player_name="Our name")
        (cache_dir / CLIENT_DOCUMENT_FILENAME).write_text(
            json.dumps({"client_id": "c1", "name": "The server's name", "walls": []}), encoding="utf-8"
        )

        text = await (await signed_in.get("/")).text()

        assert "<title>Settings · The server&#x27;s name · Arrt Player</title>" in text
        assert "that name wins" in text

    async def test_without_a_server_name_the_players_own_is_used(self, signed_in, restarts):
        await save(signed_in, player_name="Our name")

        text = await (await signed_in.get("/")).text()

        assert "<title>Settings · Our name · Arrt Player</title>" in text
        assert "that name wins" not in text

    async def test_names_are_escaped(self, signed_in, restarts):
        await save(signed_in, player_name="<b>bold</b>")

        text = await (await signed_in.get("/")).text()

        assert "<b>bold</b>" not in text
        assert "&lt;b&gt;bold&lt;/b&gt;" in text


class TestChangingThePassword:
    async def test_the_right_current_password_changes_it_and_ends_other_sessions(self, signed_in, cache_dir, clock):
        old_cookie = auth.session_cookie(store.read(cache_dir).session_secret, now=clock())

        response = await post(
            signed_in, "/password", {"current": PASSWORD, "password": "a brand new one", "again": "a brand new one"}
        )

        assert response.status == 303
        assert auth.password_matches("a brand new one", store.read(cache_dir).password)
        assert (await signed_in.get("/", allow_redirects=False)).status == 200, "the changer was signed out"
        signed_in.session.cookie_jar.clear()
        stale = await signed_in.get("/", cookies={auth.COOKIE_NAME: old_cookie}, allow_redirects=False)
        assert stale.status == 303, "a session from before the change survived it"

    async def test_a_wrong_current_password_changes_nothing_and_is_throttled(self, signed_in, cache_dir):
        before = store.read(cache_dir).password

        wrong = await post(signed_in, "/password", {"current": "nope", "password": "a brand new one", "again": "a brand new one"})
        again = await post(
            signed_in, "/password", {"current": PASSWORD, "password": "a brand new one", "again": "a brand new one"}
        )

        assert (wrong.status, again.status) == (403, 429)
        assert store.read(cache_dir).password == before


async def test_a_save_logs_which_keys_changed_and_never_their_values(signed_in, restarts, caplog):
    caplog.set_level(logging.DEBUG)

    await save(signed_in, server_url="http://arrt.example:8770", client_token=TOKEN)

    saved = [record for record in caplog.records if getattr(record, "event", None) == "settings_page.saved"]
    assert [record.changed for record in saved] == [["CLIENT_TOKEN", "SERVER_URL"]]
    assert not any(TOKEN in str(vars(record)) for record in caplog.records)


def test_a_restart_is_the_orderly_stop_systemd_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(entry.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    entry.restart_this_process()

    assert sent == [(os.getpid(), signal.SIGTERM)]


class TestTheNameAPlayerGoesBy:
    def test_with_no_server_document_it_is_its_own(self, cache_dir: Path):
        assert player_name(cache_dir, "Hall") == "Hall"

    @pytest.mark.parametrize("document", ["not json", '{"name": "no id"}', "\udcff"])
    def test_an_unreadable_server_document_falls_back_to_its_own(self, cache_dir: Path, document: str):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / CLIENT_DOCUMENT_FILENAME).write_bytes(document.encode("utf-8", "surrogateescape"))

        assert player_name(cache_dir, "Hall") == "Hall"


# -- what the review of the whole branch found -----------------------------------------------


def test_a_thousand_wrong_guesses_still_wait_a_minute_and_never_overflow():
    """`2.0 ** 1024` overflows a float: uncapped, the 1025th wrong password would make every
    later login a server error, the right password's included, until a restart."""
    clock = Clock()
    throttle = auth.Throttle(monotonic=clock)

    for _ in range(2000):
        throttle.failed()

    assert throttle.wait_seconds() == auth.MAXIMUM_THROTTLE_SECONDS


class TestAWriteTheDiskRefuses:
    @pytest.fixture
    def refusing(self, monkeypatch):
        """The disk refusing the next write, then accepting again."""
        real = store.write
        calls = {"left": 1}

        def write(cache_dir, stored):
            if calls["left"]:
                calls["left"] -= 1
                raise PermissionError(13, "Permission denied")
            real(cache_dir, stored)

        monkeypatch.setattr(store, "write", write)
        return calls

    async def test_a_failed_first_password_gives_the_code_back_so_a_retry_works(self, page, cache_dir, state, refusing, caplog):
        failed = await post(page, "/setup", {"code": CODE, "password": PASSWORD, "again": PASSWORD})

        assert failed.status == 500
        assert "could not be saved" in await failed.text()
        assert state.setup_code == CODE, "the code was spent on a password that was never set"
        assert any(getattr(record, "event", None) == "settings_page.write_failed" for record in caplog.records)

        retried = await post(page, "/setup", {"code": CODE, "password": PASSWORD, "again": PASSWORD})
        assert retried.status == 303
        assert auth.password_matches(PASSWORD, store.read(cache_dir).password)

    async def test_a_failed_save_says_so_and_does_not_restart(self, signed_in, cache_dir, restarts, refusing):
        response = await save(signed_in, player_name="Hall")
        await settled()

        assert response.status == 500
        assert "could not be saved" in await response.text()
        assert restarts.count == 0
        assert store.read(cache_dir).values == {}

    async def test_a_failed_password_change_keeps_the_old_password_and_session(self, signed_in, cache_dir, refusing):
        response = await post(
            signed_in, "/password", {"current": PASSWORD, "password": "a brand new one", "again": "a brand new one"}
        )

        assert response.status == 500
        assert auth.password_matches(PASSWORD, store.read(cache_dir).password)
        assert (await signed_in.get("/", allow_redirects=False)).status == 200


@pytest.mark.parametrize(("method", "path"), [("GET", "/nowhere"), ("PUT", "/settings")])
async def test_aiohttps_own_errors_carry_the_security_headers_too(page, method, path):
    response = await page.request(method, path, headers=origin(page))

    assert response.status in {404, 405}
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, f"{method} {path} answered without {name}"


async def test_the_flows_leave_their_events_in_the_journal(page, clock, caplog):
    """`deploy/README.md` tells the operator to look for these, so each is asserted by name."""
    caplog.set_level(logging.INFO)
    await set_up(page)
    page.session.cookie_jar.clear()
    await post(page, "/login", {"password": "wrong"})
    clock.now += 2
    await post(page, "/login", {"password": PASSWORD})
    await post(page, "/password", {"current": "wrong", "password": "irrelevant!", "again": "irrelevant!"})
    clock.now += 2
    await post(page, "/password", {"current": PASSWORD, "password": "a brand new one", "again": "a brand new one"})

    events = {getattr(record, "event", None) for record in caplog.records}

    assert {
        "settings_page.password_set",
        "settings_page.login_refused",
        "settings_page.password_refused",
        "settings_page.password_changed",
    } <= events


async def test_setup_mode_says_so_in_the_journal_and_names_the_page(monkeypatch, cache_dir, capsys, caplog):
    caplog.set_level(logging.INFO)
    port = free_port()
    monkeypatch.setattr(entry, "load_ui", lambda: loopback(cache_dir, port))

    def no_server():
        raise SetupRequired(("SERVER_URL",))

    monkeypatch.setattr(entry, "load", no_server)
    running = asyncio.create_task(entry._run())
    await _printed(capsys)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(running, timeout=5)

    by_event = {getattr(record, "event", None): record for record in caplog.records}
    assert by_event["daemon.setup_mode"].missing == ["SERVER_URL"]
    listening = by_event["settings_page.listening"]
    assert (listening.bind, listening.port, listening.setup_mode) == ("127.0.0.1", port, True)


# -- the slideshow ---------------------------------------------------------------------------

DAY = 86400.0


def a_cookie(expires_at: float | None, secret: str = "the-cookies-secret-part") -> str:  # noqa: S107 -- a test value
    claims = {"sub": "t2_x"} if expires_at is None else {"exp": expires_at}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.{secret}"


async def save_slideshow(client: TestClient, **fields: str):
    """The whole form as the page draws it in slideshow mode."""
    form = {
        "server_url": "",
        "client_token": "",
        "player_name": "",
        "player_mode": "slideshow",
        "slideshow_url": "https://www.reddit.com/r/EarthPorn/top/?t=month",
        "reddit_session": "",
        "slideshow_min_seconds": "",
        "slideshow_max_seconds": "",
        "slideshow_output": "hdmi-a-1",
        **fields,
    }
    return await post(client, "/settings", form)


class TestTheSlideshowsSettings:
    async def test_a_slideshow_is_saved_and_the_player_restarts_into_it(self, signed_in, cache_dir, restarts):
        cookie = a_cookie(1_900_000_000)

        response = await save_slideshow(signed_in, reddit_session=cookie, slideshow_min_seconds="20")
        await settled()

        assert response.status == 200
        assert store.read(cache_dir).values == {
            "PLAYER_MODE": "slideshow",
            "SLIDESHOW_URL": "https://www.reddit.com/r/EarthPorn/top/?t=month",
            "REDDIT_SESSION": cookie,
            "SLIDESHOW_MIN_SECONDS": "20",
            "SLIDESHOW_OUTPUT": "hdmi-a-1",
        }
        assert restarts.count == 1

    async def test_the_cookie_is_never_in_a_page_and_its_name_is_dropped_if_pasted(self, signed_in, cache_dir, restarts, caplog):
        cookie = a_cookie(1_900_000_000)

        with caplog.at_level(logging.DEBUG):
            saved = await save_slideshow(signed_in, reddit_session=f"reddit_session={cookie}")
            shown = await signed_in.get("/")

        assert store.read(cache_dir).values["REDDIT_SESSION"] == cookie
        for response in (saved, shown):
            assert "the-cookies-secret-part" not in await response.text()
        assert "the-cookies-secret-part" not in caplog.text
        assert "A cookie is set." in await shown.text()

    async def test_an_empty_cookie_field_keeps_the_cookie(self, signed_in, cache_dir, restarts):
        cookie = a_cookie(1_900_000_000)
        await save_slideshow(signed_in, reddit_session=cookie)

        await save_slideshow(signed_in, slideshow_min_seconds="45")

        assert store.read(cache_dir).values["REDDIT_SESSION"] == cookie

    async def test_a_form_without_the_slideshows_fields_leaves_them_as_they_are(self, signed_in, cache_dir, restarts):
        await save_slideshow(signed_in, reddit_session=a_cookie(None))

        await save(signed_in, player_name="Hall")

        assert {"PLAYER_MODE", "SLIDESHOW_URL", "REDDIT_SESSION", "PLAYER_NAME"} <= set(store.read(cache_dir).values)

    @pytest.mark.parametrize(
        ("fields", "said"),
        [
            ({"slideshow_min_seconds": "90", "slideshow_max_seconds": "60"}, "not be more than the maximum"),
            ({"slideshow_min_seconds": "2"}, "at least 5"),
            ({"slideshow_max_seconds": "a minute"}, "numbers of seconds"),
            ({"slideshow_url": "https://www.reddit.com/"}, "names no listing"),
            ({"slideshow_url": "https://imgur.com/gallery/x"}, "reddit.com address"),
            ({"reddit_session": "two words"}, "REDDIT_SESSION"),
            ({"player_mode": "both"}, "PLAYER_MODE"),
            ({"slideshow_output": "hdmi-a-1; reboot"}, "HDMI connector"),
        ],
    )
    async def test_a_value_config_would_refuse_is_refused_here(self, signed_in, cache_dir, restarts, fields, said):
        response = await save_slideshow(signed_in, **fields)
        await settled()

        assert response.status == 400
        assert said in await response.text()
        assert store.read(cache_dir).values == {}
        assert restarts.count == 0

    async def test_a_new_minimum_is_judged_against_the_maximum_already_saved(self, signed_in, cache_dir, restarts):
        await save_slideshow(signed_in, slideshow_max_seconds="40")

        response = await save_slideshow(signed_in, slideshow_min_seconds="50", slideshow_max_seconds="40")
        lone = await post(signed_in, "/settings", {"slideshow_min_seconds": "50"})

        assert response.status == 400
        assert lone.status == 400
        assert "SLIDESHOW_MIN_SECONDS" not in store.read(cache_dir).values

    async def test_a_pinned_cookie_is_neither_taken_nor_shown_but_its_expiry_is(
        self, signed_in, state, cache_dir, restarts, clock
    ):
        state.ui = UiSettings(
            cache_dir=state.ui.cache_dir,
            bind=state.ui.bind,
            port=state.ui.port,
            pinned=frozenset({"REDDIT_SESSION"}),
            pinned_session_expiry=clock.now + 3 * DAY,
        )

        await save_slideshow(signed_in, reddit_session=a_cookie(None, "another"))
        text = await (await signed_in.get("/")).text()

        assert "REDDIT_SESSION" not in store.read(cache_dir).values
        assert "in 3 days" in text
        assert 'id="reddit_session" type="password" name="reddit_session" value="" autocomplete="off" disabled' in text


class TestTheCookiesExpiry:
    @pytest.mark.parametrize(
        ("days", "said", "warned"),
        [
            (20, "It expires on", False),
            (14.5, "It expires on", False),
            (13.5, "in 14 days", True),
            (5, "in 5 days", True),
            (0.2, "in 1 day.", True),
            (-1, "it expired on", True),
        ],
    )
    async def test_a_warning_inside_two_weeks_and_none_outside(self, signed_in, clock, restarts, days, said, warned):
        await save_slideshow(signed_in, reddit_session=a_cookie(clock.now + days * DAY))

        text = await (await signed_in.get("/")).text()

        assert said in text
        assert ("Expiring" in text) is warned
        assert ("Paste a new one" in text) is warned

    async def test_a_cookie_that_does_not_say_is_said_so_without_a_warning(self, signed_in, restarts):
        await save_slideshow(signed_in, reddit_session=a_cookie(None))

        text = await (await signed_in.get("/")).text()

        assert "It does not say when it expires." in text
        assert "Expiring" not in text

    async def test_no_cookie_is_said(self, signed_in):
        assert "No cookie is set." in await (await signed_in.get("/")).text()


class TestWhatTheSlideshowSays:
    async def test_setup_mode_for_a_slideshow_says_nothing_is_shown(self, signed_in, state):
        state.mode, state.missing = "slideshow", ("SLIDESHOW_URL", "REDDIT_SESSION")

        text = await (await signed_in.get("/")).text()

        assert "SLIDESHOW_URL, REDDIT_SESSION not set when the Player started, so nothing is shown." in text
        assert "no walls" not in text

    @pytest.mark.parametrize(
        ("outcome", "said"),
        [
            (Outcome.LISTED, "Reddit gave the listing."),
            (Outcome.REFUSED, "Reddit refuses this listing even with a good cookie. Try its top listing instead"),
            (Outcome.COOKIE_REJECTED, "Reddit no longer accepts the cookie. Paste a new one"),
            (Outcome.RATE_LIMITED, "Reddit asked the Player to wait"),
            (Outcome.UNREACHABLE, "Reddit could not be reached."),
        ],
    )
    async def test_each_answer_from_reddit_is_said_in_its_own_words(self, signed_in, state, clock, outcome, said):
        state.missing = ()
        state.slideshow = SlideshowStatus(outcome=outcome, answered_at=clock.now - 30, items=4)
        others = [words for kind, words in TestWhatTheSlideshowSays.WORDS if kind is not outcome]

        text = html.unescape(await (await signed_in.get("/")).text())

        assert said in text
        assert "30 s ago" in text
        assert not any(other in text for other in others), "another outcome's words were drawn too"

    WORDS: ClassVar = [
        (Outcome.LISTED, "Reddit gave the listing."),
        (Outcome.REFUSED, "refuses this listing"),
        (Outcome.COOKIE_REJECTED, "no longer accepts the cookie"),
        (Outcome.RATE_LIMITED, "asked the Player to wait"),
        (Outcome.UNREACHABLE, "could not be reached"),
    ]

    async def test_a_listing_names_the_sites_that_gave_nothing(self, signed_in, state):
        state.missing = ()
        state.slideshow = SlideshowStatus(
            outcome=Outcome.LISTED, items=87, unresolved=Counter({"gfycat.com": 3, "youtube.com": 2}), showing="A heron"
        )

        text = await (await signed_in.get("/")).text()

        assert "87 items to show. Posts with nothing playable, by site: gfycat.com 3, youtube.com 2." in text
        assert "On screen: A heron" in text
        assert "Running" in text
        assert "a Reddit slideshow" in text

    async def test_downloads_are_said_by_the_tier_that_gave_them(self, signed_in, state):
        state.missing = ()
        state.slideshow = SlideshowStatus(
            outcome=Outcome.LISTED, items=47, downloaded=47, tiers=Counter({"reddit": 45, "host:imgur": 2}), failed=3
        )

        text = await (await signed_in.get("/")).text()

        assert "Downloaded since start: 47 (reddit 45, host:imgur 2)." in text
        assert "3 would not download last time" in text

    async def test_nothing_downloaded_and_nothing_failed_says_neither(self, signed_in, state):
        state.missing = ()
        state.slideshow = SlideshowStatus(outcome=Outcome.LISTED, items=4)

        text = await (await signed_in.get("/")).text()

        assert "Downloaded since start" not in text
        assert "would not download" not in text

    async def test_a_cookie_claiming_an_impossible_expiry_still_draws_the_page(self, signed_in, restarts):
        body = base64.urlsafe_b64encode(b'{"exp": 1e300}').decode().rstrip("=")
        await save_slideshow(signed_in, reddit_session=f"eyJh.{body}.sig")

        response = await signed_in.get("/")

        assert response.status == 200
        assert "It does not say when it expires." in await response.text()

    async def test_what_would_stop_mpv_is_said_as_a_problem_and_absent_when_there_is_none(self, signed_in, state):
        state.missing = ()
        state.slideshow = SlideshowStatus(outcome=Outcome.LISTED, renderer_problems=("this account is not in group render",))
        warned = await (await signed_in.get("/")).text()
        state.slideshow = SlideshowStatus(outcome=Outcome.LISTED)
        quiet = await (await signed_in.get("/")).text()

        assert '<p class="note error" role="alert">The slideshow cannot draw: this account is not in group render.</p>' in warned
        assert "cannot draw" not in quiet

    async def test_an_outage_says_it_plays_on_from_what_was_listed(self, signed_in, state):
        state.missing = ()
        state.slideshow = SlideshowStatus(outcome=Outcome.UNREACHABLE, items=12)

        text = await (await signed_in.get("/")).text()

        assert "Playing on from the 12 items already listed." in text

    async def test_before_reddit_answers_it_says_it_is_waiting(self, signed_in, state):
        state.missing = ()
        state.slideshow = SlideshowStatus(items=5, from_saved=True)

        text = await (await signed_in.get("/")).text()

        assert "Reddit has not answered yet. Playing the 5 items saved from the last listing." in text

    async def test_outside_a_slideshow_none_of_it_is_drawn(self, signed_in):
        text = await (await signed_in.get("/")).text()

        assert "Reddit has not answered" not in text
        assert 'role="status"' not in text.split("Slideshow</h2>", 1)[1].split("</section>", 1)[0]

    async def test_the_screens_on_offer_are_this_machines_hdmi_connectors_with_whether_one_is_connected(self, signed_in, state):
        state.outputs = lambda: [
            OutputReport("hdmi-a-1", "framebuffer", True, (3840, 2160)),
            OutputReport("hdmi-a-2", "framebuffer", False, None),
        ]

        text = await (await signed_in.get("/")).text()

        assert '<option value="hdmi-a-1" selected>hdmi-a-1 (a screen at 3840×2160)</option>' in text
        assert '<option value="hdmi-a-2">hdmi-a-2 (no screen connected)</option>' in text

    async def test_a_chosen_screen_this_machine_does_not_have_is_still_offered_and_named(self, signed_in, restarts):
        await save_slideshow(signed_in, slideshow_output="hdmi-a-2")

        text = await (await signed_in.get("/")).text()

        assert '<option value="hdmi-a-2" selected>hdmi-a-2 (not found on this machine)</option>' in text

    async def test_the_mode_the_player_is_set_to_is_the_one_checked(self, signed_in, restarts):
        before = await (await signed_in.get("/")).text()
        await save_slideshow(signed_in)
        after = await (await signed_in.get("/")).text()

        assert 'value="arrt" checked' in before
        assert 'value="slideshow" checked' not in before
        assert 'value="slideshow" checked' in after
        assert 'value="arrt" checked' not in after
