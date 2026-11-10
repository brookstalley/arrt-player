"""The settings page: its file, its password and cookie, its pages, and setup mode.

The page sits on the LAN and guards the client token, so most of what is tested
here is what it refuses: a setup without the code, a code used twice, a POST
from another origin, a cookie signed under another secret, a run of wrong
passwords. And what it never says: no password, hash, secret or cookie reaches a
log record.
"""

import asyncio
import logging
import os
import signal
import socket
import stat
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from arrt_player import __main__ as entry
from arrt_player.config import ConfigError, SetupRequired, UiSettings
from arrt_player.settings_ui import auth, store
from arrt_player.settings_ui.server import SECURITY_HEADERS, PageState, build

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
            ('{"version": 2}', "version 2"),
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
