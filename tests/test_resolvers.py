"""Host resolvers: which links each reads, what it gives, and that every failure falls back to Reddit's copy.

redgifs' answers come from `tests/fixtures/redgifs/` (real, redacted; its README
says how). The token is built here with the real claim names, since the real
one is never saved.
"""

import base64
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from arrt_player.sources import Candidate, Caption, Item, Kind, Resolved
from arrt_player.sources.cache import MediaCache
from arrt_player.sources.fetch import MAX_FILE_BYTES, ApiAnswer, Download, NotMedia, TooLarge
from arrt_player.sources.reddit import items_of, parse_listing
from arrt_player.sources.resolvers import Resolvers
from arrt_player.sources.resolvers.imgur import ImgurResolver
from arrt_player.sources.resolvers.redgifs import API, RENEW_BEFORE_S, RedgifsResolver

FIXTURES = Path(__file__).parent / "fixtures"
GIF = json.loads((FIXTURES / "redgifs" / "gif.json").read_text())
MISSING = json.loads((FIXTURES / "redgifs" / "gif_missing.json").read_text())
UNAUTHORIZED = json.loads((FIXTURES / "redgifs" / "unauthorized.json").read_text())
AUTH = json.loads((FIXTURES / "redgifs" / "auth.json").read_text())
SHAPES = json.loads((FIXTURES / "reddit" / "shapes.json").read_text())
CAPTION = Caption(title="t", community="r/x", author="u/y")
LINK = "https://www.redgifs.com/watch/redactedgifid"
NOW = 1_800_000_000.0


def token(name: str, exp: float | None) -> str:
    """A JWT with the claim names redgifs' carries, and an `exp` this test chooses."""
    claims = dict.fromkeys(AUTH["token_claims"], "redacted")
    claims["iat"] = NOW
    if exp is None:
        del claims["exp"]
    else:
        claims["exp"] = exp
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.{name}"


def answer(recorded: dict) -> ApiAnswer:
    return ApiAnswer(recorded["status"], json.dumps(recorded["body"]).encode())


class FakeApi:
    """redgifs' API as measured. Tokens are handed out in order; each lookup records the token it carried."""

    def __init__(self, tokens: list[str | ApiAnswer], lookups: list[ApiAnswer] | None = None) -> None:
        self.tokens = list(tokens)
        self.lookups = list(lookups or [])
        self.calls: list[tuple[str, dict]] = []

    async def get_api(self, url: str, headers: Mapping[str, str] | None = None) -> ApiAnswer:
        self.calls.append((url, dict(headers or {})))
        if url == f"{API}/auth/temporary":
            given = self.tokens.pop(0)
            if isinstance(given, ApiAnswer):
                return given
            body = {**AUTH["body"], "token": given}
            return ApiAnswer(200, json.dumps(body).encode())
        return self.lookups.pop(0) if self.lookups else answer(GIF)

    def tokens_used(self) -> list[str]:
        return [
            headers["Authorization"].removeprefix("Bearer ").rsplit(".", 1)[1] for url, headers in self.calls if "/gifs/" in url
        ]

    def token_fetches(self) -> int:
        return sum(url.endswith("/auth/temporary") for url, _ in self.calls)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def item(
    link: str = LINK, domain: str = "redgifs.com", media_url: str = "https://v.redd.it/x/DASH_720.mp4?source=fallback"
) -> Item:
    return Item(id="t3_abc", media_url=media_url, kind=Kind.CLIP, caption=CAPTION, tier="reddit-copy", link=link, domain=domain)


# -- imgur -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://i.imgur.com/AbC12dE.gifv", Resolved("https://i.imgur.com/AbC12dE.mp4", Kind.CLIP)),
        ("https://m.imgur.com/AbC12dE.gifv", Resolved("https://i.imgur.com/AbC12dE.mp4", Kind.CLIP)),
        ("https://i.imgur.com/AbC12dE.gif", Resolved("https://i.imgur.com/AbC12dE.mp4", Kind.CLIP)),
        ("https://i.imgur.com/AbC12dE.jpg", Resolved("https://i.imgur.com/AbC12dE.jpg", Kind.STILL)),
    ],
)
async def test_imgur_reads_its_media_links(link, expected):
    resolver = ImgurResolver()

    assert resolver.claims(link) is expected.kind
    assert await resolver.resolve(link) == expected


