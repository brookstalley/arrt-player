"""Host resolvers, keyed by domain, and the order an item's media is tried in.

**A resolver is asked when an item is about to be downloaded** (`Resolvers.candidates`,
which the media cache calls). Its answer goes first and Reddit's copy second, and
the cache keeps the first that downloads. So a resolver that fails, says the host
has nothing, or gives a URL that turns out not to be media never costs the item
its Reddit copy. Each fall-back is said in the journal, and the cache counts which
tier each download came from, so a resolver that quietly falls back all the time
shows up.

**One class per host.** A resolver names the domains it serves, and the registry
refuses two that claim the same one, so which resolver reads a link is never a
question of order.
"""

import logging
from collections.abc import Sequence
from typing import Final

from arrt_player.sources import Candidate, Item, Kind, Resolver
from arrt_player.sources.fetch import UNREACHABLE, FetchRefused

log = logging.getLogger(__name__)

#: What a resolver may raise that costs only its own answer: a refused or failed
#: fetch, or a host answering in a shape it does not read.
RESOLVER_FAILURES: Final = (FetchRefused, *UNREACHABLE, ValueError, KeyError, TypeError)


class Resolvers:
    """The resolvers in use, by domain."""

    def __init__(self, resolvers: Sequence[Resolver]) -> None:
        self._by_domain: dict[str, Resolver] = {}
        for resolver in resolvers:
            for domain in resolver.domains:
                if domain in self._by_domain:
                    raise ValueError(f"{domain} is claimed by both {self._by_domain[domain].name} and {resolver.name}")
                self._by_domain[domain] = resolver

    def claims(self, domain: str, link: str) -> Kind | None:
        """What a resolver would give for this link, or None when no resolver reads it."""
        resolver = self._by_domain.get(domain.lower())
        return resolver.claims(link) if resolver is not None else None

    async def candidates(self, item: Item) -> list[Candidate]:
        """Where to look for an item's media, best first: its host's resolver, then Reddit's copy."""
        found: list[Candidate] = []
        resolver = self._by_domain.get(item.domain.lower())
        if resolver is not None and resolver.claims(item.link) is not None:
            try:
                resolved = await resolver.resolve(item.link)
            except RESOLVER_FAILURES as error:
                resolved = None
                _fell_back(resolver.name, item, f"{type(error).__name__}: {str(error)[:120]}")
            else:
                if resolved is None:
                    _fell_back(resolver.name, item, "the host has nothing for this link")
            if resolved is not None:
                found.append(Candidate(resolved.url, f"host:{resolver.name}"))
        if item.media_url:
            found.append(Candidate(item.media_url, item.tier))
        return found


def _fell_back(name: str, item: Item, reason: str) -> None:
    then = "Reddit's copy" if item.media_url else "nothing"
    log.warning(
        "a host could not give its own copy",
        extra={"event": "slideshow.resolver.fallback", "resolver": name, "item": item.id, "to": then, "reason": reason},
    )
