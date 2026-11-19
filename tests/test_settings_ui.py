"""The settings page: its file, its password and cookie, its pages, and setup mode.

The page sits on the LAN and guards the client token, so most of what is tested
here is what it refuses: a setup without the code, a code used twice, a POST
from another origin, a cookie signed under another secret, a run of wrong
passwords. And what it never says: no password, hash, secret or cookie reaches a
log record.
"""

import asyncio
import base64
import dataclasses
import html
import json
import logging
import os
import re
import signal
import socket
import stat
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import journal

# The configuration's own refused examples, so the page is held to the same ones a start refuses.
from test_config import FRAME, REFUSED_SECONDS, REFUSED_TV_ADDRESSES, SECONDS_KEYS, a_slideshow, an_environment

from arrt_player import __main__ as entry
from arrt_player import config
from arrt_player.client import OutputReport, player_name
from arrt_player.config import CLIENT_DOCUMENT_FILENAME, ConfigError, SetupRequired, UiSettings, load
from arrt_player.programmes.slideshow_status import SlideshowStatus
from arrt_player.settings_ui import auth, store
from arrt_player.settings_ui import server as server_module
from arrt_player.settings_ui.server import SECURITY_HEADERS, PageState, build, own_names, screen_name
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
            # The first layout past the newest this code reads, whatever that is.
            (f'{{"version": {store.VERSION + 1}}}', f"version {store.VERSION + 1}"),
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


#: Two addresses on the LAN, from the range kept for documentation (RFC 5737).
GUESSER, OWNER = "192.0.2.66", "192.0.2.10"


#: A third address, one that has never signed in.
NEWCOMER = "192.0.2.77"
#: Two addresses one guesser holds (IPv4 and an IPv6 privacy address, say).
GUESSERS = ("192.0.2.66", "2001:db8::66")


def _fill_the_ceiling_from_two_addresses(throttle: auth.Throttle, clock: Clock) -> None:
    """Guess from two addresses as fast as each one's own doubling lets them, until the ceiling holds."""
    for _ in range(10_000):
        if len(throttle._recent) >= auth.CEILING_FAILURES:
            return
        for guesser in GUESSERS:
            if throttle.wait_seconds(guesser) == 0:
                throttle.failed(guesser)
        clock.now += 1
    raise AssertionError("two addresses never reached the ceiling")


class TestTheThrottle:
    def test_each_failure_doubles_the_wait_up_to_a_minute_and_success_clears_it(self):
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        waits = []
        for _ in range(9):
            throttle.failed(GUESSER)
            waits.append(throttle.wait_seconds(GUESSER))

        assert waits == [1, 2, 4, 8, 16, 32, 60, 60, 60]
        clock.now += 61
        assert throttle.wait_seconds(GUESSER) == 0
        throttle.succeeded(GUESSER)
        throttle.failed(GUESSER)
        assert throttle.wait_seconds(GUESSER) == 1

    def test_one_addresss_wrong_passwords_do_not_slow_another(self):
        """A single throttle for the process let a guesser hold the owner out for as long as it kept guessing."""
        throttle = auth.Throttle(monotonic=Clock())
        for _ in range(6):
            throttle.failed(GUESSER)

        assert throttle.wait_seconds(GUESSER) == 32
        assert throttle.wait_seconds(OWNER) == 0

    def test_wrong_passwords_from_many_addresses_meet_one_ceiling(self):
        """A guesser that takes a new address for every guess is held by the ceiling, and freed as the window passes."""
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        for n in range(auth.CEILING_FAILURES - 1):
            assert throttle.wait_seconds(f"192.0.2.{n}") == 0
            throttle.failed(f"192.0.2.{n}")
            clock.now += 1

        assert throttle.wait_seconds(OWNER) == 0, "the ceiling was reached one failure early"
        throttle.failed("192.0.2.200")
        assert throttle.wait_seconds(OWNER) == auth.CEILING_WINDOW_SECONDS - auth.CEILING_FAILURES + 1

        clock.now += auth.CEILING_WINDOW_SECONDS - auth.CEILING_FAILURES + 1
        assert throttle.wait_seconds(OWNER) == 0

    def test_a_right_password_gives_back_the_charge_its_attempt_made(self):
        """Every attempt is charged before its password is checked; a right one must not count toward the ceiling."""
        throttle = auth.Throttle(monotonic=Clock())
        for _ in range(auth.CEILING_FAILURES):
            throttle.failed(OWNER)
            throttle.succeeded(OWNER)

        assert throttle.wait_seconds(GUESSER) == 0

    def test_an_address_that_signed_in_passes_the_ceiling_and_only_the_ceiling(self):
        """Two guessing addresses reach the ceiling by their own doubling alone; the
        owner's browser, which signed in before, must still be heard, and a guesser
        sharing its address (a reverse proxy) is still held by the address's own wait."""
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        throttle.failed(OWNER)
        throttle.succeeded(OWNER)
        _fill_the_ceiling_from_two_addresses(throttle, clock)

        assert throttle.wait_seconds(NEWCOMER) > 0, "the ceiling no longer holds an address that never signed in"
        assert throttle.wait_seconds(OWNER) == 0, "the owner was held by guessers at two other addresses"
        throttle.failed(OWNER)
        assert throttle.wait_seconds(OWNER) == 1, "an address that signed in was let guess without its own wait"

    def test_an_address_with_standing_neither_fills_the_ceiling_nor_empties_it(self):
        """Its attempts are not held by the ceiling, so they must not be charged to it:
        a guesser sharing it (a proxy) would fill the ceiling for every other
        address, and a right password at a full ceiling gave back a charge some
        other address made."""
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        throttle.vouch(OWNER)
        _fill_the_ceiling_from_two_addresses(throttle, clock)
        held_for = throttle.wait_seconds(NEWCOMER)
        throttle.failed(OWNER)
        throttle.succeeded(OWNER)
        assert throttle.wait_seconds(NEWCOMER) == held_for, "the owner's sign-in let a guesser's failure go"

        throttle = auth.Throttle(monotonic=clock)
        throttle.vouch(OWNER)
        for n in range(auth.CEILING_FAILURES - 1):
            throttle.failed(f"192.0.2.{n}")
        throttle.failed(OWNER)
        assert throttle.wait_seconds(NEWCOMER) == 0, "a failure at an address with standing filled the ceiling"

    def test_a_signed_in_visit_gives_the_standing_a_sign_in_does(self):
        """A save restarts the Player, so the standing a sign-in gave is gone; the owner's next visit restores it."""
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        throttle.vouch(OWNER)
        _fill_the_ceiling_from_two_addresses(throttle, clock)

        assert throttle.wait_seconds(OWNER) == 0

    def test_the_standing_lasts_a_session_and_no_longer(self):
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        throttle.vouch(OWNER)
        clock.now += auth.SESSION_SECONDS
        _fill_the_ceiling_from_two_addresses(throttle, clock)

        assert throttle.wait_seconds(OWNER) > 0, "a sign-in older than a session still passed the ceiling"
        assert throttle._standing == {}, "an expired standing was kept"

    def test_a_new_password_ends_every_standing(self):
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        throttle.vouch(OWNER)
        throttle.forget_standing()
        _fill_the_ceiling_from_two_addresses(throttle, clock)

        assert throttle.wait_seconds(OWNER) > 0, "a standing from before the password changed survived it"

    def test_an_address_quiet_for_the_window_is_forgotten(self):
        """Otherwise every address that ever guessed is held for the life of the process."""
        clock = Clock()
        throttle = auth.Throttle(monotonic=clock)
        throttle.failed(GUESSER)
        throttle.failed(GUESSER)

        clock.now += 2 + auth.CEILING_WINDOW_SECONDS
        throttle.wait_seconds(OWNER)

        assert throttle._runs == {}
        throttle.failed(GUESSER)
        assert throttle.wait_seconds(GUESSER) == 1


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
        refused_hosts=server_module.RefusedHosts(monotonic=clock),
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


