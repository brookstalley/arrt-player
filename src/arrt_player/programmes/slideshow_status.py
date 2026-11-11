"""What a running slideshow tells the settings page: Reddit's last answer, the cache's last fill, what is up.

**Its own module so the page can read it without importing the slideshow.** The
programme reaches the network client through the media cache, and the
plane-isolation guard keeps that client's importers to `sources/`, the programme
and the entry point. This holds plain values only.
"""

from collections import Counter
from dataclasses import dataclass, field

from arrt_player.sources import Outcome


@dataclass
class SlideshowStatus:
    """Written by `programmes.slideshow.Slideshow`, read by the page. One event loop, so no lock."""

    #: How the last attempt to read the listing ended; None before the first answer.
    outcome: Outcome | None = None
    #: The source's own words for it. Never a secret.
    detail: str = ""
    #: Wall time of that answer.
    answered_at: float | None = None
    #: The items in the listing being played, and the posts that gave none, by domain.
    items: int = 0
    unresolved: Counter[str] = field(default_factory=Counter)
    #: True while the listing being played is the one saved before this start,
    #: because Reddit has not listed anything since.
    from_saved: bool = False
    #: Downloads since this start, and by the tier that gave each, so a resolver
    #: that always falls back to Reddit's copy shows on the page.
    downloaded: int = 0
    tiers: Counter[str] = field(default_factory=Counter)
    #: The last fill's failures, and whether it stopped at the cap or for the network.
    failed: int = 0
    cap_reached: bool = False
    network_down: bool = False
    #: The title of what is on the screen, or None when nothing is.
    showing: str | None = None
    #: What was found at start that would stop the screen being drawn (`mpv.renderer_problems`).
    renderer_problems: tuple[str, ...] = ()
