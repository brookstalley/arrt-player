"""What the settings page and `config` check about a Reddit slideshow: its listing address and its session cookie.

**Apart from `reddit.py` because that module imports the network client.** The
plane-isolation guard lets only `sources/`, the slideshow programme and the
entry point import `fetch.py`, and an import of `reddit.py` from `config` or the
page would carry the client in with it. Everything here is pure: no network, no
aiohttp.

**The cookie's expiry is read, never trusted for anything but a warning.** A
`reddit_session` value is a JWT (measured 2026-11-10: issued 2026-11-08, `exp`
2027-05-08). Its payload's `exp` tells the page when to warn that a new cookie
will be needed. The signature is not checked, and nothing is decided on the
claim but that warning: Reddit is the one that says whether it still accepts the
cookie (`reddit.RedditSource`).
"""

import base64
import binascii
import json
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: The one host listings are read from, and the only one the cookie is sent to.
#: `fetch.REDDIT_HOST` is the client's copy, and a test holds the two equal.
REDDIT_HOST: Final[str] = "www.reddit.com"

#: The hosts a listing URL may be written with. Each is read from `REDDIT_HOST`.
REDDIT_HOSTS: Final[frozenset[str]] = frozenset({"reddit.com", REDDIT_HOST, "old.reddit.com", "new.reddit.com"})

#: The most a listing page holds.
LISTING_LIMIT: Final[int] = 100

#: A listing's path has at least `/r/<name>`: the bare site, or `/r`, lists
#: nothing of anyone's choosing.
_LISTING_DEPTH: Final[int] = 2

#: What a browser's cookie panel shows before the value, which people paste too.
_COOKIE_NAME_PREFIX: Final[str] = "reddit_session="

#: The longest value accepted. Reddit's is about 700 characters; this is a typo guard, not a format.
_MAX_SESSION_LENGTH: Final[int] = 8192


def listing_url(written: str) -> str:
    """The JSON listing for a listing URL as a person copies it from a browser.

    `https://www.reddit.com/r/pics/top/?t=week` becomes
    `https://www.reddit.com/r/pics/top.json?t=week&limit=100&raw_json=1`.
    `raw_json=1` keeps Reddit from escaping `&` in the media URLs. Raises
    `ValueError` for anything that is not a Reddit listing.
    """
    parts = urlsplit(written.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or host not in REDDIT_HOSTS:
        raise ValueError(f"a slideshow listing is a reddit.com address, not {written!r}")
    path = parts.path.rstrip("/")
    if path.count("/") < _LISTING_DEPTH:
        raise ValueError(f"{written!r} names no listing; copy the address of a subreddit or a multireddit")
    if not path.endswith(".json"):
        path += ".json"
    query = [(key, value) for key, value in parse_qsl(parts.query) if key not in ("limit", "raw_json")]
    query += [("limit", str(LISTING_LIMIT)), ("raw_json", "1")]
    return urlunsplit(("https", REDDIT_HOST, path, urlencode(query), ""))


def listing_url_problem(written: str) -> str | None:
    """Why `written` cannot be `SLIDESHOW_URL`, in words for the page; None when it can."""
    try:
        listing_url(written)
    except ValueError as error:
        return f"SLIDESHOW_URL: {error}."
    return None


def clean_session(pasted: str) -> str:
    """The cookie's value as pasted, without surrounding space or a leading `reddit_session=`."""
    value = pasted.strip()
    return value.removeprefix(_COOKIE_NAME_PREFIX)


def session_problem(value: str) -> str | None:
    """Why `value` cannot be `REDDIT_SESSION`, or None when it can. It goes into a Cookie header, as `fetch.Fetcher` checks."""
    if not value or len(value) > _MAX_SESSION_LENGTH or any(c in value for c in ';,"\\') or any(c.isspace() for c in value):
        return "REDDIT_SESSION must be the reddit_session cookie's value alone, with no spaces, quotes or semicolons."
    return None


def session_expiry(value: str) -> float | None:
    """When the cookie says it expires, as a Unix time, or None when it does not say in a form this reads.

    Unverified: the claim decides a warning on the page and nothing else.
    """
    parts = value.split(".")
    if len(parts) != 3:  # noqa: PLR2004 -- a JWT is header, payload and signature
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return None
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    return float(expiry) if isinstance(expiry, int | float) and not isinstance(expiry, bool) else None