# -- the names it answers to ------------------------------------------------------------

#: A name the page was never told, standing for the domain a rebinding page points at it.
REBOUND = "rebound.example"

#: A name this machine goes by, given to it for the test (`PageState.machine_names`).
MACHINE = "wallpi"


def _host_refusals(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "event", None) == "settings_page.host_refused"]


@pytest.fixture
def machine(monkeypatch) -> str:
    """This machine called `MACHINE`, as `socket.gethostname` would say."""
    monkeypatch.setattr(server_module.socket, "gethostname", lambda: MACHINE)
    return MACHINE


def port_of(client: TestClient) -> int:
    return client.make_url("").port


async def setup_as(client: TestClient, *, host: str, origin_header: str | None):
    headers = {"Host": host} | ({"Origin": origin_header} if origin_header is not None else {})
    return await client.post(
        "/setup", data={"code": CODE, "password": PASSWORD, "again": PASSWORD}, headers=headers, allow_redirects=False
    )


class TestTheNamesItAnswersTo:
    """DNS rebinding: a page elsewhere points its own name at this machine, and its requests carry that name."""

    @pytest.mark.parametrize("path", ["/", "/login", f"/setup?code={CODE}", "/static/settings.css", "/no-such-page"])
    async def test_a_request_by_a_name_it_was_never_told_is_refused_before_any_page(self, page, path):
        response = await page.get(path, headers={"Host": f"{REBOUND}:{port_of(page)}"}, allow_redirects=False)

        assert response.status == server_module.HOST_REFUSED
        text = await response.text()
        assert "UI_ALLOWED_HOSTS" in text, "the refusal does not say what to change"
        assert 'name="password"' not in text
        assert "has no password yet" not in text
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value

    async def test_a_post_whose_origin_and_host_both_name_it_is_refused_and_does_nothing(self, page, cache_dir):
        """The rebinding page's own form: Origin and Host agree, which the check comparing them let through."""
        named = f"{REBOUND}:{port_of(page)}"

        response = await setup_as(page, host=named, origin_header=f"http://{named}")

        assert response.status == server_module.HOST_REFUSED
        assert store.read(cache_dir).password is None

    async def test_the_refusal_is_in_the_journal_with_the_name(self, page, caplog):
        caplog.set_level(logging.WARNING)

        await page.get("/", headers={"Host": REBOUND})

        refused = [record for record in caplog.records if getattr(record, "event", None) == "settings_page.host_refused"]
        assert [record.host for record in refused] == [REBOUND]

    async def test_a_name_refused_again_and_again_is_one_line_with_a_count(self, page, clock, caplog):
        """Anybody who can reach the port chooses how often a refusal happens, so
        it is said once per name per window, and the next line counts the rest."""
        caplog.set_level(logging.WARNING)
        for _ in range(50):
            await page.get("/", headers={"Host": REBOUND})
            await page.get("/", headers={"Host": f"other.{REBOUND}"})
        assert sorted(record.host for record in _host_refusals(caplog)) == sorted([REBOUND, f"other.{REBOUND}"])

        for _ in range(2):  # the one name keeps arriving; the other has stopped
            clock.now += server_module.REFUSED_HOST_WINDOW_SECONDS / 2
            await page.get("/", headers={"Host": REBOUND})

        said = [(record.host, record.refused) for record in _host_refusals(caplog)]
        assert said[2:] == [(f"other.{REBOUND}", 49), (REBOUND, 51)]

    async def test_a_name_refused_once_and_quiet_since_has_its_count_said_when_it_is_forgotten(self, page, clock, caplog):
        caplog.set_level(logging.WARNING)
        for _ in range(3):
            await page.get("/", headers={"Host": REBOUND})
        clock.now += server_module.REFUSED_HOST_WINDOW_SECONDS
        await page.get("/", headers={"Host": f"other.{REBOUND}"})

        said = {(record.host, record.refused) for record in _host_refusals(caplog)}
        assert said == {(REBOUND, 1), (REBOUND, 2), (f"other.{REBOUND}", 1)}

    async def test_names_chosen_by_the_caller_cannot_set_the_journals_rate_or_the_memory(self, page, state, caplog):
        caplog.set_level(logging.WARNING)
        names = server_module.REFUSED_HOST_NAMES_KEPT

        for n in range(names * 4):
            await page.get("/", headers={"Host": f"n{n}.{REBOUND}"})

        refusals = _host_refusals(caplog)
        assert len(refusals) == names + 1, "every new name the caller chose was a journal line"
        assert refusals[-1].host is None, "the names past the table were not said together"
        assert len(state.refused_hosts._names) <= names

    async def test_the_names_past_the_table_share_one_count_said_once_a_window(self, page, clock, caplog):
        caplog.set_level(logging.WARNING)
        kept = [f"n{n}.{REBOUND}" for n in range(server_module.REFUSED_HOST_NAMES_KEPT)]

        async def refuse(names: list[str]) -> None:
            for name in names:
                await page.get("/", headers={"Host": name})

        await refuse(kept)
        await refuse([f"past{n}.{REBOUND}" for n in range(5)])
        for step in range(2):  # the table's names and the rest keep arriving
            clock.now += server_module.REFUSED_HOST_WINDOW_SECONDS / 2
            await refuse([*kept, f"later{step}.{REBOUND}"])

        shared = [record.refused for record in _host_refusals(caplog) if record.host is None]
        assert shared == [1, 6], "the names past the table were not counted together, once a window"

    async def test_the_shared_count_is_said_as_it_is_forgotten(self, page, clock, caplog):
        caplog.set_level(logging.WARNING)
        for n in range(server_module.REFUSED_HOST_NAMES_KEPT + 5):
            await page.get("/", headers={"Host": f"n{n}.{REBOUND}"})
        clock.now += server_module.REFUSED_HOST_WINDOW_SECONDS

        await page.get("/", headers={"Host": REBOUND})

        said = [(record.host, record.refused) for record in _host_refusals(caplog)]
        assert said[-2:] == [(None, 4), (REBOUND, 1)], "the names past the table took their count with them"

    @pytest.mark.parametrize("port", ["\u00b2", "99999", "-1", "+80", " 80", "0x50", "8771x"])
    async def test_a_host_whose_port_is_not_a_port_is_refused_and_not_an_error(self, page, cache_dir, port):
        host = f"127.0.0.1:{port}"

        response = await setup_as(page, host=host, origin_header=f"http://{host}")

        assert response.status == server_module.HOST_REFUSED
        assert store.read(cache_dir).password is None

    @pytest.mark.parametrize("port", ["\u00b2", "99999"])
    async def test_an_origin_whose_port_is_not_a_port_is_refused_and_not_an_error(self, page, cache_dir, port):
        response = await setup_as(page, host=f"127.0.0.1:{port_of(page)}", origin_header=f"http://127.0.0.1:{port}")

        assert response.status == 403
        assert store.read(cache_dir).password is None

    @pytest.mark.parametrize("after", ["x", "80", "]", "[::2]"])
    async def test_a_bracketed_host_followed_by_anything_but_a_port_is_refused(self, page, cache_dir, after):
        """`[::1]junk` once read as `::1` with no port, and was answered as this page."""
        host = f"[::1]{after}"

        response = await setup_as(page, host=host, origin_header=f"http://{host}")

        assert response.status == server_module.HOST_REFUSED
        assert store.read(cache_dir).password is None

    @pytest.mark.parametrize("after", ["x", "80"])
    async def test_a_bracketed_origin_followed_by_anything_but_a_port_is_refused(self, page, cache_dir, after):
        """The Host is good and names no port, so the Origin's missing port would match it, but the Origin is not one."""
        response = await setup_as(page, host="[::1]", origin_header=f"http://[::1]{after}")

        assert response.status == 403
        assert store.read(cache_dir).password is None

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1:{port}",
            "127.0.0.1",
            "[::1]:{port}",
            "192.0.2.10:{port}",
            "localhost:{port}",
            "LocalHost.:{port}",
            f"{MACHINE}:{{port}}",
            f"{MACHINE}.local:{{port}}",
            f"{MACHINE.upper()}.LOCAL.",
        ],
    )
    async def test_an_address_localhost_and_the_machines_own_names_are_answered(self, state, machine, host):
        """Not answering to these would lock the owner out of a headless Pi: they are how it is reached unconfigured."""
        async with TestClient(TestServer(build(state))) as client:
            response = await client.get("/", headers={"Host": host.format(port=port_of(client))})

            assert response.status == 200, f"{host} was refused"

    async def test_the_name_this_machine_really_has_is_answered(self, page):
        """Unpatched: whatever `socket.gethostname` says here, and its `.local`."""
        short = socket.gethostname().split(".", 1)[0]

        for host in (socket.gethostname(), f"{short}.local"):
            assert (await page.get("/", headers={"Host": host})).status == 200, f"{host} was refused"

    async def test_the_machines_names_are_its_own_and_its_bound_name(self, machine):
        assert own_names("0.0.0.0") == {"localhost", MACHINE, f"{MACHINE}.local"}  # noqa: S104 -- a name, not a bind
        assert "wall.example" in own_names("wall.example")

    async def test_a_mac_style_host_name_gives_its_short_name_too(self, monkeypatch):
        monkeypatch.setattr(server_module.socket, "gethostname", lambda: "Studio.local")

        assert own_names("") == {"localhost", "studio", "studio.local"}

    async def test_the_owner_sets_up_by_the_machines_name(self, state, machine, cache_dir):
        """Host and Origin both the machine's `.local` name, as a browser sends them from the printed link."""
        async with TestClient(TestServer(build(state))) as client:
            named = f"{MACHINE}.local:{port_of(client)}"

            response = await setup_as(client, host=named, origin_header=f"http://{named}")

        assert response.status == 303
        assert store.read(cache_dir).password is not None


