"""imgur: a `.gifv` or `.gif` as the same id's `.mp4`, and a direct picture as it is.

Reddit's copy of an imgur `.gifv` is sometimes only a still (one in the
2026-11-10 live listing), and a still of something that moves is never shown. The
same id's `.mp4` on `i.imgur.com` was measured on 2026-11-10: `200 video/mp4`, or
a `301` to the same id in another case, which the downloader follows. A deleted
id redirects to imgur's HTML home page, which the downloader's content check
refuses, so the item falls back to Reddit's copy.

An imgur page or album (`imgur.com/<id>`, `imgur.com/a/<id>`) is not read: it
shows its cover image, which is Reddit's copy (the owner accepted this,
2026-11-10). No network call is made here.
"""

import re
from typing import Final
from urllib.parse import urlsplit

from arrt_player.sources import Kind, Resolved

#: imgur's media host, where every rewritten URL points.
MEDIA_HOST: Final[str] = "i.imgur.com"

_ID_AND_EXTENSION: Final = re.compile(r"/([A-Za-z0-9]{5,12})\.(gifv|gif|mp4|jpg|jpeg|png)")
_MOVING: Final = frozenset({"gifv", "gif", "mp4"})


class ImgurResolver:
    name = "imgur"
    domains: frozenset[str] = frozenset({MEDIA_HOST, "imgur.com", "m.imgur.com"})

    def claims(self, link: str) -> Kind | None:
        found = self._read(link)
        return None if found is None else (Kind.CLIP if found[1] in _MOVING else Kind.STILL)

    async def resolve(self, link: str) -> Resolved | None:
        found = self._read(link)
        if found is None:
            return None
        media_id, extension = found
        if extension in _MOVING:
            return Resolved(f"https://{MEDIA_HOST}/{media_id}.mp4", Kind.CLIP)
        return Resolved(f"https://{MEDIA_HOST}/{media_id}.{extension}", Kind.STILL)

    def _read(self, link: str) -> tuple[str, str] | None:
        parts = urlsplit(link)
        if (parts.hostname or "").lower() not in self.domains:
            return None
        match = _ID_AND_EXTENSION.fullmatch(parts.path)
        return (match.group(1), match.group(2).lower()) if match else None
