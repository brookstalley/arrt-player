"""Reddit as a slideshow source: one listing, read with the owner's session cookie, turned into items.

**What Reddit answers was measured on 2026-11-10 with the owner's cookie** (the
build plan's chunk 02 verify-api):

* A listing it serves is `200` JSON. **Every failure is `403` with an HTML page**:
  a listing it refuses even to a logged-in session (`/new`, `/hot`, `/rising`),
  a cookie it no longer accepts, and no cookie at all. Nothing answered `401` or
  redirected to log in. So the status cannot say which, and after a `403` this
  source asks `/api/me.json`, which answers `200` with the account for a cookie
  Reddit accepts and `403` otherwise. Only that answer's `kind` is read: the
  rest is the account's own data, and none of it is kept or logged.
* `429` carries `x-ratelimit-reset`, the seconds until the window resets.

**How a post becomes items**, best first, falling back on anything missing:

1. Reddit's own media: a gallery's pictures in `gallery_data` order (not
   `media_metadata`'s, which differs), a video's `fallback_url`, a GIF as its
   mp4 rendition.
2. Reddit's copy of linked media, which works for any host: its
   `reddit_video_preview` (redgifs carries one), its mp4 rendition of a GIF, and
   otherwise its copy of the picture.
3. The post's own URL, when it ends in a media extension.

A crosspost carries its media only on the original (`crosspost_parent_list`),
so media come from there; the caption stays the crosspost's, since that is the
post the listing shows. **Something that moves is never shown as a still of
itself.** A video page (`post_hint` `rich:video`, which is how YouTube arrives)
or an imgur `.gifv` with no moving copy on Reddit is unresolved, like a post with
nothing at all (a gfycat link: the host is gone). Unresolved posts are counted by
domain, so a host worth a resolver shows up by name.
"""

import json
import logging
from collections import Counter
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any, Final, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from arrt_player.sources import Caption, Item, Kind, Listing, Outcome
from arrt_player.sources.fetch import REDDIT_HOST, UNREACHABLE, ApiAnswer, FetchRefused

log = logging.getLogger(__name__)

#: The hosts a listing URL may be written with. Each is read from www, the one
#: host the cookie is sent to.
REDDIT_HOSTS: Final[frozenset[str]] = frozenset({"reddit.com", REDDIT_HOST, "old.reddit.com", "new.reddit.com"})

#: Answers `200` with the account for a session Reddit accepts, `403` otherwise.
ME_URL: Final[str] = f"https://{REDDIT_HOST}/api/me.json"

#: The most a listing page holds.
LISTING_LIMIT: Final[int] = 100

#: A post's URL that is itself a media file, by its extension, and what it is.
_MEDIA_EXTENSIONS: Final[dict[str, Kind]] = {
    ".jpg": Kind.STILL,
    ".jpeg": Kind.STILL,
    ".png": Kind.STILL,
    ".webp": Kind.STILL,
    ".gif": Kind.CLIP,
    ".mp4": Kind.CLIP,
}


#: A listing's path has at least `/r/<name>`: the bare site, or `/r`, lists
#: nothing of anyone's choosing.
_LISTING_DEPTH: Final[int] = 2


class ApiClient(Protocol):
    async def get_api(self, url: str) -> ApiAnswer: ...


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


def caption_of(post: dict[str, Any]) -> Caption:
    author = post.get("author")
    score = post.get("score")
    return Caption(
        title=str(post.get("title") or ""),
        community=str(post.get("subreddit_name_prefixed") or f"r/{post.get('subreddit', '')}"),
        author=f"u/{author}" if author else "",
        score=score if isinstance(score, int) else None,
    )


def items_of(post: dict[str, Any]) -> list[Item]:
    """The items one post shows, in order; none when nothing in it can be shown."""
    caption = caption_of(post)
    source = _media_post(post)
    gallery = _gallery(post["name"], source, caption)
    if gallery is not None:
        return gallery
    found = _reddit_media(source) or _reddit_copy(source) or _direct(source)
    if found is None:
        return []
    url, kind, tier = found
    return [Item(id=post["name"], media_url=url, kind=kind, caption=caption, tier=tier)]