class TestTheNamesItIsTold:
    """`UI_ALLOWED_HOSTS`: a router's DNS name for this machine, or a reverse proxy's."""

    @pytest.fixture
    def told(self, state: PageState) -> PageState:
        state.ui = UiSettings(
            cache_dir=state.ui.cache_dir, bind="127.0.0.1", port=0, pinned=frozenset(), allowed_hosts=frozenset({"wall.lan"})
        )
        return state

    async def test_a_listed_name_is_answered(self, told):
        async with TestClient(TestServer(build(told))) as client:
            assert (await client.get("/", headers={"Host": "Wall.Lan:8771"})).status == 200

    async def test_a_listed_name_is_a_good_origin_on_any_scheme_or_port(self, told, cache_dir):
        """A reverse proxy changes both: the browser's Origin is the proxy's, the Host may be the address."""
        async with TestClient(TestServer(build(told))) as client:
            response = await setup_as(client, host=f"127.0.0.1:{port_of(client)}", origin_header="https://wall.lan:8443")

        assert response.status == 303
        assert store.read(cache_dir).password is not None

    async def test_another_name_is_still_refused(self, told):
        async with TestClient(TestServer(build(told))) as client:
            assert (await client.get("/", headers={"Host": REBOUND})).status == server_module.HOST_REFUSED


