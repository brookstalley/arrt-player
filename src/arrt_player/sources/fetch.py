"""The slideshow's network client: API calls to a fixed set of hosts, and media from public addresses only.

**The second module in this plane allowed an HTTP client** (`pull.py` is the
first), and `tests/test_plane_isolation.py` holds it to what this docstring
says. The rule it is an exception to exists to stop a quiet second channel to
the Arrt server. A slideshow has no server, and a post may link media on any
host, so this module is bounded by *what it can reach* rather than by a list:

* **Listing and API calls** (`get_api`) go only to `API_HOSTS`, over https, and
  follow no redirect: an answer is the caller's to read (`reddit.py` says what
  Reddit's mean), and a redirect followed could carry the cookie elsewhere.
* **The session cookie goes to `REDDIT_HOST` and nowhere else.** It is sent as a
  header on that host's API calls only. There is no cookie jar, so nothing a
  host sets travels to another, and media downloads carry no cookie at all.
* **Media downloads** (`download`) may reach any host, but only at a public
  address. A post can name any URL, and this process sits on a home network: a
  post linking the router's own address would otherwise have it fetch from the
  router. That is server-side request forgery through a listing. So every
  address is checked **at connection time**, by the resolver aiohttp connects
  through (`PublicOnlyResolver`), which means a name that resolves to a public
  address when checked and a private one when connected cannot slip past.
  aiohttp does not consult the resolver for an address written into the URL
  (`aiohttp/connector.py`, `_resolve_host`, 3.14), so those are checked here
  first. Redirects are followed by hand, at most `MAX_REDIRECTS`, each hop
  checked the same way.
* **A download is only kept if it is a picture or a clip**: the header must say
  `image/` or `video/` *and* its first bytes must be one of the formats in
  `sniff`, since a header is the host's claim. It is cut off past its byte
  limit. It is written beside its destination and moved into place only when
  whole, so a half-downloaded file never exists under the name mpv is given.

Nothing here imports the Arrt server's configuration, the contract or
`pull.py`, so this module has no way to learn the server's address or token.
"""

import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Final, Self

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver
from yarl import URL

from arrt_player.sources import Kind

log = logging.getLogger(__name__)

#: The host the session cookie belongs to, and the only one it is sent to.
REDDIT_HOST: Final[str] = "www.reddit.com"

#: The only hosts a listing or API call may go to. The plane-isolation guard reads
#: this set, and holds this module to spelling no other host.
API_HOSTS: Final[frozenset[str]] = frozenset({REDDIT_HOST, "api.redgifs.com"})

#: Redirects a download follows before it gives up. Reddit's media hosts answer
#: directly; a link shortener or an image host's CDN adds one or two.
MAX_REDIRECTS: Final[int] = 5

#: The most one media file may be. A 1080p clip of a minute at Reddit's bitrates
#: is tens of megabytes; this is room for that with headroom, and a stop for a
#: link to something that is not a slideshow item at all.
MAX_FILE_BYTES: Final[int] = 200 * 1024 * 1024

#: The most an API answer may be. A listing of 100 posts is about a megabyte.
MAX_API_BYTES: Final[int] = 16 * 1024 * 1024

#: Sent with every request. Reddit asks automated clients to say what they are.
USER_AGENT: Final[str] = "arrt-player/slideshow (one household's picture frame)"

_REDIRECTS: Final = frozenset(
    {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    }
)

#: The suffix a download carries until it is whole. The media cache clears any
#: left by a process that died mid-download.
PARTIAL_SUFFIX: Final[str] = ".partial"

_CHUNK: Final[int] = 64 * 1024


class FetchRefused(Exception):
    """A request this module would not make, or a download it would not keep. The message names why, never a secret."""


class AddressRefused(FetchRefused, OSError):
    """A host that is, or resolves only to, an address that is not public.

    An `OSError` because aiohttp's connector turns what a resolver raises into a
    connection error only when it is one, and keeps it as the cause.
    """


class NotMedia(FetchRefused):
    """The answer is not a picture or a clip, by its header or by its bytes."""


class TooLarge(FetchRefused):
    """The answer is bigger than the limit it was fetched under."""


class BadAnswer(FetchRefused):
    """A status other than 200, too many redirects, or a redirect that goes nowhere a download may."""


#: Reaching the host failed: no answer, a refused connection or a timeout.
UNREACHABLE: Final = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is on the public internet, and so one a download may reach.

    Python's `is_global` is the test, with two corrections. An IPv6 address that
    carries an IPv4 one (IPv4-mapped `::ffff:a.b.c.d`, or the NAT64 prefix
    `64:ff9b::/96`) is judged by the IPv4 address it carries: Python calls
    `64:ff9b::c0a8:101` global although it reaches a LAN address through a NAT64
    gateway. And multicast is refused although some of it is global.

    The IPv4-mapped unwrap changes nothing on Python 3.13, which already judges
    a mapped address by the one it carries (measured 2026-11-10). It stays because
    the floor is 3.12, where that has not been measured.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address in _NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address.is_global and not address.is_multicast


_NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")

AddressPolicy = Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool]


class PublicOnlyResolver(AbstractResolver):
    """Resolves a name and keeps only the addresses `allowed` admits; none left is `AddressRefused`.

    aiohttp connects only to what its resolver returns, so this is the check that
    holds at connection time, whatever the name resolved to a moment earlier.
    """

    def __init__(self, allowed: AddressPolicy = is_public_address, inner: AbstractResolver | None = None) -> None:
        self._allowed = allowed
        self._inner = inner

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        if self._inner is None:
            self._inner = ThreadedResolver()
        results = await self._inner.resolve(host, port, family)
        kept = [result for result in results if self._allowed(ipaddress.ip_address(result["host"]))]
        if not kept:
            raise AddressRefused(f"{host} resolves to no public address")
        return kept

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


@dataclass(frozen=True)
class ApiAnswer:
    """An API call's answer, whatever its status. `location` is a redirect's target, which is not followed."""

    status: int
    body: bytes
    location: str | None = None
    #: Reddit's `x-ratelimit-reset`, in seconds, when it sent one.
    ratelimit_reset_s: float | None = None
    content_type: str = ""


@dataclass(frozen=True)
class Download:
    """A whole file, moved into place, and what its bytes say it is."""

    path: Path
    kind: Kind
    extension: str
    size: int


@dataclass(frozen=True)
class _Format:
    extension: str
    kind: Kind


def sniff(head: bytes) -> _Format | None:
    """What a file's first bytes say it is, from the formats a slideshow shows, or None.

    The animated formats a post can carry are clips (GIF, MP4, WebM); a GIF that
    turns out to have one frame is told apart later, when mpv reports no length.
    """
    if head[4:8] == b"ftyp":
        # An ISO media file names its brand: AVIF is a still, HEIF is not shown,
        # and every other brand is a video container.
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1"):
            return None
        return _Format("avif", Kind.STILL) if brand in (b"avif", b"avis") else _Format("mp4", Kind.CLIP)
    if head[:4] == b"RIFF":
        return _Format("webp", Kind.STILL) if head[8:12] == b"WEBP" else None
    return next((found for magic, found in _MAGIC if head.startswith(magic)), None)


_MAGIC: Final[tuple[tuple[bytes, _Format], ...]] = (
    (b"\xff\xd8\xff", _Format("jpg", Kind.STILL)),
    (b"\x89PNG\r\n\x1a\n", _Format("png", Kind.STILL)),
    (b"GIF87a", _Format("gif", Kind.CLIP)),
    (b"GIF89a", _Format("gif", Kind.CLIP)),
    (b"\x1a\x45\xdf\xa3", _Format("webm", Kind.CLIP)),
)


#: Enough of a file's start for `sniff` to decide.
SNIFF_BYTES: Final[int] = 12


@dataclass
class Fetcher:
    """The client, used as an async context manager. Holds the session cookie, which it never shows.

    `allowed`, `inner_resolver` and `api_schemes` exist so the suite can stand a
    local server in for a public host; the defaults are what the Player runs with.
    """

    cookie: str | None = field(default=None, repr=False)
    allowed: AddressPolicy = is_public_address
    inner_resolver: AbstractResolver | None = None
    api_schemes: tuple[str, ...] = ("https",)
    timeout: aiohttp.ClientTimeout = field(
        default_factory=lambda: aiohttp.ClientTimeout(total=120, sock_connect=10, sock_read=30)
    )
    _session: aiohttp.ClientSession | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.cookie is not None and (not self.cookie or any(c in self.cookie for c in ";\r\n ")):
            raise ValueError("the session cookie must be the value alone, with no spaces, semicolons or line breaks")

    async def __aenter__(self) -> Self:
        connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(self.allowed, self.inner_resolver))
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=self.timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={"User-Agent": USER_AGENT},
            trust_env=False,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_api(self, url: str) -> ApiAnswer:
        """GET one of `API_HOSTS`, without following a redirect. The cookie rides only to `REDDIT_HOST`."""
        target = URL(url)
        if target.scheme not in self.api_schemes or target.host not in API_HOSTS:
            raise FetchRefused(f"API calls go only to {sorted(API_HOSTS)} over https, not {target.scheme}://{target.host}")
        headers = {"Cookie": f"reddit_session={self.cookie}"} if self.cookie and target.host == REDDIT_HOST else {}
        async with _refusal_raised_as_itself(), self._client().get(target, headers=headers, allow_redirects=False) as response:
            body = await _read_capped(response, MAX_API_BYTES)
            return ApiAnswer(
                status=response.status,
                body=body,
                location=response.headers.get("Location"),
                ratelimit_reset_s=_seconds(response.headers.get("x-ratelimit-reset")),
                content_type=response.headers.get("Content-Type", ""),
            )

    async def download(self, url: str, destination: Path, max_bytes: int = MAX_FILE_BYTES) -> Download:
        """Fetch a media file from a public address into `destination` plus its sniffed extension.

        Raises `FetchRefused` (a refused address, not media, too large, a bad
        answer) or one of `UNREACHABLE`. Either way no file is left behind.
        """
        target = URL(url)
        for _hop in range(MAX_REDIRECTS + 1):
            _check_target(target, self.allowed)
            async with _refusal_raised_as_itself(), self._client().get(target, allow_redirects=False) as response:
                if response.status in _REDIRECTS:
                    location = response.headers.get("Location")
                    if not location:
                        raise BadAnswer(f"{target.host} redirected nowhere")
                    target = target.join(URL(location))
                    continue
                if response.status != HTTPStatus.OK:
                    raise BadAnswer(f"{target.host} answered {response.status}")
                return await _save(response, destination, max_bytes)
        raise BadAnswer(f"more than {MAX_REDIRECTS} redirects from {URL(url).host}")

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Fetcher is used inside `async with`")
        return self._session