def parse_listing(document: object) -> tuple[list[Item], Counter[str]]:
    """Every item in a listing, in its order, and the posts that gave none, by domain.

    Raises `ValueError` if it is not a listing.
    """
    try:
        children: Sequence[Any] = document["data"]["children"]  # type: ignore[index]
        posts = [child["data"] for child in children if child.get("kind") == "t3"]
    except (KeyError, TypeError) as error:
        raise ValueError(f"not a listing: {error!r}") from None
    items: list[Item] = []
    unresolved: Counter[str] = Counter()
    for post in posts:
        try:
            found = items_of(post)
        except (KeyError, TypeError, ValueError) as error:
            # One post Reddit sent in a shape this does not know costs that post only.
            log.warning("slideshow.reddit.post_unreadable post=%s error=%r", post.get("name"), error)
            found = []
        if found:
            items += found
        else:
            unresolved[str(post.get("domain") or "unknown")] += 1
    return items, unresolved


class RedditSource:
    """One listing, read through an `ApiClient` that holds the cookie (`fetch.Fetcher`)."""

    def __init__(self, url: str, client: ApiClient) -> None:
        self.url = listing_url(url)
        self._client = client

    def __repr__(self) -> str:
        return f"RedditSource({self.url!r})"

    async def listing(self) -> Listing:
        try:
            answer = await self._client.get_api(self.url)
        except (FetchRefused, *UNREACHABLE) as error:
            return _logged(Listing(Outcome.UNREACHABLE, detail=f"Reddit could not be reached: {type(error).__name__}"))
        if answer.status == HTTPStatus.OK:
            return _logged(_listed(answer))
        if answer.status == HTTPStatus.FORBIDDEN:
            return _logged(await self._why_refused())
        if answer.status == HTTPStatus.TOO_MANY_REQUESTS:
            return _logged(
                Listing(
                    Outcome.RATE_LIMITED,
                    retry_after_s=answer.ratelimit_reset_s,
                    detail="Reddit asked us to slow down",
                )
            )
        return _logged(Listing(Outcome.UNREACHABLE, detail=f"Reddit answered {answer.status}"))

    async def _why_refused(self) -> Listing:
        """A 403 is a refused listing or a refused session, and only asking about the session says which."""
        try:
            me = await self._client.get_api(ME_URL)
        except (FetchRefused, *UNREACHABLE) as error:
            return Listing(
                Outcome.UNREACHABLE, detail=f"Reddit refused the listing, then could not be reached: {type(error).__name__}"
            )
        if me.status == HTTPStatus.OK and _is_account(me.body):
            return Listing(Outcome.REFUSED, detail="Reddit refuses this listing; try its top listing instead")
        return Listing(Outcome.COOKIE_REJECTED, detail="Reddit no longer accepts the session cookie")


def _listed(answer: ApiAnswer) -> Listing:
    try:
        items, unresolved = parse_listing(json.loads(answer.body))
    except ValueError as error:
        return Listing(Outcome.UNREACHABLE, detail=f"Reddit's answer was not a listing ({str(error)[:80]})")
    return Listing(Outcome.LISTED, items=tuple(items), unresolved=unresolved)


def _is_account(body: bytes) -> bool:
    try:
        document = json.loads(body)
    except ValueError:
        return False
    return isinstance(document, dict) and document.get("kind") == "t2"


def _logged(listing: Listing) -> Listing:
    if listing.outcome is Outcome.LISTED:
        log.info(
            "slideshow.reddit.listed items=%d unresolved=%s",
            len(listing.items),
            ",".join(f"{domain}:{count}" for domain, count in listing.unresolved.most_common()) or "none",
        )
    else:
        log.warning("slideshow.reddit.%s detail=%s", listing.outcome.value, listing.detail)
    return listing


