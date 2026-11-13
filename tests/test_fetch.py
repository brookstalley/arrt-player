"""The slideshow's network client: where it may go, what it carries, and what it keeps.

A local server stands in for the internet. Names resolve through a fake
resolver, so `public.test` can be a "public" host at 127.0.0.1 and
`private.test` a name for a LAN address, and the address policy admits exactly
127.0.0.1 beside the real public ranges. Everything else is the shipped code.
"""

import ipaddress
import logging
import socket
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestServer

from arrt_player.sources import Kind
from arrt_player.sources.fetch import (
    API_HOSTS,
    MAX_REDIRECTS,
    PARTIAL_SUFFIX,
    REDDIT_HOST,
    AddressRefused,
    BadAnswer,
    Fetcher,
    FetchRefused,
    NotMedia,
    TooLarge,
    is_public_address,
    sniff,
)

LOOPBACK = ipaddress.ip_address("127.0.0.1")
# prawduct:allow prawduct/credential-shape -- a made-up cookie in a JWT's shape; it signs nothing and Reddit never saw it
COOKIE = "eyJhbGciOiJSUzI1NiJ9.c2VjcmV0LWNvb2tpZS12YWx1ZQ.sig-zzz"

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 500
MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00" + b"\x00" * 500
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

NAMES: dict[str, str] = {
    "public.test": "127.0.0.1",
    "other.test": "127.0.0.1",
    "www.reddit.com": "127.0.0.1",
    "api.redgifs.com": "127.0.0.1",
    "media.reddit.com": "127.0.0.1",
    "private.test": "192.168.1.10",
    "rebinding.test": "10.0.0.7",
}