@contextlib.asynccontextmanager
async def _refusal_raised_as_itself() -> AsyncIterator[None]:
    """Raise a refused address as `AddressRefused`, not as the connection error aiohttp wraps it in.

    `PublicOnlyResolver` refuses at connection time, and aiohttp's connector
    re-raises what a resolver raises as `ClientConnectorError` (a DNS error),
    keeping it only as the cause. A caller tells a broken link from a network
    that is down by the error's type, so the refusal has to arrive as itself:
    the media cache would otherwise count it as the network being down.
    """
    try:
        yield
    except aiohttp.ClientConnectorError as error:
        if isinstance(error.__cause__, AddressRefused):
            raise error.__cause__ from None
        raise


def _check_target(target: URL, allowed: AddressPolicy) -> None:
    """Refuse a scheme other than http(s), and an address in the URL that is not public.

    A name is left to `PublicOnlyResolver`. What aiohttp takes for an address
    (anything with a colon, or all digits and dots) and Python cannot read as
    one is refused rather than handed on.
    """
    if target.scheme not in ("http", "https") or not target.host:
        raise BadAnswer(f"a download goes over http or https to a host, not {target.scheme}://{target.host}")
    host = target.host
    if ":" in host or host.replace(".", "").isdigit():
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise AddressRefused(f"{host} is not an address this client will read") from None
        if not allowed(address):
            raise AddressRefused(f"{host} is not a public address")


async def _save(response: aiohttp.ClientResponse, destination: Path, max_bytes: int) -> Download:
    """Stream an answer to a partial file, check it, and move it into place; leave nothing on any failure."""
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not content_type.startswith(("image/", "video/")):
        raise NotMedia(f"{response.url.host} sent {content_type or 'no content type'}")
    declared = response.content_length
    if declared is not None and declared > max_bytes:
        raise TooLarge(f"{response.url.host} offers {declared} bytes, over the {max_bytes} limit")

    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    size = 0
    head = b""
    try:
        with partial.open("wb") as out:
            async for chunk in response.content.iter_chunked(_CHUNK):
                # What the bytes are is decided before how many there are, so
                # a page posing as a clip is named as that, not as too large.
                if len(head) < SNIFF_BYTES:
                    head += chunk[: SNIFF_BYTES - len(head)]
                    if len(head) >= SNIFF_BYTES and sniff(head) is None:
                        raise NotMedia(f"{response.url.host} sent {content_type} whose bytes are no format shown here")
                size += len(chunk)
                if size > max_bytes:
                    raise TooLarge(f"{response.url.host} sent more than the {max_bytes} limit")
                out.write(chunk)
        found = sniff(head)
        if found is None:
            raise NotMedia(f"{response.url.host} sent {content_type} whose bytes are no format shown here")
        final = destination.with_name(f"{destination.name}.{found.extension}")
        partial.replace(final)
        return Download(path=final, kind=found.kind, extension=found.extension, size=size)
    finally:
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()


async def _read_capped(response: aiohttp.ClientResponse, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK):
        body += chunk
        if len(body) > limit:
            raise TooLarge(f"{response.url.host} sent more than {limit} bytes")
    return bytes(body)


def _seconds(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