def _media_post(post: dict[str, Any]) -> dict[str, Any]:
    """The post whose media to read: a crosspost's original, which is where Reddit puts them."""
    parents = post.get("crosspost_parent_list") or []
    return parents[0] if parents and isinstance(parents[0], dict) else post


def _gallery(name: str, post: dict[str, Any], caption: Caption) -> list[Item] | None:
    """A gallery's items in its own order, or None when the post is not a gallery."""
    if not post.get("is_gallery"):
        return None
    metadata = post.get("media_metadata") or {}
    items: list[Item] = []
    for entry in (post.get("gallery_data") or {}).get("items", []):
        media_id = entry["media_id"]
        found = _gallery_media(metadata.get(media_id) or {})
        if found is not None:
            url, kind = found
            items.append(Item(id=f"{name}-{media_id}", media_url=url, kind=kind, caption=caption, tier="reddit"))
    return items


def _gallery_media(metadata: dict[str, Any]) -> tuple[str, Kind] | None:
    """One gallery entry's media. An animated entry is played as its mp4, never its GIF, when it has one.

    Only `Image` entries were seen in the 2026-11-10 probe (about 280 entries over
    about 1,000 posts); the `AnimatedImage` reading follows Reddit's documented
    shape and is unmeasured.
    """
    if metadata.get("status") != "valid":
        return None
    biggest = metadata.get("s") or {}
    if metadata.get("e") == "AnimatedImage":
        if biggest.get("mp4"):
            return biggest["mp4"], Kind.CLIP
        return (biggest["gif"], Kind.CLIP) if biggest.get("gif") else None
    return (biggest["u"], Kind.STILL) if biggest.get("u") else None


def _reddit_media(post: dict[str, Any]) -> tuple[str, Kind, str] | None:
    """Media Reddit hosts itself: a v.redd.it video, or an i.redd.it picture or GIF."""
    video = (post.get("secure_media") or post.get("media") or {}).get("reddit_video") or {}
    if video.get("fallback_url"):
        return video["fallback_url"], Kind.CLIP, "reddit"
    if post.get("domain") == "i.redd.it":
        url = str(post.get("url") or "")
        if url.lower().endswith(".gif"):
            rendition = _mp4_rendition(post)
            return (rendition, Kind.CLIP, "reddit") if rendition else (url, Kind.CLIP, "reddit")
        kind = _MEDIA_EXTENSIONS.get(_extension(url))
        if kind is not None:
            return url, kind, "reddit"
    return None


def _reddit_copy(post: dict[str, Any]) -> tuple[str, Kind, str] | None:
    """Reddit's copy of what a link points at; None for something that moves and has no moving copy."""
    preview = post.get("preview") or {}
    moving = (preview.get("reddit_video_preview") or {}).get("fallback_url")
    if moving:
        return moving, Kind.CLIP, "reddit-copy"
    rendition = _mp4_rendition(post)
    if rendition:
        return rendition, Kind.CLIP, "reddit-copy"
    if post.get("post_hint") == "rich:video" or str(post.get("url") or "").lower().endswith(".gifv"):
        return None
    images = preview.get("images") or []
    source = (images[0].get("source") or {}).get("url") if images else None
    return (source, Kind.STILL, "reddit-copy") if source else None


def _direct(post: dict[str, Any]) -> tuple[str, Kind, str] | None:
    """The post's own URL, when it is a media file."""
    url = str(post.get("url_overridden_by_dest") or post.get("url") or "")
    kind = _MEDIA_EXTENSIONS.get(_extension(url))
    return (url, kind, "direct") if kind is not None and url.startswith(("https://", "http://")) else None


def _mp4_rendition(post: dict[str, Any]) -> str | None:
    images = (post.get("preview") or {}).get("images") or []
    if not images:
        return None
    return ((images[0].get("variants") or {}).get("mp4") or {}).get("source", {}).get("url")


def _extension(url: str) -> str:
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    return path[dot:] if dot > path.rfind("/") else ""