class TestWhichOriginsMayPost:
    @pytest.mark.parametrize(
        "origin_header",
        [
            "http://127.0.0.1:{other}",
            "http://127.0.0.2:{port}",
            "https://127.0.0.1:{port}",
            "ftp://127.0.0.1:{port}",
            "http://127.0.0.1:{port}/setup",
            "http://user@127.0.0.1:{port}",
            "null",
            "",
        ],
    )
    async def test_an_origin_that_is_not_this_address_is_refused(self, page, cache_dir, origin_header):
        port = port_of(page)

        response = await setup_as(page, host=f"127.0.0.1:{port}", origin_header=origin_header.format(port=port, other=port + 1))

        assert response.status == 403
        assert store.read(cache_dir).password is None

    @pytest.mark.parametrize(
        ("host", "origin_header"), [("localhost", "http://localhost:80"), ("[::1]:{port}", "http://[::1]:{port}")]
    )
    async def test_the_requests_own_address_is_a_good_origin_in_any_spelling(self, page, cache_dir, host, origin_header):
        port = port_of(page)

        response = await setup_as(page, host=host.format(port=port), origin_header=origin_header.format(port=port))

        assert response.status == 303


# -- the throttle, through the login --------------------------------------------------------


@pytest.fixture
async def from_addresses(state: PageState):
    """The page, with each request coming from the address its `X-Peer` header names.

    A test client connects from one address only. This stands in for several
    machines on the LAN by setting what `request.remote` says, which is what
    the page throttles by; the page's own handlers are unchanged.
    """

    @web.middleware
    async def peer(request: web.Request, handler):
        return await handler(request.clone(remote=request.headers.get("X-Peer", request.remote)))

    app = build(state)
    app.middlewares.insert(0, peer)
    async with TestClient(TestServer(app)) as client:
        await set_up(client)
        yield client


async def login_from(client: TestClient, address: str, password: str):
    client.session.cookie_jar.clear()
    return await post(client, "/login", {"password": password}, **{"X-Peer": address})


class TestAGuesserCannotLockTheOwnerOut:
    async def test_wrong_passwords_from_one_address_do_not_delay_a_right_one_from_another(self, from_addresses):
        assert (await login_from(from_addresses, GUESSER, "wrong")).status == 403
        assert (await login_from(from_addresses, GUESSER, "wrong again")).status == 429

        owner = await login_from(from_addresses, OWNER, PASSWORD)

        assert owner.status == 303, "the owner waited on the guesser's throttle"

    async def test_the_guesser_is_still_throttled_after_the_owner_signs_in(self, from_addresses):
        await login_from(from_addresses, GUESSER, "wrong")
        await login_from(from_addresses, OWNER, PASSWORD)

        assert (await login_from(from_addresses, GUESSER, PASSWORD)).status == 429

    async def test_a_wrong_current_password_slows_only_its_own_address(self, from_addresses):
        def change(address: str, current: str):
            form = {"current": current, "password": "a new password", "again": "a new password"}
            return post(from_addresses, "/password", form, **{"X-Peer": address})

        assert (await change(GUESSER, "wrong")).status == 403
        assert (await change(GUESSER, PASSWORD)).status == 429
        assert (await change(OWNER, PASSWORD)).status == 303

    async def test_guessers_at_two_addresses_do_not_hold_out_an_owner_who_signed_in_before(self, from_addresses, state, clock):
        """The ceiling bounds a guesser with many addresses; two are enough to reach
        it, and from then on it held every address, the owner's included."""
        assert (await login_from(from_addresses, OWNER, PASSWORD)).status == 303
        _fill_the_ceiling_from_two_addresses(state.throttle, clock)
        assert (await login_from(from_addresses, NEWCOMER, PASSWORD)).status == 429

        owner = await login_from(from_addresses, OWNER, PASSWORD)

        assert owner.status == 303, "the owner was held out by guessers at two other addresses"

    async def test_after_a_restart_the_owners_signed_in_visit_restores_the_standing(self, from_addresses, state, clock):
        """A save restarts the Player, and the standing is in memory; the owner's cookie outlives both."""
        assert (await login_from(from_addresses, OWNER, PASSWORD)).status == 303
        state.throttle = auth.Throttle(monotonic=clock)
        assert (await from_addresses.get("/", headers={"X-Peer": OWNER}, allow_redirects=False)).status == 200
        _fill_the_ceiling_from_two_addresses(state.throttle, clock)

        assert (await login_from(from_addresses, OWNER, PASSWORD)).status == 303

    async def test_the_address_that_set_the_password_has_standing(self, from_addresses, state, clock):
        _fill_the_ceiling_from_two_addresses(state.throttle, clock)

        assert (await login_from(from_addresses, "127.0.0.1", PASSWORD)).status == 303

    async def test_a_password_changed_elsewhere_takes_the_owners_standing_with_it(self, from_addresses, state, clock):
        assert (await login_from(from_addresses, OWNER, PASSWORD)).status == 303
        from_addresses.session.cookie_jar.clear()
        assert (await login_from(from_addresses, NEWCOMER, PASSWORD)).status == 303
        form = {"current": PASSWORD, "password": "a new password", "again": "a new password"}
        assert (await post(from_addresses, "/password", form, **{"X-Peer": NEWCOMER})).status == 303
        _fill_the_ceiling_from_two_addresses(state.throttle, clock)

        assert (await login_from(from_addresses, OWNER, "a new password")).status == 429
        assert (await login_from(from_addresses, NEWCOMER, "a new password")).status == 303

    async def test_a_refused_login_names_its_address_in_the_journal(self, from_addresses, caplog):
        caplog.set_level(logging.WARNING)

        await login_from(from_addresses, GUESSER, "wrong")

        refused = [record for record in caplog.records if getattr(record, "event", None) == "settings_page.login_refused"]
        assert [record.remote for record in refused] == [GUESSER]


# -- the server's words ---------------------------------------------------------------------


