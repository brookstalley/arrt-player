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
(redgifs' HD, imgur's mp4). One is keyed by the domains it serves.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, Protocol

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
    """One thing to show. `tier` names how its media was found, so a host worth a resolver shows up."""

    id: str
    media_url: str
    kind: Kind
    caption: Caption
    tier: str

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


class Resolver(Protocol):
    """Finds a better media URL for a post on one of `domains` than Reddit's own copy, or None."""

    domains: frozenset[str]

    async def resolve(self, post: dict[str, Any]) -> tuple[str, Kind] | None: ...
