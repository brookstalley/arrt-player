"""The settings the page writes, in one file in `CACHE_DIR`.

**Beneath the environment, never above it.** `config.load` reads a key from here
only when neither the environment nor `.env` sets it, so there is one rule to
predict a value by: whatever is exported wins, and the page shows such a key as
read-only and says why. To manage a key from the page, delete it from `.env`.

**In `CACHE_DIR`, under a leading dot.** `CACHE_DIR` is the one directory the
Player already owns and writes; the checkout may not be writable by the account
the unit runs as. A wall id may not begin with a dot (`config.ClientSettings.wall`),
so no wall's directory can be named this, as with `.client.json`.

**The layout is persisted, so it is versioned.** It answers four questions: which
keys the page set and to what (`values`); whether a password is set and whether
one is right (`auth.password`, a scrypt encoding from `auth.py`); whether a session
cookie is genuine, under a secret that survives the restart a save causes
(`auth.session_secret`); and which layout this is (`version`), so a later mode can
add its keys without guessing what an older file meant. A version this code does
not know is refused rather than read in part.

**It holds `CLIENT_TOKEN` in the clear**, as `.env` does: the Player has to send
it. The file is created 0600, and written by temp-and-rename with an `fsync`
between, the same discipline as `heartbeat.write`, because a Pi on an SD card with
no UPS can otherwise keep a file that exists and holds nothing.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: The file, directly under `CACHE_DIR`.
SETTINGS_FILENAME: Final[str] = ".settings.json"

#: The layout this code reads and writes.
VERSION: Final[int] = 1

#: The keys the page may set. Everything else stays in `.env`: a page that could
#: move `CACHE_DIR` or its own port could lose its own file or lock itself out.
#: `TV_CLIENT_NAME` is absent on purpose, because changing it costs a pairing
#: prompt at the television.
MANAGED_KEYS: Final[tuple[str, ...]] = ("SERVER_URL", "CLIENT_TOKEN", "PLAYER_NAME")

#: Owner read and write only: the file holds the client token.
FILE_MODE: Final[int] = 0o600


class SettingsUnreadable(ValueError):
    """The file exists and cannot be trusted: not JSON, the wrong shape, or a version this code does not know."""


@dataclass(frozen=True)
class Stored:
    """What the file holds. An absent file is the empty value, not an error."""

    values: Mapping[str, str] = field(default_factory=dict)
    password: str | None = field(default=None, repr=False)
    session_secret: str | None = field(default=None, repr=False)

    def with_values(self, values: Mapping[str, str]) -> "Stored":
        return Stored(values=dict(values), password=self.password, session_secret=self.session_secret)

    def with_auth(self, *, password: str, session_secret: str) -> "Stored":
        return Stored(values=dict(self.values), password=password, session_secret=session_secret)


def path_in(cache_dir: Path) -> Path:
    return cache_dir / SETTINGS_FILENAME


def read(cache_dir: Path) -> Stored:
    """The file's contents, or `Stored()` when there is no file. Raises `SettingsUnreadable`."""
    path = path_in(cache_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Stored()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsUnreadable(f"{path} is not JSON ({exc.msg})") from exc
    if not isinstance(document, dict):
        raise SettingsUnreadable(f"{path} does not hold an object")
    if document.get("version") != VERSION:
        raise SettingsUnreadable(f"{path} is layout version {document.get('version')!r}, and this Player reads version {VERSION}")
    values = document.get("values", {})
    auth = document.get("auth", {})
    if not isinstance(values, dict) or not isinstance(auth, dict):
        raise SettingsUnreadable(f"{path} has 'values' or 'auth' that is not an object")
    unknown = sorted(set(values) - set(MANAGED_KEYS))
    if unknown:
        raise SettingsUnreadable(f"{path} sets {', '.join(unknown)}, which the page does not manage")
    if not all(isinstance(value, str) for value in values.values()):
        raise SettingsUnreadable(f"{path} has a value that is not text")
    password, secret = auth.get("password"), auth.get("session_secret")
    if not all(item is None or isinstance(item, str) for item in (password, secret)):
        raise SettingsUnreadable(f"{path} has an 'auth' entry that is not text")
    return Stored(values=values, password=password, session_secret=secret)


def write(cache_dir: Path, stored: Stored) -> None:
    """Replace the file atomically, owner-only. Raises `OSError` on failure."""
    document: dict[str, object] = {"version": VERSION, "values": dict(stored.values), "auth": {}}
    auth: dict[str, str] = {}
    if stored.password is not None:
        auth["password"] = stored.password
    if stored.session_secret is not None:
        auth["session_secret"] = stored.session_secret
    document["auth"] = auth

    # A first start in setup mode may be the first thing to touch `CACHE_DIR`:
    # nothing has pulled yet, so nothing has made it.
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = path_in(cache_dir)
    temporary = path.with_name(f"{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    # O_CREAT's mode applies only to a file it creates; a temp file left by a
    # crash keeps whatever mode it had.
    temporary.chmod(FILE_MODE)
    temporary.replace(path)