class _VisibleText(HTMLParser):
    """The text a person sees: element content and the title, never an attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def visible(document: str) -> str:
    parser = _VisibleText()
    parser.feed(document)
    return " ".join(" ".join(parser.parts).split())


#: The word the server's vocabulary has retired for a Player, as a word: `CLIENT_TOKEN`, a key
#: for `.env`, is not one.
CLIENT_WORD = re.compile(r"\bclients?\b", re.IGNORECASE)


class TestThePageSpeaksTheServersWords:
    def test_screens_are_named_as_the_server_names_them(self):
        assert [screen_name(name) for name in ("hdmi-a-1", "hdmi-a-2", "frame", "dsi-1")] == [
            "HDMI 1",
            "HDMI 2",
            "Samsung Frame",
            "dsi-1",
        ]

    async def test_the_screen_picker_says_hdmi_1_and_never_the_connector(self, signed_in, state):
        state.outputs = lambda: [OutputReport("hdmi-a-1", "framebuffer", True, (1920, 1080))]

        document = await (await signed_in.get("/")).text()

        assert "HDMI 1 (a screen at 1920×1080)" in visible(document)
        assert "hdmi-a-1" not in visible(document)
        assert '<option value="hdmi-a-1" selected>' in document, "the saved value must stay the connector"

    async def test_the_fields_are_server_address_and_token(self, signed_in):
        document = await (await signed_in.get("/")).text()

        assert '<label for="server_url">Server address</label>' in document
        assert '<label for="client_token">Token</label>' in document
        assert "In .env, SERVER_URL." in visible(document)
        assert "In .env, CLIENT_TOKEN." in visible(document)

    async def test_no_visible_text_says_client(self, page, state, restarts):
        """Arrt's vocabulary names this a Player. Each page the Player serves, in each state it can be in."""
        documents = [await (await page.get("/")).text(), await (await page.get(f"/setup?code={CODE}")).text()]
        documents.append(await (await page.get("/", headers={"Host": REBOUND})).text())
        await set_up(page)
        documents.append(await (await page.get("/")).text())
        await save(page, server_url="http://arrt.example:8770", client_token=TOKEN)
        state.missing = ()
        documents.append(await (await page.get("/")).text())
        state.mode, state.slideshow = "slideshow", SlideshowStatus()
        documents.append(await (await page.get("/")).text())
        page.session.cookie_jar.clear()
        documents.append(await (await page.get("/login")).text())

        said = [CLIENT_WORD.findall(visible(document)) for document in documents]
        assert all(visible(document) for document in documents), "a page had no text, so this checks nothing"
        assert said == [[]] * len(documents)


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
    state.ui = dataclasses.replace(
        state.ui,
        pinned=frozenset(values),
        pinned_values={name: value for name, value in values.items() if name != "CLIENT_TOKEN"},
        environment={**state.ui.environment, **values},
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
    later login a server error, the right password's included, until a restart.

    Each guess is made once the last one's wait is over, as the page would hear them,
    so the run is one address's and the ceiling for all addresses never fills."""
    clock = Clock()
    throttle = auth.Throttle(monotonic=clock)

    for _ in range(2000):
        clock.now += throttle.wait_seconds(GUESSER)
        throttle.failed(GUESSER)

    assert throttle.wait_seconds(GUESSER) == auth.MAXIMUM_THROTTLE_SECONDS


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
        assert "the-cookies-secret-part" not in journal(caplog)
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

        assert '<option value="hdmi-a-1" selected>HDMI 1 (a screen at 3840×2160)</option>' in text
        assert '<option value="hdmi-a-2">HDMI 2 (no screen connected)</option>' in text

    async def test_a_chosen_screen_this_machine_does_not_have_is_still_offered_and_named(self, signed_in, restarts):
        await save_slideshow(signed_in, slideshow_output="hdmi-a-2")

        text = await (await signed_in.get("/")).text()

        assert '<option value="hdmi-a-2" selected>HDMI 2 (not found on this machine)</option>' in text

    async def test_the_mode_the_player_is_set_to_is_the_one_checked(self, signed_in, restarts):
        before = await (await signed_in.get("/")).text()
        await save_slideshow(signed_in)
        after = await (await signed_in.get("/")).text()

        assert 'value="arrt" checked' in before
        assert 'value="slideshow" checked' not in before
        assert 'value="slideshow" checked' in after
        assert 'value="arrt" checked' not in after


# -- the Frame's address, the server's name, and what a save says (Chunk 09) ---------------

#: The sun a Frame follows, without the Frame: what a Player with a Frame keeps in `.env`.
LOCATION = {key: value for key, value in FRAME.items() if key != "TV_ADDRESS"}
#: Two television addresses, from the range kept for documentation (RFC 5737).
OLD_SET, NEW_SET = "192.0.2.20", "192.0.2.21"
SERVER = "http://arrt.example:8770"


def give_location(state: PageState, **environment: str) -> None:
    """The Player started with a location in `.env`, and keeps its pairing token where the Frame would."""
    state.ui = dataclasses.replace(
        state.ui, environment={**LOCATION, **environment}, tv_token_file=state.ui.cache_dir / config.TV_TOKEN_FILENAME
    )


def paired(state: PageState) -> Path:
    """A pairing token on disk, as the library leaves one."""
    path = state.ui.tv_token_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("the-sets-token", encoding="utf-8")
    return path


def running(state: PageState, cache_dir: Path, values: dict[str, str]) -> None:
    """A Player out of setup mode, its page's file holding `values`."""
    stored = store.read(cache_dir)
    store.write(cache_dir, stored.with_values(values))
    state.missing = ()


def alert(document: str) -> str:
    """The page's problem paragraph, as a person reads it."""
    found = re.findall(r'<p class="note error" role="alert">(.*?)</p>', document)
    assert len(found) == 1, f"expected one problem paragraph, found {found}"
    return html.unescape(found[0])


def said(document: str) -> str:
    return visible(document)


def refusal_from_load(environment: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as raised:
        load(environment)
    return str(raised.value)


class TestTheFramesAddress:
    async def test_an_address_saved_here_is_the_one_the_next_start_gives_the_frame(self, signed_in, state, cache_dir, restarts):
        give_location(state)

        response = await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=f" {NEW_SET} ")

        assert response.status == 200
        assert load({"CACHE_DIR": str(cache_dir), **LOCATION}).frame.tv_address == NEW_SET
        assert '"version": 3' in store.path_in(cache_dir).read_text(encoding="utf-8")

    async def test_before_any_save_the_page_says_a_new_address_asks_that_set_to_pair(self, signed_in):
        document = await (await signed_in.get("/")).text()

        assert '<input id="tv_address" name="tv_address" value=""' in document
        assert "the television at the new address is asked to pair: expect a prompt on that set" in said(document)
        assert "forgets this Player's pairing with the set at the old one" in said(document)

    async def test_a_new_address_forgets_the_old_sets_pairing_and_says_so(self, signed_in, state, cache_dir, restarts):
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=OLD_SET)
        token = paired(state)

        response = await save(signed_in, server_url=SERVER, tv_address=NEW_SET)

        assert not token.exists(), "the old set's token would be sent to the new address"
        text = said(await response.text())
        assert f"The television at {NEW_SET} will be asked to pair as the Player starts again" in text
        assert f"The pairing with the television at {OLD_SET} is forgotten." in text

    async def test_clearing_the_address_forgets_the_pairing_and_says_there_is_no_frame(
        self, signed_in, state, cache_dir, restarts
    ):
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=OLD_SET)
        token = paired(state)

        response = await save(signed_in, server_url=SERVER, tv_address="")

        assert not token.exists()
        assert "TV_ADDRESS" not in store.read(cache_dir).values
        assert "This Player no longer drives a Frame" in said(await response.text())
        assert '"version": 2' in store.path_in(cache_dir).read_text(encoding="utf-8")

    async def test_an_address_set_beside_a_token_kept_from_before_forgets_it(self, signed_in, state, restarts):
        """A token whose set the page never knew (an address once in `.env`) may be any set's."""
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN)
        token = paired(state)

        response = await save(signed_in, server_url=SERVER, tv_address=NEW_SET)

        assert not token.exists()
        assert "The pairing kept from an earlier television is forgotten." in said(await response.text())

    async def test_the_same_address_saved_again_keeps_the_pairing(self, signed_in, state, cache_dir, restarts):
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=OLD_SET)
        token = paired(state)

        response = await save(signed_in, server_url=SERVER, tv_address=OLD_SET, player_name="Hall")

        assert token.exists()
        assert store.read(cache_dir).values["PLAYER_NAME"] == "Hall"
        assert "pair" not in said(await response.text())

    async def test_a_refused_save_keeps_the_pairing(self, signed_in, state, cache_dir, restarts):
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=OLD_SET)
        token = paired(state)

        response = await save(signed_in, server_url=SERVER, tv_address=REFUSED_TV_ADDRESSES[0])

        assert response.status == 400
        assert token.exists()
        assert store.read(cache_dir).values["TV_ADDRESS"] == OLD_SET

    async def test_a_token_that_will_not_go_saves_nothing(self, signed_in, state, cache_dir, restarts, caplog):
        """The new address is not written while the old set's token is still where the Frame reads it."""
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=OLD_SET)
        await settled()
        state.ui.tv_token_file.mkdir()  # a path `unlink` refuses, as a read-only directory would

        response = await save(signed_in, server_url=SERVER, tv_address=NEW_SET)
        await settled()

        assert response.status == 500
        assert "could not be forgotten" in alert(await response.text())
        assert store.read(cache_dir).values["TV_ADDRESS"] == OLD_SET
        assert restarts.count == 1, "a save that saved nothing restarted the Player"
        assert any(getattr(record, "event", None) == "settings_page.pairing_not_forgotten" for record in caplog.records)

    async def test_a_file_that_will_not_write_once_the_pairing_is_forgotten_says_the_set_will_ask_again(
        self, signed_in, state, cache_dir, restarts, monkeypatch
    ):
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=OLD_SET)
        paired(state)

        def refuse(*_args):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(store, "write", refuse)
        response = await save(signed_in, server_url=SERVER, tv_address=NEW_SET)

        assert response.status == 500
        assert "already forgotten, so it will ask to pair again" in alert(await response.text())

    @pytest.mark.parametrize("address", REFUSED_TV_ADDRESSES)
    async def test_each_address_a_start_refuses_is_refused_here_in_the_same_words(
        self, signed_in, state, cache_dir, restarts, address
    ):
        give_location(state)
        words = refusal_from_load(an_environment(cache_dir, TV_ADDRESS=address))

        response = await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=address)
        await settled()

        assert response.status == 400
        assert alert(await response.text()) == words
        assert store.read(cache_dir).values == {}
        assert restarts.count == 0

    async def test_an_address_without_the_sun_a_frame_follows_is_refused_in_the_words_of_a_start(
        self, signed_in, state, cache_dir, restarts
    ):
        """No field is wrong; together they would stop the Player, which is the case only the one validator sees."""
        environment = an_environment(cache_dir, frame=False, TV_ADDRESS=NEW_SET)
        words = refusal_from_load(environment)

        response = await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=NEW_SET)
        await settled()

        assert response.status == 400
        assert words in alert(await response.text())
        assert "LATITUDE" in words
        assert store.read(cache_dir).values == {}
        assert restarts.count == 0

    async def test_an_address_on_a_player_drawing_to_files_is_refused_by_that_rule(
        self, signed_in, state, cache_dir, restarts, tmp_path
    ):
        """`ClientSettings.files` drives no hardware, and refuses `TV_ADDRESS` beside it: from the page as from `.env`."""
        files = {"FILE_OUTPUT_DIR": str(tmp_path / "drawn")}
        give_location(state, **files)
        words = refusal_from_load(an_environment(cache_dir, TV_ADDRESS=NEW_SET, **files))

        shown = said(await (await signed_in.get("/")).text())
        response = await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=NEW_SET)

        assert "This Player draws to files (FILE_OUTPUT_DIR in .env), so it drives no television" in shown
        assert response.status == 400
        assert "FILE_OUTPUT_DIR is set beside TV_ADDRESS" in words
        assert words in alert(await response.text())
        assert store.read(cache_dir).values == {}

    @pytest.mark.parametrize(
        ("headers", "status"),
        [({"Host": "rebound.example"}, server_module.HOST_REFUSED), ({"Origin": "http://elsewhere.example"}, 403)],
    )
    async def test_a_form_carrying_an_address_goes_through_the_host_and_origin_checks(
        self, signed_in, state, cache_dir, restarts, headers, status
    ):
        give_location(state)
        form = {"server_url": SERVER, "client_token": TOKEN, "player_name": "", "tv_address": NEW_SET}

        response = await post(signed_in, "/settings", form, **headers)

        assert response.status == status
        assert store.read(cache_dir).values == {}
        assert restarts.count == 0