@pytest.mark.parametrize(
    "link",
    [
        "https://imgur.com/a/AbC12dE",
        "https://imgur.com/AbC12dE",
        "https://i.imgur.com/AbC12dE.gifv/../x",
        "https://elsewhere.example/AbC12dE.gifv",
    ],
)
async def test_imgur_leaves_pages_albums_and_other_hosts_alone(link):
    resolver = ImgurResolver()

    assert resolver.claims(link) is None
    assert await resolver.resolve(link) is None


# -- redgifs ----------------------------------------------------------------------


async def test_redgifs_gives_the_hd_mp4_and_sends_the_token_not_a_cookie():
    api = FakeApi([token("first", NOW + 86_400)])

    resolved = await RedgifsResolver(api, clock=Clock()).resolve(LINK)

    assert resolved == Resolved(GIF["body"]["gif"]["urls"]["hd"], Kind.CLIP)
    assert [url for url, _ in api.calls] == [f"{API}/auth/temporary", f"{API}/gifs/redactedgifid"]
    assert all("Cookie" not in headers for _, headers in api.calls)


async def test_redgifs_takes_sd_when_there_is_no_hd():
    gif = json.loads(json.dumps(GIF))
    del gif["body"]["gif"]["urls"]["hd"]
    api = FakeApi([token("first", NOW + 86_400)], [answer(gif)])

    resolved = await RedgifsResolver(api, clock=Clock()).resolve(LINK)

    assert resolved == Resolved(GIF["body"]["gif"]["urls"]["sd"], Kind.CLIP)


async def test_the_token_is_reused_until_shortly_before_it_expires_then_renewed():
    """Steps that are not multiples of anything: a renewal one step early or late shows."""
    clock = Clock()
    api = FakeApi([token("first", NOW + 86_400), token("second", NOW + 2 * 86_400)])
    resolver = RedgifsResolver(api, clock=clock)

    await resolver.resolve(LINK)
    clock.now = NOW + 86_400 - RENEW_BEFORE_S - 7
    await resolver.resolve(LINK)
    assert api.tokens_used() == ["first", "first"]

    clock.now = NOW + 86_400 - RENEW_BEFORE_S + 3
    await resolver.resolve(LINK)
    assert api.tokens_used() == ["first", "first", "second"]
    assert api.token_fetches() == 2


async def test_a_token_whose_expiry_cannot_be_read_is_trusted_for_an_hour():
    clock = Clock()
    api = FakeApi([token("first", None), token("second", None)])
    resolver = RedgifsResolver(api, clock=clock)

    await resolver.resolve(LINK)
    clock.now += 3600 - 11
    await resolver.resolve(LINK)
    clock.now += 23
    await resolver.resolve(LINK)

    assert api.tokens_used() == ["first", "first", "second"]


async def test_a_refused_token_is_replaced_once_and_the_lookup_tried_again():
    api = FakeApi([token("first", NOW + 86_400), token("second", NOW + 86_400)], [answer(UNAUTHORIZED), answer(GIF)])

    resolved = await RedgifsResolver(api, clock=Clock()).resolve(LINK)

    assert resolved is not None
    assert api.tokens_used() == ["first", "second"]


async def test_a_token_refused_twice_gives_no_answer_rather_than_looping():
    api = FakeApi([token("first", NOW + 86_400), token("second", NOW + 86_400)], [answer(UNAUTHORIZED), answer(UNAUTHORIZED)])

    assert await RedgifsResolver(api, clock=Clock()).resolve(LINK) is None
    assert api.token_fetches() == 2


@pytest.mark.parametrize("lookup", [answer(MISSING), ApiAnswer(429, b""), ApiAnswer(503, b"")])
async def test_a_gif_that_is_gone_or_a_host_that_will_not_answer_gives_no_answer(lookup):
    api = FakeApi([token("first", NOW + 86_400)], [lookup])

    assert await RedgifsResolver(api, clock=Clock()).resolve(LINK) is None


@pytest.mark.parametrize(
    "link",
    [
        "https://www.redgifs.com/users/someone",
        "https://redgifs.com/watch/",
        "https://elsewhere.example/watch/abc",
        "https://v.redd.it/watch/abc",
    ],
)
async def test_redgifs_reads_only_its_watch_links(link):
    api = FakeApi([])
    resolver = RedgifsResolver(api, clock=Clock())

    assert resolver.claims(link) is None
    assert await resolver.resolve(link) is None
    assert api.calls == []


