"""redgifs: a gif's HD mp4 through redgifs' own API, in place of Reddit's often lower copy.

Measured on 2026-11-10 (the build plan's chunk 03 verify-api; the answers are
in `tests/fixtures/redgifs/`):

* `GET /v2/auth/temporary` gives a token with no account. It is a JWT that lasts
  24 hours and is bound to the caller's address and user agent, so it is kept
  and reused until shortly before its `exp`, and asked for again after that.
* `GET /v2/gifs/{id}` with `Authorization: Bearer <token>` answers 200 with
  `gif.urls.hd` (an unsigned `media.redgifs.com/<name>.mp4`) and `gif.urls.sd`.
  A gif that is gone is 404; a missing or bad token is 401.
* A post's link is `www.redgifs.com/watch/<id>`, with a lowercase id.

Every call goes through `fetch.Fetcher.get_api`, whose host set includes
`api.redgifs.com` and whose cookie goes only to Reddit. A token that stops
working (401) is replaced once and the lookup tried again; anything else gives no
answer, and the item falls back to Reddit's copy.
"""

import base64
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Final, Protocol
from urllib.parse import urlsplit

from arrt_player.sources import Kind, Resolved
from arrt_player.sources.fetch import ApiAnswer

log = logging.getLogger(__name__)

API: Final[str] = "https://api.redgifs.com/v2"

#: How long before a token's `exp` a new one is asked for, so one never expires mid-lookup.
RENEW_BEFORE_S: Final[float] = 300.0

#: How long a token is trusted when its `exp` cannot be read: well inside the
#: measured 24 hours, and a 401 replaces it sooner if need be.
UNREAD_TOKEN_LIFETIME_S: Final[float] = 3600.0

_WATCH: Final = re.compile(r"/watch/([A-Za-z0-9]+)/?")


class ApiClient(Protocol):
    async def get_api(self, url: str, headers: Mapping[str, str] | None = None) -> ApiAnswer: ...


class RedgifsResolver:
    name = "redgifs"
    domains: frozenset[str] = frozenset({"redgifs.com", "www.redgifs.com", "v3.redgifs.com"})

    def __init__(self, client: ApiClient, clock: Callable[[], float] = time.time) -> None:
        self._client = client
        self._clock = clock
        self._token: str | None = None
        self._renew_at = 0.0

    def __repr__(self) -> str:
        return "RedgifsResolver()"

    def claims(self, link: str) -> Kind | None:
        return Kind.CLIP if self._gif_id(link) else None

    async def resolve(self, link: str) -> Resolved | None:
        gif_id = self._gif_id(link)
        if gif_id is None:
            return None
        answer = await self._lookup(gif_id)
        if answer.status == HTTPStatus.UNAUTHORIZED:
            log.info("slideshow.resolver.redgifs.token_refused renewing=once")
            self._token = None
            answer = await self._lookup(gif_id)
        if answer.status == HTTPStatus.NOT_FOUND:
            log.info("slideshow.resolver.redgifs.gone gif=%s", gif_id)
            return None
        if answer.status != HTTPStatus.OK:
            log.warning("slideshow.resolver.redgifs.lookup_failed gif=%s status=%d", gif_id, answer.status)
            return None
        urls = json.loads(answer.body)["gif"]["urls"]
        if not isinstance(urls, dict):
            # Raised as a shape the resolver does not read, which costs only this answer.
            raise TypeError(f"redgifs gave urls as {type(urls).__name__} for {gif_id}")
        url = urls.get("hd") or urls.get("sd")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError(f"redgifs gave no https media URL for {gif_id}")
        return Resolved(url, Kind.CLIP)

    async def _lookup(self, gif_id: str) -> ApiAnswer:
        token = await self._current_token()
        return await self._client.get_api(f"{API}/gifs/{gif_id}", headers={"Authorization": f"Bearer {token}"})

    async def _current_token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._renew_at:
            return self._token
        answer = await self._client.get_api(f"{API}/auth/temporary")
        if answer.status != HTTPStatus.OK:
            raise ValueError(f"redgifs gave no token: {answer.status}")
        token = json.loads(answer.body)["token"]
        if not isinstance(token, str) or not token:
            raise ValueError("redgifs gave an empty token")
        expires = _expiry(token)
        self._token = token
        self._renew_at = (expires - RENEW_BEFORE_S) if expires is not None else now + UNREAD_TOKEN_LIFETIME_S
        log.info("slideshow.resolver.redgifs.token valid_for_s=%.0f", self._renew_at - now)
        return token

    def _gif_id(self, link: str) -> str | None:
        parts = urlsplit(link)
        if (parts.hostname or "").lower() not in self.domains:
            return None
        match = _WATCH.fullmatch(parts.path)
        return match.group(1).lower() if match else None


def _expiry(token: str) -> float | None:
    """The token's `exp`, read from its JWT payload without checking its signature; None when it has none."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expires = claims["exp"]
    except (IndexError, ValueError, KeyError, TypeError):
        return None
    return float(expires) if isinstance(expires, int | float) else None