class TestWhereAValueComesFrom:
    """One rule (`config._beneath_the_environment`): `.env` wins, the page fills what it leaves empty. Both orders."""

    async def test_an_address_env_sets_is_shown_read_only_and_says_which_of_the_pages_it_overrides(
        self, signed_in, state, cache_dir, restarts
    ):
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=OLD_SET)
        pin(state, TV_ADDRESS=NEW_SET)

        document = await (await signed_in.get("/")).text()
        await save(signed_in, server_url=SERVER, tv_address="192.0.2.99")

        assert f'name="tv_address" value="{NEW_SET}" disabled' in document
        assert f"This page holds '{OLD_SET}' for it too, which is not used while .env sets it." in said(document)
        assert "tv_address-pairing" not in document, "a save here changes no pinned address, so it forgets no pairing"
        assert store.read(cache_dir).values["TV_ADDRESS"] == OLD_SET
        assert load({**an_environment(cache_dir), "TV_ADDRESS": NEW_SET}).frame.tv_address == NEW_SET

    async def test_the_pages_address_with_nothing_in_env_is_shown_editable_and_used(self, signed_in, state, cache_dir, restarts):
        give_location(state)
        await save(signed_in, server_url=SERVER, client_token=TOKEN, tv_address=OLD_SET)

        document = await (await signed_in.get("/")).text()

        assert f'name="tv_address" value="{OLD_SET}" aria-describedby' in document
        assert "not used while .env sets it" not in said(document)
        assert load({"CACHE_DIR": str(cache_dir), **LOCATION}).frame.tv_address == OLD_SET

    async def test_a_value_env_pins_and_the_page_agrees_with_says_nothing_of_an_override(self, signed_in, state, cache_dir):
        await save(signed_in, player_name="Hall")
        pin(state, PLAYER_NAME="Hall")

        assert "not used while .env sets it" not in said(await (await signed_in.get("/")).text())

    async def test_a_secret_the_page_holds_under_an_env_one_is_never_shown(self, signed_in, state, cache_dir, restarts):
        await save(signed_in, server_url=SERVER, client_token="the-pages-own-token")
        pin(state, CLIENT_TOKEN=TOKEN)

        document = await (await signed_in.get("/")).text()

        assert "the-pages-own-token" not in document
        assert TOKEN not in document
        assert "This page holds a value for it too" in said(document)