# -- the registry and the order of candidates --------------------------------------


class Broken:
    name = "broken"
    domains = frozenset({"broken.example"})

    def __init__(self, error: Exception | None) -> None:
        self.error = error

    def claims(self, link: str) -> Kind | None:
        return Kind.CLIP

    async def resolve(self, link: str) -> Resolved | None:
        if self.error is not None:
            raise self.error
        return None


async def test_a_resolvers_answer_goes_before_reddits_copy():
    resolvers = Resolvers([RedgifsResolver(FakeApi([token("first", NOW + 86_400)]), clock=Clock()), ImgurResolver()])

    found = await resolvers.candidates(item())

    assert found == [Candidate(GIF["body"]["gif"]["urls"]["hd"], "host:redgifs"), Candidate(item().media_url, "reddit-copy")]


async def test_a_resolver_is_asked_only_about_its_own_domain():
    api = FakeApi([token("first", NOW + 86_400)])
    resolvers = Resolvers([RedgifsResolver(api, clock=Clock())])

    found = await resolvers.candidates(item(domain="v.redd.it"))

    assert found == [Candidate(item().media_url, "reddit-copy")]
    assert api.calls == []


async def test_a_link_on_a_resolvers_domain_that_it_does_not_read_is_left_to_reddits_copy_quietly(caplog):
    """A redgifs profile page, say: no lookup is spent on it and no fall-back is reported."""
    api = FakeApi([token("first", NOW + 86_400)])
    resolvers = Resolvers([RedgifsResolver(api, clock=Clock())])

    found = await resolvers.candidates(item(link="https://www.redgifs.com/users/someone"))

    assert found == [Candidate(item().media_url, "reddit-copy")]
    assert api.calls == []
    assert "slideshow.resolver.fallback" not in caplog.text


@pytest.mark.parametrize("error", [None, TimeoutError(), ValueError("a shape it does not read"), KeyError("gif"), NotMedia("x")])
async def test_whatever_a_resolver_does_wrong_the_item_keeps_reddits_copy_and_it_is_said(error, caplog):
    resolvers = Resolvers([Broken(error)])

    found = await resolvers.candidates(item(link="https://broken.example/x", domain="broken.example"))

    assert found == [Candidate(item().media_url, "reddit-copy")]
    assert "slideshow.resolver.fallback resolver=broken" in caplog.text


def test_two_resolvers_cannot_claim_one_domain():
    class Also(Broken):
        name = "also"

    with pytest.raises(ValueError, match="claimed by both"):
        Resolvers([Broken(None), Also(None)])


# -- through the parser and the cache ------------------------------------------------


def test_a_post_only_a_resolver_can_show_becomes_an_item_whose_one_hope_is_the_resolver():
    gifv = next(c["data"] for c in SHAPES["data"]["children"] if c["data"]["title"] == "A gifv on imgur without a copy")
    resolvers = Resolvers([ImgurResolver()])

    found = items_of(gifv, resolvers.claims)

    assert [(i.media_url, i.kind, i.tier, i.domain) for i in found] == [("", Kind.CLIP, "host", "i.imgur.com")]
    assert found[0].link == gifv["url"]
    _, unresolved = parse_listing(SHAPES, resolvers.claims)
    assert "i.imgur.com" not in unresolved
    assert unresolved["gfycat.com"] == 1


async def test_with_no_resolver_and_no_copy_there_is_nothing_to_fetch():
    found = await Resolvers([]).candidates(item(media_url=""))

    assert found == []


class Downloads:
    """Fails the URLs it is told to, writes the rest."""

    def __init__(self, failing: dict[str, Exception]) -> None:
        self.failing = failing
        self.asked: list[str] = []

    async def download(self, url: str, destination: Path, max_bytes: int = MAX_FILE_BYTES) -> Download:
        self.asked.append(url)
        if url in self.failing:
            raise self.failing[url]
        path = destination.with_name(destination.name + ".mp4")
        path.write_bytes(b"\x00" * 10)
        return Download(path=path, kind=Kind.CLIP, extension="mp4", size=10)


