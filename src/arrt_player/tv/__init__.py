"""The television boundary.

**Only the interface is exported here.** The concrete `SamsungTv` is imported by
name, from the one place that builds it, so that importing `arrt_player.tv` does not
drag the `samsungtvws` fork in behind it. That keeps the seam honest in a way a
convenience re-export would quietly undo: everything above this line is written
against `TvClient` and can be exercised without a television or its library.
"""

from arrt_player.tv.client import (
    READS,
    UPLOADED_CATEGORY,
    WRITES,
    Gate,
    IdentityUnreadable,
    PowerStateUnreadable,
    RemovalOutcome,
    SelectionAnnouncement,
    SelectionObserver,
    TvClient,
    TvNotOurs,
    TvRemovalUnconfirmed,
    TvUnavailable,
    TvUploadFailed,
    upload_marker,
)

__all__ = [
    "READS",
    "UPLOADED_CATEGORY",
    "WRITES",
    "Gate",
    "IdentityUnreadable",
    "PowerStateUnreadable",
    "RemovalOutcome",
    "SelectionAnnouncement",
    "SelectionObserver",
    "TvClient",
    "TvNotOurs",
    "TvRemovalUnconfirmed",
    "TvUnavailable",
    "TvUploadFailed",
    "upload_marker",
]