class TestWhatASaveSays:
    async def test_it_names_what_changed_in_the_fields_words_and_that_the_player_restarts(self, signed_in, restarts):
        response = await save(signed_in, server_url=SERVER, client_token=TOKEN)
        await settled()

        text = said(await response.text())
        assert "Saved: Server address and Token." in text
        assert "The Player is restarting onto them. Under systemd it is back in a few seconds" in text
        assert restarts.count == 1

    async def test_a_save_that_changes_nothing_says_nothing_restarts(self, signed_in, restarts):
        await save(signed_in, player_name="Hall")

        response = await save(signed_in, player_name="Hall")

        assert "Nothing changed, so nothing was saved and nothing restarts." in said(await response.text())

    async def test_slideshow_fields_saved_in_arrt_mode_say_it_stays_in_arrt_mode_and_restart_nothing(
        self, signed_in, state, cache_dir, restarts, caplog
    ):
        """#46: the owner saved a listing with the mode still on Arrt, and the page let it look as if it took."""
        running(state, cache_dir, {"SERVER_URL": SERVER, "CLIENT_TOKEN": TOKEN})
        caplog.set_level(logging.INFO)

        response = await save_slideshow(signed_in, server_url=SERVER, player_mode="arrt")
        await settled()

        text = said(await response.text())
        assert "Saved: Listing address." in text, "the choice and the menu posted as they were are no change"
        assert "Nothing restarts. The Player stays in Arrt mode, showing walls from its server" in text
        assert "wait until the mode is A Reddit slideshow" in text
        assert restarts.count == 0
        assert store.read(cache_dir).values["SLIDESHOW_URL"]
        saved = [record for record in caplog.records if getattr(record, "event", None) == "settings_page.saved"]
        assert [record.restarting for record in saved] == [False]

    async def test_in_setup_mode_they_restart_and_still_say_the_mode_did_not_change(self, signed_in, restarts):
        response = await save_slideshow(signed_in, player_mode="arrt")
        await settled()

        assert "The Player stays in Arrt mode" in said(await response.text())
        assert restarts.count == 1

    async def test_a_running_slideshows_own_settings_restart_it(self, signed_in, state, cache_dir, restarts):
        """The entry point gives a running slideshow's page its status and leaves `mode` at its default."""
        running(
            state, cache_dir, {"PLAYER_MODE": "slideshow", "SLIDESHOW_URL": LISTING_EXAMPLE, "REDDIT_SESSION": a_cookie(None)}
        )
        state.slideshow = SlideshowStatus()

        response = await save_slideshow(signed_in, slideshow_url="https://www.reddit.com/r/EarthPorn/top/?t=year")
        await settled()

        text = said(await response.text())
        assert restarts.count == 1
        assert "The Player is restarting onto them." in text, "a slideshow told it restarts into the mode it is in"
        assert "The Player stays" not in text

    async def test_a_running_slideshow_saving_the_servers_settings_restarts_nothing(self, signed_in, state, cache_dir, restarts):
        running(
            state, cache_dir, {"PLAYER_MODE": "slideshow", "SLIDESHOW_URL": LISTING_EXAMPLE, "REDDIT_SESSION": a_cookie(None)}
        )
        state.slideshow = SlideshowStatus()

        response = await save_slideshow(signed_in, slideshow_url=LISTING_EXAMPLE, server_url=SERVER, client_token=TOKEN)
        await settled()

        assert "Nothing restarts. The Player stays a Reddit slideshow" in said(await response.text())
        assert restarts.count == 0

    async def test_a_switch_of_mode_says_the_mode_it_restarts_into(self, signed_in, restarts):
        response = await save_slideshow(signed_in, reddit_session=a_cookie(None))

        assert "The Player is restarting as a Reddit slideshow." in said(await response.text())

    async def test_it_names_the_walls_that_start_again(self, signed_in, state, cache_dir, restarts):
        running(state, cache_dir, {"SERVER_URL": SERVER, "CLIENT_TOKEN": TOKEN})
        document = {
            "client_id": "c1",
            "name": "Lounge",
            "walls": [
                {"wall_id": "w1", "name": "Hall", "output": "hdmi-a-1"},
                {"wall_id": "w2", "name": "Office", "output": "frame"},
            ],
        }
        (cache_dir / CLIENT_DOCUMENT_FILENAME).write_text(json.dumps(document), encoding="utf-8")

        response = await save(signed_in, server_url=SERVER, client_token="a-new-token")

        assert "and with it every wall it shows: Hall and Office." in said(await response.text())

    async def test_a_save_that_leaves_setup_mode_short_says_what_is_still_missing(self, signed_in, restarts):
        response = await save(signed_in, server_url=SERVER)

        assert "Until Token is set, it serves only this page." in said(await response.text())