def _test_policy(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address == LOOPBACK or is_public_address(address)


class FakeResolver(AbstractResolver):
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        self.asked.append(host)
        if host not in NAMES:
            raise OSError(f"no such name {host}")
        return [{"hostname": host, "host": NAMES[host], "port": port, "family": socket.AF_INET, "proto": 0, "flags": 0}]

    async def close(self) -> None:
        pass


def _h(make):
    """An aiohttp handler from a function of the request, since bare functions are deprecated."""

    async def handler(request: web.Request) -> web.StreamResponse:
        return make(request)

    return handler


class Internet:
    """The local server, recording each request's Host and Cookie."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None, str]] = []
        self.authorizations: list[str | None] = []
        self.port = 0

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._record])
        app.router.add_get("/still", _h(lambda r: web.Response(body=JPEG, content_type="image/jpeg")))
        app.router.add_get("/clip", _h(lambda r: web.Response(body=MP4, content_type="video/mp4")))
        app.router.add_get("/png-as-jpeg", _h(lambda r: web.Response(body=PNG, content_type="image/jpeg")))
        app.router.add_get("/page", _h(lambda r: web.Response(text="<html>a page</html>", content_type="text/html")))
        app.router.add_get(
            "/lie", _h(lambda r: web.Response(body=b"<html>not a picture at all</html>", content_type="image/jpeg"))
        )
        app.router.add_get("/tiny", _h(lambda r: web.Response(body=b"\xff\xd8", content_type="image/jpeg")))
        app.router.add_get("/big", _h(lambda r: web.Response(body=JPEG + b"\x00" * 5000, content_type="image/jpeg")))
        app.router.add_get("/jpeg-as-html", _h(lambda r: web.Response(body=JPEG, content_type="text/html")))
        app.router.add_get("/endless", self._endless)
        app.router.add_get("/endless-page", self._endless_page)
        app.router.add_get("/hop/{n}", self._hop)
        app.router.add_get("/to/{where}", self._to)
        app.router.add_get("/api", self._api)
        app.router.add_get(
            "/login", _h(lambda r: web.Response(status=302, headers={"Location": "https://www.reddit.com/login/"}))
        )
        return app

    @web.middleware
    async def _record(self, request: web.Request, handler):
        self.seen.append((request.host.split(":")[0], request.headers.get("Cookie"), request.path))
        self.authorizations.append(request.headers.get("Authorization"))
        return await handler(request)

    async def _endless(self, request: web.Request) -> web.StreamResponse:
        """No Content-Length, so the cap has to stop it partway."""
        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        await response.prepare(request)
        await response.write(MP4)
        for _ in range(100):
            await response.write(b"\x00" * 4096)
        return response

    async def _api(self, request: web.Request) -> web.Response:
        """Sets a cookie for the whole domain, as Reddit does, which must not travel to another of its hosts."""
        response = web.json_response({"ok": True}, headers={"x-ratelimit-reset": "187"})
        response.set_cookie("tracker", "set-by-the-api", domain="reddit.com")
        return response

    async def _endless_page(self, request: web.Request) -> web.StreamResponse:
        """Claims to be a clip and is a page: it should be dropped at its first bytes, not read to the cap."""
        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        await response.prepare(request)
        for _ in range(100):
            await response.write(b"<html>" + b"x" * 4090)
        return response

    async def _hop(self, request: web.Request) -> web.Response:
        n = int(request.match_info["n"])
        if n == 0:
            return web.Response(body=JPEG, content_type="image/jpeg")
        return web.Response(status=302, headers={"Location": f"/hop/{n - 1}"})

    async def _to(self, request: web.Request) -> web.Response:
        targets = {
            "lan-address": "http://10.0.0.1/still",
            "lan-name": f"http://private.test:{self.port}/still",
            "other-host": f"http://other.test:{self.port}/still",
            "file": "file:///etc/passwd",
            "ftp": f"ftp://public.test:{self.port}/still",
        }
        return web.Response(status=302, headers={"Location": targets[request.match_info["where"]]})


@pytest.fixture
async def internet():
    world = Internet()
    server = TestServer(world.app())
    await server.start_server()
    world.port = server.port
    yield world
    await server.close()


def _fetcher(**kwargs) -> Fetcher:
    return Fetcher(allowed=_test_policy, inner_resolver=FakeResolver(), api_schemes=("http",), **kwargs)


def _url(internet: Internet, path: str, host: str = "public.test") -> str:
    return f"http://{host}:{internet.port}{path}"


# -- the address guard ----------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104 -- an address refused, not one bound
        "255.255.255.255",
        "224.0.0.251",
        "192.0.2.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:192.168.1.1",
        "64:ff9b::c0a8:101",
        "2002:c0a8:101::1",
    ],
)
def test_a_private_or_reserved_address_is_not_public(address):
    assert not is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "151.101.1.140", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"])
def test_a_public_address_is(address):
    assert is_public_address(ipaddress.ip_address(address))


async def test_a_public_host_is_downloaded(internet, tmp_path):
    async with _fetcher() as fetcher:
        got = await fetcher.download(_url(internet, "/still"), tmp_path / "item")

    assert got.path == tmp_path / "item.jpg"
    assert got.path.read_bytes() == JPEG
    assert got.kind is Kind.STILL
    assert got.size == len(JPEG)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/still",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[::ffff:192.168.1.1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.1/",
        "http://2130706433/",
    ],
)
async def test_an_address_in_the_url_that_is_not_public_is_refused(url, tmp_path):
    """Checked before aiohttp, which does not ask the resolver about an address in the URL."""
    resolver = FakeResolver()
    async with Fetcher(allowed=_test_policy, inner_resolver=resolver) as fetcher:
        with pytest.raises((AddressRefused, aiohttp.InvalidURL)):
            await fetcher.download(url, tmp_path / "item")

    assert list(tmp_path.iterdir()) == []


async def test_a_public_name_for_a_private_address_is_refused_at_connection(internet, tmp_path):
    async with _fetcher() as fetcher:
        with pytest.raises(AddressRefused):
            await fetcher.download(_url(internet, "/still", host="private.test"), tmp_path / "item")

    assert [host for host, _, _ in internet.seen] == []


@pytest.mark.parametrize("where", ["lan-address", "lan-name"])
async def test_a_redirect_from_a_public_host_to_a_private_one_is_refused(internet, tmp_path, where):
    async with _fetcher() as fetcher:
        with pytest.raises(AddressRefused):
            await fetcher.download(_url(internet, f"/to/{where}"), tmp_path / "item")

    assert [path for _, _, path in internet.seen] == [f"/to/{where}"]
    assert list(tmp_path.iterdir()) == []


async def test_a_redirect_to_another_public_host_is_followed(internet, tmp_path):
    async with _fetcher() as fetcher:
        got = await fetcher.download(_url(internet, "/to/other-host"), tmp_path / "item")

    assert got.kind is Kind.STILL
    assert [host for host, _, _ in internet.seen] == ["public.test", "other.test"]


@pytest.mark.parametrize("where", ["file", "ftp"])
async def test_a_redirect_to_another_scheme_is_refused(internet, tmp_path, where):
    async with _fetcher() as fetcher:
        with pytest.raises(BadAnswer, match="http or https"):
            await fetcher.download(_url(internet, f"/to/{where}"), tmp_path / "item")


async def test_redirects_are_followed_up_to_the_limit_and_no_further(internet, tmp_path):
    async with _fetcher() as fetcher:
        got = await fetcher.download(_url(internet, f"/hop/{MAX_REDIRECTS}"), tmp_path / "item")
        assert got.kind is Kind.STILL
        with pytest.raises(BadAnswer, match="redirects"):
            await fetcher.download(_url(internet, f"/hop/{MAX_REDIRECTS + 1}"), tmp_path / "again")


# -- what a download keeps ------------------------------------------------


async def test_a_clip_is_kept_as_a_clip(internet, tmp_path):
    async with _fetcher() as fetcher:
        got = await fetcher.download(_url(internet, "/clip"), tmp_path / "item")

    assert (got.kind, got.path.name) == (Kind.CLIP, "item.mp4")


async def test_the_bytes_decide_the_format_not_the_header(internet, tmp_path):
    async with _fetcher() as fetcher:
        got = await fetcher.download(_url(internet, "/png-as-jpeg"), tmp_path / "item")

    assert got.path.name == "item.png"


@pytest.mark.parametrize("path", ["/page", "/lie", "/tiny", "/jpeg-as-html"])
async def test_what_is_not_a_picture_or_a_clip_is_not_kept(internet, tmp_path, path):
    async with _fetcher() as fetcher:
        with pytest.raises(NotMedia):
            await fetcher.download(_url(internet, path), tmp_path / "item")

    assert list(tmp_path.iterdir()) == []


async def test_a_file_over_the_limit_is_refused_by_its_declared_length(internet, tmp_path):
    async with _fetcher() as fetcher:
        with pytest.raises(TooLarge, match="offers"):
            await fetcher.download(_url(internet, "/big"), tmp_path / "item", max_bytes=1000)

    assert list(tmp_path.iterdir()) == []


async def test_a_file_over_the_limit_is_stopped_partway_and_leaves_nothing(internet, tmp_path):
    async with _fetcher() as fetcher:
        with pytest.raises(TooLarge, match="more than"):
            await fetcher.download(_url(internet, "/endless"), tmp_path / "item", max_bytes=20_000)

    assert list(tmp_path.iterdir()) == []


async def test_what_is_not_media_is_dropped_at_its_first_bytes_not_read_to_the_limit(internet, tmp_path):
    async with _fetcher() as fetcher:
        with pytest.raises(NotMedia):
            await fetcher.download(_url(internet, "/endless-page"), tmp_path / "item", max_bytes=20_000)


async def test_a_partial_file_never_has_the_final_name(internet, tmp_path, monkeypatch):
    """What mpv is given only ever exists whole: the bytes land beside it first."""
    written: list[str] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        written.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    async with _fetcher() as fetcher:
        await fetcher.download(_url(internet, "/clip"), tmp_path / "item")

    assert written == ["item" + PARTIAL_SUFFIX]


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (JPEG[:12], ("jpg", Kind.STILL)),
        (PNG[:12], ("png", Kind.STILL)),
        (b"RIFF\x00\x00\x00\x00WEBP", ("webp", Kind.STILL)),
        (b"GIF89a\x01\x00\x01\x00\x00\x00", ("gif", Kind.CLIP)),
        (MP4[:12], ("mp4", Kind.CLIP)),
        (b"\x00\x00\x00\x1cftypavif", ("avif", Kind.STILL)),
        (b"\x1a\x45\xdf\xa3\x00\x00\x00\x00\x00\x00\x00\x00", ("webm", Kind.CLIP)),
    ],
)
def test_sniff_reads_each_format_shown(head, expected):
    found = sniff(head)
    assert found is not None
    assert (found.extension, found.kind) == expected


@pytest.mark.parametrize("head", [b"<html><body>", b"%PDF-1.7\n\x00\x00\x00", b"\x00\x00\x00\x18ftypheic", b""])
def test_sniff_reads_nothing_else(head):
    assert sniff(head) is None


# -- listing and API calls, and the cookie -------------------------------


async def test_the_cookie_goes_to_reddit_and_to_no_other_host(internet, tmp_path):
    async with _fetcher(cookie=COOKIE) as fetcher:
        await fetcher.get_api(_url(internet, "/api", host=REDDIT_HOST))
        await fetcher.get_api(_url(internet, "/api", host="api.redgifs.com"))
        await fetcher.download(_url(internet, "/still", host=REDDIT_HOST), tmp_path / "a")
        await fetcher.download(_url(internet, "/still"), tmp_path / "b")
        await fetcher.download(_url(internet, "/to/other-host"), tmp_path / "c")
        await fetcher.download(_url(internet, "/still", host="media.reddit.com"), tmp_path / "d")

    cookies = [(host, path, cookie) for host, cookie, path in internet.seen]
    assert cookies[0] == (REDDIT_HOST, "/api", f"reddit_session={COOKIE}")
    assert all(cookie is None for _, _, cookie in cookies[1:]), cookies


async def test_an_api_call_follows_no_redirect(internet):
    """Reddit sends a session it no longer accepts to log in; that is the answer, not somewhere to go."""
    async with _fetcher(cookie=COOKIE) as fetcher:
        answer = await fetcher.get_api(_url(internet, "/login", host=REDDIT_HOST))

    assert (answer.status, answer.location) == (302, "https://www.reddit.com/login/")
    assert [path for _, _, path in internet.seen] == ["/login"]


async def test_an_api_answer_carries_the_rate_limit_reset(internet):
    async with _fetcher() as fetcher:
        answer = await fetcher.get_api(_url(internet, "/api", host=REDDIT_HOST))

    assert (answer.status, answer.ratelimit_reset_s) == (200, 187.0)


@pytest.mark.parametrize("url", ["http://public.test/api", "http://reddit.com/api", "http://old.reddit.com/r/x.json"])
async def test_an_api_call_to_a_host_outside_the_set_is_refused(url):
    async with _fetcher(cookie=COOKIE) as fetcher:
        with pytest.raises(FetchRefused):
            await fetcher.get_api(url)


async def test_an_api_call_is_https_only_by_default():
    async with Fetcher(cookie=COOKIE, inner_resolver=FakeResolver()) as fetcher:
        with pytest.raises(FetchRefused, match="https"):
            await fetcher.get_api("http://www.reddit.com/r/pics/top.json")


def test_the_api_hosts_are_reddit_and_redgifs():
    assert {"www.reddit.com", "api.redgifs.com"} == API_HOSTS


def test_the_cookie_is_not_in_the_repr():
    assert COOKIE not in repr(Fetcher(cookie=COOKIE))


@pytest.mark.parametrize("bad", ["", "a b", "a;b", "a\r\nX-Evil: 1"])
def test_a_cookie_that_would_break_the_header_is_refused_without_echoing_it(bad):
    with pytest.raises(ValueError, match="the value alone") as caught:
        Fetcher(cookie=bad)

    assert not bad or bad not in str(caught.value)


async def test_the_cookie_is_in_no_log_record(internet, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with _fetcher(cookie=COOKIE) as fetcher:
        await fetcher.get_api(_url(internet, "/api", host=REDDIT_HOST))
        await fetcher.get_api(_url(internet, "/login", host=REDDIT_HOST))
        with pytest.raises(FetchRefused):
            await fetcher.get_api("http://elsewhere.test/api")
        await fetcher.download(_url(internet, "/still"), tmp_path / "a")

    assert caplog.records, "nothing logged at DEBUG, so this checks nothing"
    assert all(COOKIE not in record.getMessage() for record in caplog.records)


# -- through the cache, which is what reads these errors --------------------------


async def test_a_name_for_a_lan_address_is_a_refused_item_in_the_cache_not_a_network_down(internet, tmp_path):
    """The cache tells a broken link from a dead network by the error's type, so the real one must arrive.

    Three refused items in a row: read as unreachable they would mark the network
    down and stall every fill on them; read as refused, each waits out its retry
    and the fill goes on to the item after them.
    """
    from arrt_player.sources import Caption, Item
    from arrt_player.sources.cache import RETRY_AFTER_S, MediaCache

    caption = Caption(title="t", community="r/x", author="u/y")
    lan = [
        Item(
            id=f"t3_lan{n}",
            media_url=_url(internet, f"/still?{n}", host="private.test"),
            kind=Kind.STILL,
            caption=caption,
            tier="direct",
        )
        for n in range(3)
    ]
    good = Item(id="t3_good", media_url=_url(internet, "/still"), kind=Kind.STILL, caption=caption, tier="direct")
    now = [5000.0]
    async with _fetcher() as fetcher:
        cache = MediaCache(tmp_path / "cache", fetcher, clock=lambda: now[0])
        report = await cache.fill([*lan, good])
        assert not report.network_down
        assert sorted(report.failed) == ["t3_lan0", "t3_lan1", "t3_lan2"]
        assert all("public address" in reason for reason in report.failed.values()), report.failed
        assert report.downloaded == ["t3_good"]

        now[0] += RETRY_AFTER_S - 13
        again = await cache.fill([*lan, good])
        assert (again.failed, again.downloaded) == ({}, [])


@pytest.mark.parametrize("name", ["Cookie", "cookie", "COOKIE"])
async def test_a_caller_cannot_send_a_cookie_header_of_its_own(internet, name):
    async with _fetcher(cookie=COOKIE) as fetcher:
        with pytest.raises(FetchRefused, match="Cookie"):
            await fetcher.get_api(_url(internet, "/api", host="api.redgifs.com"), headers={name: f"reddit_session={COOKIE}"})

    assert internet.seen == []


async def test_a_callers_headers_reach_the_api_host_beside_no_cookie(internet):
    async with _fetcher(cookie=COOKIE) as fetcher:
        await fetcher.get_api(_url(internet, "/api", host="api.redgifs.com"), headers={"Authorization": "Bearer abc"})

    assert internet.seen == [("api.redgifs.com", None, "/api")]
    assert internet.authorizations == ["Bearer abc"]
