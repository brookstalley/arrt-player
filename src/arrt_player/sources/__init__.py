"""Where a slideshow's items come from: a `Source` gives an ordered `Listing` of `Item`s.

**A source is an interface so another can replace Reddit.** The slideshow
programme reads a listing and plays it; it never knows the listing came from
Reddit. `reddit.RedditSource` is the first implementation.

**An item names one thing to show**, a still or a clip, with the caption the
e-paper label draws for it. A Reddit gallery becomes one item per picture, so an
item's id is the post's id plus, for a gallery, the picture's place in it. The
media cache names its files by that id, which is why `ITEM_ID` limits its
characters: it becomes a file name.

**A listing that could not be read keeps its reason** (`Outcome`), because each
reason means something different to the person at the settings page: Reddit
refusing this listing is fixed by choosing another, a cookie Reddit no longer
accepts is fixed by pasting a new one, and neither is the same as Reddit being
down.

**`Resolver`** is the seam for a host whose own media beats Reddit's copy of it
(redgifs' HD, imgur's mp4). One is keyed by the domains it serves, and is asked
when an item is about to be downloaded, not when the listing is read: only a
download shows whether its answer is any good, and asking for every post at every
refresh would spend a host's API calls on items that are already cached
(`resolvers.Resolvers` and `cache.MediaCache` carry it out).
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Protocol

#: What an item id may be: it becomes a file name in the media cache, so no
#: separator, no dot and nothing a shell or a path would read specially.
ITEM_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


class Kind(Enum):
    """How an item is shown: a still for the dwell's minimum, a clip by the dwell rule."""

    STILL = "still"
    CLIP = "clip"


@dataclass(frozen=True)
class Caption:
    """What the e-paper label says about an item. `score` is Reddit's count when the listing was fetched."""

    title: str
    community: str
    author: str
    score: int | None = None


@dataclass(frozen=True)
class Item:
    """One thing to show. `tier` names how its media was found, so a host worth a resolver shows up.

    `link` and `domain` are the post's own, for a resolver to read. `media_url`
    is empty when only a resolver can give the item anything.
    """

    id: str
    media_url: str
    kind: Kind
    caption: Caption
    tier: str
    link: str = ""
    domain: str = ""

    def __post_init__(self) -> None:
        if not ITEM_ID.fullmatch(self.id):
            raise ValueError(f"an item id must match {ITEM_ID.pattern}, so it can name a file: {self.id!r}")


class Outcome(Enum):
    """How the last attempt to read a listing ended."""

    LISTED = "listed"
    #: The listing answered 403 while the session is accepted. Reddit refuses some listings (`/new`, `/hot`,
    #: `/rising`) even to a logged-in session; another listing may answer.
    REFUSED = "refused"
    #: Reddit no longer accepts the session cookie (`reddit.py` says how that is told).
    COOKIE_REJECTED = "cookie_rejected"
    #: 429. `Listing.retry_after_s` says how long Reddit asked us to wait.
    RATE_LIMITED = "rate_limited"
    #: No answer, a timeout, a 5xx, or an answer that is not a listing.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Listing:
    """A source's answer. Only `LISTED` carries items; the others say why there are none."""

    outcome: Outcome
    items: tuple[Item, ...] = ()
    #: Posts nothing could resolve, counted by the domain they link to, so a host
    #: that would be worth a resolver shows up by name.
    unresolved: Counter[str] = field(default_factory=Counter)
    retry_after_s: float | None = None
    #: A short reason in words, for the journal and the page. Never a secret.
    detail: str = ""


class Source(Protocol):
    """Gives the slideshow its listing."""

    async def listing(self) -> Listing: ...


@dataclass(frozen=True)
class Candidate:
    """One place an item's media may be, and the tier that names it."""

    url: str
    tier: str


@dataclass(frozen=True)
class Resolved:
    """A resolver's answer: where the media is, and what it should be."""

    url: str
    kind: Kind


class Resolver(Protocol):
    """Gives better media than Reddit's copy for links on `domains`, which it names."""

    name: str
    domains: frozenset[str]

    def claims(self, link: str) -> Kind | None:
        """What this link would give, if it is one this resolver reads; None otherwise. No network."""
        ...

    async def resolve(self, link: str) -> Resolved | None:
        """The media for a claimed link, or None when the host has none for it. May raise a fetch error."""
        ...