class TestTheNameTheServerGives:
    """audit-5 F7a: once paired, the name is the server's, shown read-only with where to change it."""

    def given(self, cache_dir: Path, name: str = "Lounge") -> None:
        (cache_dir / CLIENT_DOCUMENT_FILENAME).write_text(
            json.dumps({"client_id": "c1", "name": name, "walls": []}), encoding="utf-8"
        )

    async def test_with_a_cached_document_it_is_the_servers_read_only_with_where_to_rename_it(
        self, signed_in, cache_dir, restarts
    ):
        await save(signed_in, player_name="Our name")
        self.given(cache_dir)

        document = await (await signed_in.get("/")).text()

        assert 'name="player_name" value="Lounge" disabled' in document
        assert "To rename it, rename it on the server, under Settings › Players." in said(document)
        assert "Its own name, used whenever the server's cannot be read, is Our name." in said(document)

    async def test_without_one_it_is_the_players_own_and_editable(self, signed_in, restarts):
        await save(signed_in, player_name="Our name")

        document = await (await signed_in.get("/")).text()

        assert 'name="player_name" value="Our name" aria-describedby' in document
        assert "Settings › Players" not in said(document)

    async def test_a_post_cannot_change_the_players_own_name_while_the_server_names_it(self, signed_in, cache_dir, restarts):
        await save(signed_in, player_name="Our name")
        self.given(cache_dir)

        await save(signed_in, player_name="Changed")

        assert store.read(cache_dir).values["PLAYER_NAME"] == "Our name"

    async def test_a_form_without_the_name_leaves_it_as_it_is(self, signed_in, cache_dir, restarts):
        await save(signed_in, player_name="Our name")

        await post(signed_in, "/settings", {"server_url": SERVER, "client_token": TOKEN})

        assert store.read(cache_dir).values["PLAYER_NAME"] == "Our name"


class _Nesting(HTMLParser):
    """Where the page's one save sits: the elements open around the button its caption describes."""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[tuple[str, dict[str, str | None]]] = []
        self.around: list[tuple[str, dict[str, str | None]]] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "input":
            return
        named = dict(attrs)
        if tag == "button" and named.get("aria-describedby") == "act-caption":
            self.around = list(self.open)
        self.open.append((tag, named))

    def handle_endtag(self, tag: str) -> None:
        while self.open and self.open.pop()[0] != tag:
            pass


class TestThePagesOneSave:
    """#46: the button sat inside "This Player", so it read as that section's, while it saved every one."""

    async def test_it_is_the_settings_forms_and_inside_no_section(self, signed_in):
        parser = _Nesting()
        document = await (await signed_in.get("/")).text()
        parser.feed(document)

        assert parser.around is not None, "the page has no save"
        assert [tag for tag, _ in parser.around if tag == "section"] == []
        assert ("form", {"method": "post", "action": "/settings"}) in parser.around
        assert "Saves every section above." in said(document)

    async def test_without_a_cookie_the_slideshow_section_says_so_plainly(self, signed_in):
        hint = re.search(r'id="reddit_session-hint">(.*?)</p>', await (await signed_in.get("/")).text()).group(1)

        assert "No cookie is set. A slideshow cannot play without one: paste it here." in html.unescape(hint)
        assert "keep the current one" not in hint

    async def test_without_a_token_the_field_does_not_offer_to_keep_one(self, signed_in):
        hint = re.search(r'id="client_token-hint">(.*?)</p>', await (await signed_in.get("/")).text()).group(1)

        assert "No token is set." in hint
        assert "keep the current one" not in hint


class TestThePairingLabels:
    """The server's dialog and this page say "Server address" and "Token" alike; here they are said in one place."""

    async def test_the_fields_are_labelled_from_the_one_place(self, signed_in, monkeypatch):
        monkeypatch.setattr(
            server_module, "PAIRING_LABELS", {"SERVER_URL": "The server's words", "CLIENT_TOKEN": "Its other words"}
        )

        document = await (await signed_in.get("/")).text()

        assert '<label for="server_url">The server&#x27;s words</label>' in document
        assert '<label for="client_token">Its other words</label>' in document

    def test_the_labels_are_written_nowhere_else_in_the_page(self):
        source = Path(server_module.__file__).read_text(encoding="utf-8")

        assert [source.count(f'"{label}"') for label in server_module.PAIRING_LABELS.values()] == [1, 1]


#: A listing Reddit's slideshow takes, the test's own.
LISTING_EXAMPLE = "https://www.reddit.com/r/EarthPorn/top/?t=month"

#: The `*_SECONDS` keys the page writes, from the configuration's own scan and the page's own keys.
PAGE_SECONDS_KEYS = sorted(set(SECONDS_KEYS) & set(store.MANAGED_KEYS))


class TestTheSecondsThePageWrites:
    def test_the_page_writes_some(self):
        assert PAGE_SECONDS_KEYS == ["SLIDESHOW_MAX_SECONDS", "SLIDESHOW_MIN_SECONDS"]

    @pytest.mark.parametrize("value", REFUSED_SECONDS)
    @pytest.mark.parametrize("key", PAGE_SECONDS_KEYS)
    async def test_each_value_a_start_refuses_is_refused_here_with_its_reason(self, signed_in, cache_dir, restarts, key, value):
        words = refusal_from_load(a_slideshow(cache_dir, **{key: value}))

        response = await save_slideshow(signed_in, reddit_session=a_cookie(None), **{server_module._SLIDESHOW_FIELDS[key]: value})
        await settled()

        assert response.status == 400
        assert alert(await response.text()) in words, "the page's reason is not the one a start gives"
        assert store.read(cache_dir).values == {}
        assert restarts.count == 0