async def test_the_cache_falls_back_when_a_resolvers_url_is_not_media_and_counts_the_tier(tmp_path, caplog):
    """The imgur case measured: a deleted id's mp4 lands on an HTML page the download refuses."""
    api = FakeApi([token("first", NOW + 86_400)])
    resolvers = Resolvers([RedgifsResolver(api, clock=Clock())])
    hd = GIF["body"]["gif"]["urls"]["hd"]
    downloads = Downloads({hd: NotMedia("text/html")})
    cache = MediaCache(tmp_path, downloads, candidates=resolvers.candidates)

    report = await cache.fill([item()])

    assert downloads.asked == [hd, item().media_url]
    assert report.downloaded == ["t3_abc"]
    assert report.tiers == {"reddit-copy": 1}
    assert "slideshow.cache.fell_back item=t3_abc to=reddit-copy" in caplog.text


async def test_the_cache_counts_a_resolvers_download_under_its_tier(tmp_path):
    resolvers = Resolvers([RedgifsResolver(FakeApi([token("first", NOW + 86_400)]), clock=Clock())])
    cache = MediaCache(tmp_path, Downloads({}), candidates=resolvers.candidates)

    report = await cache.fill([item()])

    assert report.tiers == {"host:redgifs": 1}


@pytest.mark.parametrize(
    ("failing", "network_down"),
    [
        ({"hd": TimeoutError(), "copy": TimeoutError()}, True),
        ({"hd": TimeoutError(), "copy": NotMedia("x")}, False),
        ({"hd": TooLarge("over the file limit"), "copy": TimeoutError()}, False),
    ],
)
async def test_an_item_is_unreachable_only_when_every_place_was(tmp_path, failing, network_down):
    resolvers = Resolvers([RedgifsResolver(FakeApi([token("first", NOW + 86_400)]), clock=Clock())])
    urls = {"hd": GIF["body"]["gif"]["urls"]["hd"], "copy": item().media_url}
    downloads = Downloads({urls[key]: error for key, error in failing.items()})
    cache = MediaCache(tmp_path, downloads, candidates=resolvers.candidates)
    items = [
        Item(
            id=f"t3_{n}",
            media_url=urls["copy"],
            kind=Kind.CLIP,
            caption=CAPTION,
            tier="reddit-copy",
            link=LINK,
            domain="redgifs.com",
        )
        for n in "abc"
    ]

    report = await cache.fill(items)

    assert report.network_down is network_down
    assert len(report.failed) == 3


def _token_answer(token_value: object) -> ApiAnswer:
    return ApiAnswer(200, json.dumps({**AUTH["body"], "token": token_value}).encode())


@pytest.mark.parametrize(
    "refusal",
    [
        ApiAnswer(429, json.dumps({"error": {"code": "TooManyRequests"}}).encode()),
        ApiAnswer(503, b""),
        _token_answer(""),
        _token_answer(None),
        # Only the status check stops this one: a refusal that still carries a token.
        ApiAnswer(503, json.dumps({**AUTH["body"], "token": "stale"}).encode()),
    ],
)
async def test_no_token_means_reddits_copy_said_so_and_no_lookup(refusal, caplog):
    """The token request refused, or answered with no token: the item keeps Reddit's copy, and nothing is looked up."""
    api = FakeApi([refusal])
    resolvers = Resolvers([RedgifsResolver(api, clock=Clock())])

    found = await resolvers.candidates(item())

    assert found == [Candidate(item().media_url, "reddit-copy")]
    assert "slideshow.resolver.fallback resolver=redgifs" in caplog.text
    assert [url for url, _ in api.calls] == [f"{API}/auth/temporary"]


@pytest.mark.parametrize(
    "urls",
    [
        ["https://media.redgifs.com/a.mp4"],
        "https://media.redgifs.com/a.mp4",
        None,
        {"hd": 7},
        {"hd": "http://media.redgifs.com/a.mp4"},
    ],
)
async def test_a_lookup_answer_in_a_shape_not_read_falls_back_and_does_not_end_the_fill(urls, tmp_path):
    gif = json.loads(json.dumps(GIF))
    gif["body"]["gif"]["urls"] = urls
    api = FakeApi([token("first", NOW + 86_400)], [answer(gif)])
    resolvers = Resolvers([RedgifsResolver(api, clock=Clock())])
    cache = MediaCache(tmp_path, Downloads({}), candidates=resolvers.candidates)

    report = await cache.fill([item()])

    assert report.tiers == {"reddit-copy": 1}
