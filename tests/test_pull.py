"""Pulling a wall into its own cache, and rendering only from it.

Against the server double (`server_double.py`), which serves the contract's own
documents on the contract's own routes (`contract/routes.json`), so these tests
pin the client to the contract rather than to Arrt's code, which a Player in
another repository will not have. `player-contract.md` § Transport is the
specification.

**The test this chunk exists for is the one that stops the server while the wall
runs**: the cache is the only thing the wall renders from, so a server that goes
away must change nothing about what is on the wall.
"""

import asyncio
import errno
import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import timedelta

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from conftest import WALL_ID, tick_until
from fakes import RecordingOutput
from PIL import Image
from server_double import CONTRACT, ROUTES, TOKEN
from server_double import ServerDouble as Stub

from arrt_player import pull as pull_module
from arrt_player.displays.frame import frame_wall
from arrt_player.displays.screen import screen_wall
from arrt_player.manifest import REQUESTED_MAJORS, Feed, Watcher, media_name
from arrt_player.pull import (
    BACKOFF_START_SECONDS,
    CLIENT_ETAG_FILENAME,
    CLIENT_HEARTBEAT_ROUTE,
    CLIENT_ROUTE,
    ETAG_FILENAME,
    HEARTBEAT_ROUTE,
    HIGHER_MAJOR_SECONDS,
    LABEL_ROUTE,
    MANIFEST_MAJOR_ROUTE,
    MEDIA_DIRNAME,
    CachedLabel,
    ClientPull,
    LabelAnswer,
    LabelPull,
    Pull,
)

#: How long a feed is held while a master fails for a reason that may clear:
#: ten minutes, the plan's decision, stated here rather than read from the module.
HOLD_SECONDS = 10 * 60
#: The most of a client or label document the Player reads: 1 MiB, likewise.
DOCUMENT_CAP_BYTES = 1024 * 1024
#: How often a cached label document is stamped as answered while the server
#: sends nothing new: a minute, likewise.
LABEL_STAMP_SECONDS = 60


@pytest.fixture
async def stub():
    stub = Stub()
    server = TestServer(stub.app())
    await server.start_server()
    stub.server = server
    stub.url = str(server.make_url("")).rstrip("/")
    yield stub
    await server.close()


@pytest.fixture
def http_settings(settings, stub):
    """The fixture wall on the Frame, pointed at the double."""
    return replace(settings, server_url=stub.url)


@pytest.fixture
def pull(http_settings):
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    return Pull(http_settings)


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def _watcher(settings) -> Watcher:
    return Watcher(settings.manifest_path)


def _works(settings) -> list[str]:
    """The works of the feed in the cache, in the order it names them."""
    return list(_cached(settings)["works"])


def _media(document: dict, work_id: str) -> str:
    """The name a work's master is cached under."""
    return media_name(document["works"][work_id]["media"]["sha256"])


def _cached(settings) -> dict | None:
    path = settings.manifest_path
    return json.loads(path.read_text()) if path.exists() else None


def _held(settings) -> set[str]:
    return {path.name for path in (settings.wall_dir / MEDIA_DIRNAME).iterdir()}


#: A wall heartbeat the contract's schema accepts, which is the only kind the double takes.
_A_HEARTBEAT = json.loads((CONTRACT / "fixtures" / "heartbeat.v1" / "valid" / "fresh-start-no-panel.json").read_text())


def _heartbeat(reported_at: str, **changes: object) -> str:
    """A wall heartbeat file's text: a valid report made at `reported_at`."""
    return json.dumps({**_A_HEARTBEAT, "reported_at": reported_at, **changes})


def _heartbeat_posts(stub) -> int:
    """How many wall heartbeats reached the double, accepted or not."""
    return stub.requests.count(("POST", HEARTBEAT_ROUTE.format(wall_id=WALL_ID)))


async def _until(done, *, within: float = 5.0) -> None:
    """Wait for `done` on a running loop, failing rather than passing on the budget."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not done():
        if loop.time() > deadline:
            raise AssertionError(f"not done after {within} s")
        await asyncio.sleep(0.01)


# -- the contract's routes ------------------------------------------------------------


def test_the_client_requests_the_routes_the_contract_names():
    assert ROUTES["manifest_major"]["path"] == MANIFEST_MAJOR_ROUTE
    assert ROUTES["heartbeat"]["path"] == HEARTBEAT_ROUTE
    assert ROUTES["client"]["path"] == CLIENT_ROUTE
    assert ROUTES["client_heartbeat"]["path"] == CLIENT_HEARTBEAT_ROUTE
    assert ROUTES["manifest_major"]["method"] == "GET"
    assert ROUTES["heartbeat"]["method"] == "POST"
    assert ROUTES["client"]["method"] == "GET"
    assert ROUTES["client_heartbeat"]["method"] == "POST"
    assert ROUTES["label"]["path"] == LABEL_ROUTE
    assert ROUTES["label"]["method"] == "GET"


def test_a_wall_reads_and_renders_from_its_own_directory_in_the_cache(http_settings, cache_dir):
    assert http_settings.manifest_path == cache_dir / WALL_ID / "manifest.json"
    assert http_settings.render_root == cache_dir / WALL_ID


# -- adopting ------------------------------------------------------------------------------


async def test_a_new_feed_is_cached_as_sent_with_its_masters_verified(pull, stub, session, http_settings):
    published = stub.publish("w1", "w2")

    assert await pull.cycle(session) is True

    assert _cached(http_settings) == published, "the feed is the server's, and the wall reads it as sent"
    for work in published["works"].values():
        held = http_settings.wall_dir / MEDIA_DIRNAME / media_name(work["media"]["sha256"])
        assert hashlib.sha256(held.read_bytes()).hexdigest() == work["media"]["sha256"]
    adopted = _watcher(http_settings).poll()
    assert list(adopted.works) == ["w1", "w2"]


async def test_a_feed_is_cached_whole_with_every_works_media_verified(pull, stub, session, http_settings):
    """Staged works included, so applying a scene is a switch rather than a download."""
    published = stub.publish_feed([("w1", "2026-06-21T08:00:00+00:00", "2026-06-21T13:00:00+00:00")], staging=("w7",))

    assert await pull.cycle(session) is True

    assert _cached(http_settings) == published, "a feed is cached as the server sent it"
    for work in published["works"].values():
        held = http_settings.wall_dir / MEDIA_DIRNAME / f"sha256-{work['media']['sha256']}"
        assert hashlib.sha256(held.read_bytes()).hexdigest() == work["media"]["sha256"]
    adopted = _watcher(http_settings).poll()
    assert isinstance(adopted, Feed)
    assert sorted(adopted.works) == ["w1", "w7"]


async def test_a_feed_whose_one_master_is_bad_is_cached_whole_without_it(pull, stub, session, http_settings):
    """Every work the schedule names stays in the feed; the one with no good media is the programme's to skip.

    The server holds bytes for w2 that do not match its hash, the failure the
    hash exists to catch.
    """
    published = stub.publish_feed(
        [
            ("w1", "2026-06-21T08:00:00+00:00", "2026-06-21T13:00:00+00:00"),
            ("w2", "2026-06-21T13:00:00+00:00", "2026-06-21T18:00:00+00:00"),
        ],
    )
    stub.media[published["works"]["w2"]["media"]["sha256"]] = b"not the master that was hashed"

    assert await pull.cycle(session) is True

    assert _cached(http_settings) == published
    assert _held(http_settings) == {f"sha256-{published['works']['w1']['media']['sha256']}"}


async def test_a_feeds_media_that_cannot_be_fetched_keeps_the_manifest_already_cached(pull, stub, session, http_settings):
    stub.publish("w1")
    assert await pull.cycle(session) is True
    stub.publish_feed([("w2", "2026-06-21T08:00:00+00:00", "2026-06-21T13:00:00+00:00")])
    stub.media_status = 503

    assert await pull.cycle(session) is False

    assert _works(http_settings) == ["w1"], "a feed was cached without its media"


async def test_a_feeds_media_is_evicted_once_two_documents_in_a_row_have_not_named_it(pull, stub, session, http_settings):
    first = stub.publish_feed([("w1", "2026-06-21T08:00:00+00:00", "2026-06-21T13:00:00+00:00")])
    await pull.cycle(session)
    stub.publish_feed([("w2", "2026-06-21T08:00:00+00:00", "2026-06-21T13:00:00+00:00")])
    await pull.cycle(session)
    w1 = f"sha256-{first['works']['w1']['media']['sha256']}"
    assert w1 in _held(http_settings), "the media the feed being replaced names went at once"

    stub.publish_feed([("w3", "2026-06-21T08:00:00+00:00", "2026-06-21T13:00:00+00:00")])
    await pull.cycle(session)

    assert w1 not in _held(http_settings)


async def test_nothing_is_cached_while_a_master_cannot_be_fetched(pull, stub, session, http_settings):
    stub.publish("w1")
    stub.media_status = 503

    await pull.cycle(session)
    assert _cached(http_settings) is None, "a feed was cached before its master"

    stub.media_status = None
    await pull.cycle(session)
    assert _works(http_settings) == ["w1"]


async def test_an_unchanged_manifest_is_not_downloaded_again(pull, stub, session, http_settings):
    stub.publish("w1")
    await pull.cycle(session)
    etag = (http_settings.wall_dir / ETAG_FILENAME).read_text()

    before = http_settings.manifest_path.stat().st_mtime_ns
    await pull.cycle(session)

    assert (http_settings.wall_dir / ETAG_FILENAME).read_text() == etag
    assert http_settings.manifest_path.stat().st_mtime_ns == before


async def test_a_feed_served_with_no_etag_drops_the_etag_of_the_feed_it_replaced(pull, stub, session, http_settings):
    """The cached ETag describes the cached manifest, so a manifest adopted
    without one leaves none. Kept, it would name a document no longer here: when
    the server goes back to that document, it answers the old ETag with 304, and
    the wall keeps the feed the server has stopped serving."""
    stub.publish("w1")
    await pull.cycle(session)
    first = stub.manifests[WALL_ID]
    assert (http_settings.wall_dir / ETAG_FILENAME).is_file(), "the first feed was not cached with its ETag"

    stub.manifest_etags = False
    stub.publish("w2")
    await pull.cycle(session)
    assert _works(http_settings) == ["w2"]
    assert not (http_settings.wall_dir / ETAG_FILENAME).exists()

    stub.manifest_etags = True
    stub.manifests[WALL_ID] = first
    await pull.cycle(session)

    assert _works(http_settings) == ["w1"], "the server went back to the first feed and the wall did not"


async def test_a_manifest_replaced_while_its_etag_cannot_be_written_leaves_no_older_etag(
    pull, stub, session, http_settings, monkeypatch
):
    """A crash or a full disk between the manifest's write and its ETag's must
    not leave the old ETag beside the new manifest, for the reason the test
    above gives."""
    stub.publish("w1")
    await pull.cycle(session)
    etag_path = http_settings.wall_dir / ETAG_FILENAME
    assert etag_path.is_file()

    real_write = pull_module._write_atomically

    def etag_write_fails(path, data: bytes) -> None:
        if path == etag_path:
            raise OSError(28, "No space left on device")
        real_write(path, data)

    monkeypatch.setattr(pull_module, "_write_atomically", etag_write_fails)
    stub.publish("w2")
    with pytest.raises(OSError, match="No space"):
        await pull.cycle(session)

    assert _works(http_settings) == ["w2"]
    assert not etag_path.exists()


async def test_a_master_that_does_not_match_its_hash_is_discarded_and_reported_once(pull, stub, session, http_settings, caplog):
    """The feed is cached whole: the work stays in it, for the programme to pass over until good bytes arrive."""
    stub.publish("w1", "w2")
    bad = stub.manifest["works"]["w2"]["media"]["sha256"]
    stub.media[bad] = b"bytes that are not the master"

    with caplog.at_level(logging.WARNING, logger="arrt_player.pull"):
        await pull.cycle(session)
        stub.publish("w1", "w2")
        stub.media[bad] = b"bytes that are not the master"
        await pull.cycle(session)

    assert _works(http_settings) == ["w1", "w2"]
    assert f"sha256-{bad}" not in _held(http_settings)
    mismatches = [record for record in caplog.records if "did not match its hash" in record.getMessage()]
    assert len(mismatches) == 1


async def test_the_contract_fixtures_placeholder_hash_is_refused_by_the_bytes(pull, stub, session, http_settings):
    """The fixture's own `media` names hashes no real bytes have, so none of its masters reaches the cache."""
    fixture = json.loads((CONTRACT / "fixtures" / "manifest.v2" / "valid" / "a-day-with-dark-hours.json").read_text())
    stub.manifest = fixture
    for work in fixture["works"].values():
        stub.media[work["media"]["sha256"]] = b"anything"

    await pull.cycle(session)

    assert _cached(http_settings) == fixture
    assert _held(http_settings) == set()


async def test_a_major_1_document_served_as_major_2_is_refused_and_never_cached(pull, stub, session, http_settings, caplog):
    """This Player reads major 2 only: a major 1 document is a version it refuses, and the cache keeps the last feed."""
    stub.publish("w1")
    await pull.cycle(session)
    stub.manifest = json.loads((CONTRACT / "fixtures" / "manifest.v2" / "invalid" / "major-1.json").read_text())
    stub.served_at[WALL_ID] = 2

    with caplog.at_level(logging.ERROR, logger="arrt_player.pull"):
        await pull.cycle(session)
        await pull.cycle(session)

    assert _works(http_settings) == ["w1"]
    (refused,) = [record for record in caplog.records if "refusing the manifest" in record.getMessage()]
    assert "major 1 is not supported" in refused.getMessage()


async def test_eviction_removes_a_master_once_two_feeds_in_a_row_have_not_named_it(pull, stub, session, http_settings):
    """One generation of grace.

    The wall adopts a new feed on its next poll, and may reach for a master
    only the old one names until then.
    """
    first = stub.publish("w1", "w2")
    await pull.cycle(session)

    second = stub.publish("w2", "w3")
    await pull.cycle(session)
    names = {work: _media(document, work) for document in (first, second) for work in document["works"]}
    assert _held(http_settings) == {names["w1"], names["w2"], names["w3"]}, "the outgoing feed's master went early"

    third = stub.publish("w3", "w4")
    await pull.cycle(session)
    names["w4"] = _media(third, "w4")
    assert _held(http_settings) == {names["w2"], names["w3"], names["w4"]}
    assert names["w1"] not in _held(http_settings)


async def test_a_stray_file_in_the_media_cache_is_evicted(pull, stub, session, http_settings):
    stray = http_settings.wall_dir / MEDIA_DIRNAME / "sha256-left-over"
    stray.write_bytes(b"from a crash mid-write")
    stub.publish("w1")

    await pull.cycle(session)

    assert not stray.exists()


async def test_a_cache_left_by_a_player_that_read_major_1_gives_way_to_the_first_feed(pull, stub, session, http_settings):
    """A Pi upgraded in place: its cached document is a major 1 manifest, which names renders and no works.

    The pull reads that cache to keep the outgoing document's media one
    generation longer, so it must read a shape it no longer parses without
    failing, and the old renders go once the feed is cached.
    """
    render = "c" * 64
    http_settings.manifest_path.write_text(
        json.dumps(
            {
                "schema": {"major": 1, "minor": 2},
                "entries": [{"work_id": "w0", "render_path": f"media/sha256-{render}", "media": {"sha256": render}}],
            }
        )
    )
    old = http_settings.wall_dir / MEDIA_DIRNAME / f"sha256-{render}"
    old.write_bytes(b"a render composed by the server")
    published = stub.publish("w1")

    assert await pull.cycle(session) is True

    assert _cached(http_settings) == published
    assert _held(http_settings) == {_media(published, "w1")}, "the major 1 render outlived the first feed"


# -- refusals and outages keep the cache ------------------------------------------------------


@pytest.mark.parametrize(
    ("holders", "status"),
    [({}, 401), ({TOKEN: "another-wall"}, 403)],
    ids=["token-unknown", "token-for-another-wall"],
)
async def test_a_refused_token_is_reported_once_and_the_cache_is_kept(
    pull, stub, session, http_settings, caplog, holders, status
):
    stub.publish("w1")
    await pull.cycle(session)
    stub.tokens = holders
    stub.publish("w2")

    with caplog.at_level(logging.ERROR, logger="arrt_player.pull"):
        for _ in range(3):
            assert await pull.cycle(session) is True

    assert _works(http_settings) == ["w1"]
    refused = [record for record in caplog.records if record.getMessage().startswith("the server refused")]
    assert len(refused) == 1
    assert refused[0].status == status


async def test_the_server_stopped_while_the_wall_runs_and_the_schedule_continues_from_the_cache(
    pull, stub, session, http_settings, tv, state, clock
):
    """The test this chunk exists for."""
    stub.publish("w1", "w2")
    await pull.cycle(session)
    daemon = frame_wall(settings=http_settings, tv=tv, state=state, watcher=_watcher(http_settings), clock=clock.as_clock())
    await tick_until(daemon.tick, lambda: tv.on_the_wall is not None)
    first = tv.on_the_wall

    await stub.server.close()
    assert await pull.cycle(session) is False, "a stopped server was reported as reachable"
    clock.advance(181)
    await tick_until(daemon.tick, lambda: tv.on_the_wall != first)

    assert tv.on_the_wall.parent == http_settings.composed_root
    assert _cached(http_settings) is not None


async def test_a_player_restarted_with_the_server_down_starts_from_its_cache(stub, session, http_settings, pull):
    stub.publish("w1", "w2")
    await pull.cycle(session)
    await stub.server.close()

    restarted = Pull(http_settings)
    assert await restarted.cycle(session) is False
    adopted = _watcher(http_settings).poll()

    assert list(adopted.works) == ["w1", "w2"]
    for work in adopted.works.values():
        assert (http_settings.render_root / work.media_path).is_file()


async def test_an_unreachable_server_is_reported_once_and_its_return_once(stub, session, http_settings, caplog):
    stub.publish("w1")
    pull = Pull(http_settings)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    port = stub.server.port
    await stub.server.close()

    with caplog.at_level(logging.INFO, logger="arrt_player.pull"):
        for _ in range(3):
            assert await pull.cycle(session) is False
        stub.server = TestServer(stub.app(), port=port)
        await stub.server.start_server()
        for _ in range(2):
            assert await pull.cycle(session) is True

    messages = [record.getMessage() for record in caplog.records]
    assert len([message for message in messages if "cannot be reached" in message]) == 1
    assert len([message for message in messages if "answers again" in message]) == 1


async def test_a_manifest_404_is_reported_once_as_nothing_published(pull, stub, session, caplog):
    with caplog.at_level(logging.ERROR, logger="arrt_player.pull"):
        for _ in range(3):
            assert await pull.cycle(session) is True

    refused = [record for record in caplog.records if record.getMessage().startswith("the server refused")]
    assert len(refused) == 1
    assert refused[0].status == 404
    assert "publishes no manifest major this Player reads (2)" in refused[0].getMessage()


async def test_a_refusal_ends_on_a_304(pull, stub, session, caplog):
    """The server accepting the token again for a manifest already cached is the end of the refusal."""
    stub.publish("w1")
    await pull.cycle(session)
    holders = stub.tokens
    stub.tokens = {}

    with caplog.at_level(logging.INFO, logger="arrt_player.pull"):
        await pull.cycle(session)
        stub.tokens = holders
        await pull.cycle(session)
        stub.tokens = {}
        await pull.cycle(session)

    messages = [record.getMessage() for record in caplog.records]
    assert len([message for message in messages if message.startswith("the server refused")]) == 2
    assert len([message for message in messages if "serves this wall's manifest again" in message]) == 1


async def test_a_master_the_server_does_not_hold_is_skipped_and_the_rest_cached(pull, stub, session, http_settings):
    """The feed is cached whole; the work whose master is missing is the programme's to pass over."""
    published = stub.publish("w1", "w2")
    del stub.media[published["works"]["w1"]["media"]["sha256"]]

    assert await pull.cycle(session) is True

    assert _works(http_settings) == ["w1", "w2"]
    assert _held(http_settings) == {_media(published, "w2")}


async def test_a_master_that_keeps_failing_is_said_once_and_backed_off(pull, stub, session, http_settings, caplog):
    stub.publish("w1")
    await pull.cycle(session)
    stub.publish("w2")
    stub.media_status = 503
    heartbeat = http_settings.heartbeat_root / f"display-heartbeat-{WALL_ID}.json"
    heartbeat.write_text(_heartbeat("2026-09-30T12:00:00+00:00"))

    with caplog.at_level(logging.INFO, logger="arrt_player.pull"):
        for _ in range(3):
            assert await pull.cycle(session) is False, "a failing master was not backed off"
        # Checked while the master is still failing: the manifest route answered,
        # so the Player must still be heard.
        assert [json.loads(body)["reported_at"] for body in stub.heartbeats] == ["2026-09-30T12:00:00+00:00"]
        stub.media_status = None
        assert await pull.cycle(session) is True

    messages = [record.getMessage() for record in caplog.records]
    assert len([message for message in messages if "could not be fetched" in message]) == 1
    assert len([message for message in messages if "arrive again" in message]) == 1
    assert _works(http_settings) == ["w2"]


# -- what a restarted Player asks with ---------------------------------------------------


def _truncated(path) -> None:
    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) // 2])


def _not_utf8(path) -> None:
    path.write_bytes(b"\xff\xfe" + path.read_bytes())


def _a_feed_the_watcher_refuses(path) -> None:
    """JSON, so only a reader that runs the watcher's own parser refuses it."""
    document = json.loads(path.read_text())
    document["schedule"]["slots"][0]["work_id"] = "a-work-the-feed-does-not-carry"
    path.write_text(json.dumps(document))


@pytest.mark.parametrize("spoil", [_truncated, _not_utf8, _a_feed_the_watcher_refuses], ids=["truncated", "not-utf8", "refused"])
async def test_a_cached_manifest_the_watcher_cannot_read_is_not_asked_for_with_its_etag(
    pull, stub, session, http_settings, spoil
):
    """Sent beside a manifest the watcher refuses, the ETag gets a 304, and the wall has no feed until the server's changes."""
    published = stub.publish("w1", "w2")
    await pull.cycle(session)
    assert (http_settings.wall_dir / ETAG_FILENAME).is_file(), "the feed was not cached with its ETag"
    spoil(http_settings.manifest_path)
    assert _watcher(http_settings).poll() is None, "the spoiled manifest is still one the watcher reads"

    restarted = Pull(http_settings)
    assert await restarted.cycle(session) is True

    assert _cached(http_settings) == published, "the restarted Player kept the manifest the watcher refuses"
    assert list(_watcher(http_settings).poll().works) == ["w1", "w2"]


def _client_truncated(path) -> None:
    _truncated(path)


def _client_refused(path) -> None:
    """JSON, so only a reader that runs `parse_client_document` refuses it."""
    path.write_text(json.dumps({"client_id": "c-the-pi", "name": "The Pi in the hall", "walls": "not a list"}))


@pytest.fixture
async def client_link(client_settings, stub):
    links = []

    def make() -> ClientPull:
        links.append(ClientPull(replace(client_settings, server_url=stub.url)))
        return links[-1]

    yield make
    for link in links:
        await link.close()


@pytest.mark.parametrize("spoil", [_client_truncated, _client_refused], ids=["truncated", "refused"])
async def test_a_cached_client_document_that_cannot_be_read_is_not_asked_for_with_its_etag(client_link, client_settings, spoil):
    """Sent beside a document the Player refuses, the ETag gets a 304, and no wall starts until a curator changes it."""
    first = await client_link().fetch()
    assert first is not None
    assert (client_settings.cache_dir / CLIENT_ETAG_FILENAME).is_file(), "the document was not cached with its ETag"
    spoil(client_settings.client_document_path)

    restarted = client_link()
    assert restarted.cached() is None, "the spoiled document is still one the Player reads"

    assert await restarted.fetch() == first, "the restarted Player was answered 304 for a document it cannot read"
    assert restarted.cached() == first


# -- a master that keeps failing ------------------------------------------------------------


async def test_a_failing_master_does_not_hold_the_heartbeat_back_to_the_backoff(stub, http_settings):
    """The manifest poll backs off while a master fails; the heartbeat still goes every poll interval."""
    published = stub.publish("w1", "w2")
    stub.media_status_by_sha[published["works"]["w2"]["media"]["sha256"]] = 500
    heartbeat = http_settings.heartbeat_root / f"display-heartbeat-{WALL_ID}.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text(_heartbeat("2026-09-30T12:00:00+00:00"))
    manifest_route = MANIFEST_MAJOR_ROUTE.format(wall_id=WALL_ID, major=2)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    running = asyncio.create_task(Pull(http_settings, interval_seconds=0.05).run(stop))
    try:
        await _until(lambda: len(stub.heartbeats) == 1)
        heartbeat.write_text(_heartbeat("2026-09-30T12:00:01+00:00"))
        changed_at = loop.time()
        await _until(lambda: len(stub.heartbeats) == 2)
        took = loop.time() - changed_at
        polls = stub.requests.count(("GET", manifest_route))
    finally:
        stop.set()
        await asyncio.wait_for(running, timeout=5)

    assert took < BACKOFF_START_SECONDS / 2, f"the changed heartbeat took {took:.2f} s, the manifest's backoff"
    assert polls == 1, "the heartbeat was carried by polling the manifest more often, not beside it"


async def test_the_heartbeat_waits_while_the_server_does_not_answer(stub, http_settings):
    """Posting to a server that just failed the manifest poll would hammer it at the poll's pace."""
    stub.publish("w1")
    stub.manifest_status = 503
    heartbeat = http_settings.heartbeat_root / f"display-heartbeat-{WALL_ID}.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text(_heartbeat("2026-09-30T12:00:00+00:00"))
    stop = asyncio.Event()
    running = asyncio.create_task(Pull(http_settings, interval_seconds=0.02).run(stop))
    try:
        await _until(lambda: len(stub.majors_requested) == 1)
        await asyncio.sleep(0.3)
        posted_while_failing = _heartbeat_posts(stub)
    finally:
        stop.set()
        await asyncio.wait_for(running, timeout=5)

    assert posted_while_failing == 0


async def test_a_heartbeat_loop_that_crashes_ends_the_pull(stub, http_settings):
    """A pull that goes on polling without being heard is a wall the health panel calls silent, for no reason it can see."""
    stub.publish("w1")
    heartbeat = http_settings.heartbeat_root / f"display-heartbeat-{WALL_ID}.json"
    heartbeat.mkdir(parents=True)
    stop = asyncio.Event()
    running = asyncio.create_task(Pull(http_settings, interval_seconds=0.02).run(stop))
    try:
        # Not `wait_for`: cancelling the pull would surface the same error on the way out, ended or not.
        await asyncio.wait({running}, timeout=2)
        ended = running.done()
    finally:
        stop.set()
        await asyncio.wait({running}, timeout=5)

    assert ended, "the pull went on polling after its heartbeat loop crashed"
    assert isinstance(running.exception(), IsADirectoryError)


def _with_a_scene(document: dict, work_id: str) -> dict:
    document["scene"] = {"id": "scene-1", "work_id": work_id, "from": "2026-06-21T11:00:00+00:00", "until": None}
    return document


async def test_a_feed_whose_master_keeps_failing_is_adopted_without_it_after_the_hold(
    http_settings, stub, session, tv, state, clock
):
    """One master that will not download must not freeze the wall's schedule, scene and staging for good."""
    elapsed = _Ticking()
    pull = Pull(http_settings, monotonic=elapsed)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    stub.publish("w0")
    assert await pull.cycle(session) is True
    published = _with_a_scene(
        stub.publish_feed(
            [("w2", "2026-06-21T08:00:00+00:00", "2026-06-21T18:00:00+00:00")],
            staging=("w1",),
        ),
        "w1",
    )
    failing = published["works"]["w2"]["media"]["sha256"]
    stub.media_status_by_sha[failing] = 500
    fetches = ("GET", f"/media/sha256-{failing}")

    assert await pull.cycle(session) is False
    elapsed.now += HOLD_SECONDS - 1
    assert await pull.cycle(session) is False
    assert _works(http_settings) == ["w0"], "the feed was adopted before the hold ran out"

    elapsed.now += 2
    assert await pull.cycle(session) is False, "a master still failing was not backed off"
    assert _cached(http_settings) == published, "the feed was still held past the hold"
    assert _held(http_settings) >= {_media(published, "w1")}
    assert media_name(failing) not in _held(http_settings)
    assert not (http_settings.wall_dir / ETAG_FILENAME).exists(), "an ETag written now would stop the failing master's retries"
    daemon = frame_wall(settings=http_settings, tv=tv, state=state, watcher=_watcher(http_settings), clock=clock.as_clock())
    await tick_until(daemon.tick, lambda: tv.on_the_wall is not None)
    shown = json.loads((http_settings.wall_dir / f"display-heartbeat-{WALL_ID}.json").read_text())
    assert (shown["current_work_id"], shown["scene_id"]) == ("w1", "scene-1"), "the scene did not reach the wall"

    adopted_at = http_settings.manifest_path.stat().st_mtime_ns
    tried = stub.requests.count(fetches)
    elapsed.now += 1
    await pull.cycle(session)
    assert stub.requests.count(fetches) == tried + 1, "the failing master was not asked for again"
    assert http_settings.manifest_path.stat().st_mtime_ns == adopted_at, "the same feed was written again"

    del stub.media_status_by_sha[failing]
    elapsed.now += 1
    assert await pull.cycle(session) is True
    assert media_name(failing) in _held(http_settings)
    assert (http_settings.wall_dir / ETAG_FILENAME).is_file()

    # The hold is the episode's, not the process's: the next master to fail is held its own ten minutes.
    later = stub.publish("w3")
    stub.media_status_by_sha[later["works"]["w3"]["media"]["sha256"]] = 500
    elapsed.now += 1
    await pull.cycle(session)
    assert _cached(http_settings) == published, "a new failure was not held"


async def test_a_304_ends_the_hold_so_the_next_failure_is_held_its_own_ten_minutes(http_settings, stub, session, caplog):
    """The server going back to the feed already cached is the end of the wait for the one it had moved to."""
    caplog.set_level(logging.INFO, logger="arrt_player.pull")
    elapsed = _Ticking()
    pull = Pull(http_settings, monotonic=elapsed)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    first = stub.publish("w0")
    await pull.cycle(session)
    second = stub.publish("w1")
    stub.media_status_by_sha[second["works"]["w1"]["media"]["sha256"]] = 500
    assert await pull.cycle(session) is False

    stub.manifests[WALL_ID] = first
    elapsed.now += HOLD_SECONDS / 2
    assert await pull.cycle(session) is True
    third = stub.publish("w3")
    stub.media_status_by_sha[third["works"]["w3"]["media"]["sha256"]] = 500
    elapsed.now += HOLD_SECONDS / 2 + 1
    await pull.cycle(session)

    assert _works(http_settings) == ["w0"], "a new failure inherited the wait of one the server had already withdrawn"
    events = [record.__dict__.get("event") for record in caplog.records]
    assert events.count("pull.media_failing") == 2, "the second failure was not said"
    assert events.count("pull.media_ok") == 1


async def test_a_server_gone_mid_poll_is_not_asked_for_the_rest_and_a_failing_master_is_asked_for_last(
    pull, stub, session, http_settings
):
    """Asking every master of a server that has gone costs a timeout each; and a failing master must not keep the rest out."""
    published = stub.publish("w-dropped", "w1")
    dropped = published["works"]["w-dropped"]["media"]["sha256"]
    stub.media_dropped.add(dropped)
    w1 = ("GET", f"/media/sha256-{published['works']['w1']['media']['sha256']}")

    assert await pull.cycle(session) is False
    assert w1 not in stub.requests, "a master was asked for after the server dropped the connection"

    assert await pull.cycle(session) is False
    assert _media(published, "w1") in _held(http_settings), "the master that failed kept the next one out"
    assert stub.requests.index(w1) < len(stub.requests) - 1, "the failing master was not asked for after the rest"


async def test_a_master_that_runs_past_its_declared_size_is_stopped_and_skipped(pull, stub, session, http_settings, caplog):
    """Its hash is right; its manifest says it is a thousand bytes. Read whole, it would be cached."""
    big = os.urandom(32 * 1024 * 1024)
    published = stub.publish("w1", "w2", media={"w2": big})
    published["works"]["w2"]["media"]["bytes"] = 1000
    sha = published["works"]["w2"]["media"]["sha256"]

    with caplog.at_level(logging.WARNING, logger="arrt_player.pull"):
        assert await pull.cycle(session) is True

    assert _cached(http_settings) == published
    assert _held(http_settings) == {_media(published, "w1")}, "the oversized master, or a piece of it, was kept"
    (sent,) = stub.media_sent[sha]
    assert sent < len(big), "the Player read the whole master before judging its size"
    (skipped,) = [record for record in caplog.records if record.__dict__.get("event") == "pull.work_skipped"]
    assert "w2" in skipped.getMessage()
    assert "1000" in skipped.getMessage()


@pytest.mark.parametrize("declared", [None, 0, "1000", True], ids=["absent", "zero", "text", "boolean"])
async def test_a_master_with_no_usable_declared_size_is_skipped_without_asking(pull, stub, session, http_settings, declared):
    """The declared size is the bound on the download, so a master without one is not downloaded."""
    published = stub.publish("w1", "w2")
    media = published["works"]["w2"]["media"]
    if declared is None:
        del media["bytes"]
    else:
        media["bytes"] = declared

    assert await pull.cycle(session) is True

    assert _cached(http_settings) == published
    assert _held(http_settings) == {_media(published, "w1")}
    assert ("GET", f"/media/sha256-{media['sha256']}") not in stub.requests


# -- documents read with a cap --------------------------------------------------------------


async def test_a_client_document_larger_than_the_cap_is_refused_and_not_cached(client_link, client_settings, stub):
    """Additive keys are allowed, so only the size makes this one wrong."""
    stub.client_body = json.dumps(
        {"client_id": "c-the-pi", "name": "The Pi in the hall", "walls": [], "padding": "x" * DOCUMENT_CAP_BYTES}
    ).encode()

    assert await client_link().fetch() is None

    assert not client_settings.client_document_path.exists()


async def test_a_label_document_larger_than_the_cap_is_refused(stub, client_settings):
    document = json.loads((CONTRACT / "fixtures" / "label.v1" / "valid" / "showing-art.json").read_text())
    stub.map_label("l-hall", "epd-0", WALL_ID, {**document, "padding": "x" * DOCUMENT_CAP_BYTES})
    link = LabelPull(replace(client_settings, server_url=stub.url), "l-hall")
    try:
        answer = await link.fetch()
        stub.label_documents["l-hall"] = document
        fits = await link.fetch()
    finally:
        await link.close()

    assert answer == LabelAnswer(reachable=True), "a document over the cap was read"
    assert fits.document is not None, "the cap refused a document under it"


# -- the label's id ----------------------------------------------------------------------------


@pytest.mark.parametrize("label_id", ["../x", "..", "a/b", "x?y=1", "x#y", "%2e%2e", ""])
async def test_a_label_id_that_is_not_a_plain_path_component_is_refused_by_name_and_never_requested(
    stub, client_settings, caplog, label_id
):
    link = LabelPull(replace(client_settings, server_url=stub.url), label_id)
    try:
        with caplog.at_level(logging.ERROR, logger="arrt_player.pull"):
            first = await link.fetch()
            second = await link.fetch()
    finally:
        await link.close()

    assert stub.seen == [], "a request went out for a label id that is not a path component"
    assert first == second == LabelAnswer(reachable=True)
    (refused,) = [record for record in caplog.records if record.__dict__.get("event") == "label.id_unusable"]
    assert repr(label_id) in refused.getMessage()


# -- the label's cache ------------------------------------------------------------------------


def _showing_art() -> dict:
    return json.loads((CONTRACT / "fixtures" / "label.v1" / "valid" / "showing-art.json").read_text())


def _label_link(stub, client_settings, clock, label_id: str = "l-hall") -> LabelPull:
    return LabelPull(
        replace(client_settings, server_url=stub.url),
        label_id,
        now=clock.as_clock().now,
        monotonic=clock.as_clock().monotonic,
    )


def _files_under(root) -> set:
    return {path for path in root.rglob("*") if path.is_file()}


async def _cache_one_label(stub, client_settings, clock):
    """Fetch one label document through a link, and return what it answered and the one file it wrote."""
    stub.map_label("l-hall", "epd-0", WALL_ID, _showing_art())
    before = _files_under(client_settings.cache_dir)
    link = _label_link(stub, client_settings, clock)
    try:
        answer = await link.fetch()
    finally:
        await link.close()
    (written,) = _files_under(client_settings.cache_dir) - before
    return answer, written


async def test_an_accepted_label_document_is_cached_and_a_new_link_starts_from_it(stub, client_settings, clock):
    answer, _ = await _cache_one_label(stub, client_settings, clock)
    clock.advance(17.3)

    restarted = _label_link(stub, client_settings, clock)

    assert answer.document is not None
    answered_at = clock.as_clock().now() - timedelta(seconds=17.3)
    assert restarted.cached() == CachedLabel(document=answer.document, answered_at=answered_at)


async def test_the_cached_document_is_stamped_as_answered_once_a_minute_while_the_server_sends_nothing_new(
    stub, client_settings, clock
):
    stub.map_label("l-hall", "epd-0", WALL_ID, _showing_art())
    link = _label_link(stub, client_settings, clock)
    first = clock.as_clock().now()
    try:
        await link.fetch()
        clock.advance(LABEL_STAMP_SECONDS - 22.7)
        not_modified = await link.fetch()
        stamped_early = link.cached()
        clock.advance(23.9)
        await link.fetch()
        stamped_later = link.cached()
    finally:
        await link.close()

    assert not_modified == LabelAnswer(reachable=True), "the second request was not a 304"
    assert stamped_early is not None
    assert stamped_early.answered_at == first, "the stamp was moved more often than once a minute"
    assert stamped_later is not None
    assert stamped_later.answered_at == clock.as_clock().now(), "a 304 a minute on did not stamp the cached document"


async def test_a_document_this_reader_refuses_leaves_the_cached_one(stub, client_settings, clock):
    answer, _ = await _cache_one_label(stub, client_settings, clock)
    stub.label_documents["l-hall"] = {**_showing_art(), "schema": {"major": 2, "minor": 0}}
    link = _label_link(stub, client_settings, clock)
    try:
        refused = await link.fetch()
    finally:
        await link.close()

    assert refused == LabelAnswer(reachable=True)
    kept = _label_link(stub, client_settings, clock).cached()
    assert kept is not None
    assert kept.document == answer.document


async def test_a_document_that_cannot_be_cached_takes_the_last_one_with_it(stub, client_settings, clock, caplog, monkeypatch):
    """Left in place, the last one would be held by a restart as the server's latest, which may say the screen is dark."""
    await _cache_one_label(stub, client_settings, clock)

    def disk_full(path, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(pull_module, "_write_atomically", disk_full)
    dark = {**_showing_art(), "display_state": {"state": "dark", "work_id": None, "since": None}, "label": None}
    stub.label_documents["l-hall"] = dark
    link = _label_link(stub, client_settings, clock)
    try:
        with caplog.at_level(logging.ERROR, logger="arrt_player.pull"):
            answer = await link.fetch()
    finally:
        await link.close()

    assert answer.document is not None
    assert answer.document.state == "dark", "a document that could not be cached was not handed to the renderer"
    assert _label_link(stub, client_settings, clock).cached() is None, "the document the server replaced is still cached"
    assert [record.__dict__.get("event") for record in caplog.records] == ["label.cache_unwritable"]


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(lambda path: path.write_text("{not json", encoding="utf-8"), id="not JSON"),
        pytest.param(
            lambda path: path.write_text(json.dumps({**_showing_art(), "schema": {"major": 2, "minor": 0}}), encoding="utf-8"),
            id="another major",
        ),
        pytest.param(lambda path: path.write_bytes(b"\xff\xfe{"), id="not UTF-8"),
        pytest.param(lambda path: (path.unlink(), path.mkdir()), id="a directory"),
    ],
)
async def test_an_unreadable_cached_document_is_absent_and_the_next_link_asks_for_the_whole_document(
    stub, client_settings, clock, caplog, spoil
):
    _, written = await _cache_one_label(stub, client_settings, clock)
    spoil(written)

    link = _label_link(stub, client_settings, clock)
    try:
        with caplog.at_level(logging.WARNING, logger="arrt_player.pull"):
            cached = link.cached()
        answer = await link.fetch()
    finally:
        await link.close()

    assert cached is None
    # A directory in the file's place cannot be written over either, which the
    # fetch says in a line of its own.
    assert [record.__dict__.get("event") for record in caplog.records].count("label.cache_unreadable") == 1
    assert answer.document is not None, "the link asked with an ETag for a document it does not hold"


async def test_a_label_id_that_is_not_a_plain_component_reads_no_cached_file(stub, client_settings, clock):
    """Put into a path, "../x" would name a file beside the label cache rather than in it."""
    _, written = await _cache_one_label(stub, client_settings, clock)
    planted = written.parent / ".." / "x.json"
    planted.write_bytes(written.read_bytes())

    assert _label_link(stub, client_settings, clock, label_id="x").cached() is None, "the plant is where a plain id caches"
    assert _label_link(stub, client_settings, clock, label_id="../x").cached() is None


# -- the token -------------------------------------------------------------------------------


async def test_the_token_never_reaches_the_journal(pull, stub, session, http_settings, caplog):
    with caplog.at_level(logging.DEBUG):
        stub.publish("w1", "w2")
        bad = stub.manifest["works"]["w2"]["media"]["sha256"]
        stub.media[bad] = b"wrong"
        await pull.cycle(session)
        stub.tokens = {}
        stub.publish("w3")
        await pull.cycle(session)
        await stub.server.close()
        await pull.cycle(session)

    arrt_player_lines = [record.getMessage() for record in caplog.records if record.name.startswith("arrt_player")]
    assert arrt_player_lines, "nothing was logged, so this checks nothing"
    assert all(TOKEN not in line for line in arrt_player_lines)
    assert all(
        TOKEN not in json.dumps(record.__dict__, default=str)
        for record in caplog.records
        if record.name.startswith("arrt_player")
    )
    assert TOKEN not in repr(http_settings)


async def test_the_token_is_not_sent_to_another_host(pull, stub, session, http_settings):
    """A manifest names where to fetch, so it must not be able to send the token there."""
    elsewhere = Stub()
    other = TestServer(elsewhere.app())
    await other.start_server()
    try:
        stub.publish("w1")
        sha = stub.manifest["works"]["w1"]["media"]["sha256"]
        elsewhere.media[sha] = stub.media[sha]
        elsewhere.tokens = {"": WALL_ID}
        stub.manifest["works"]["w1"]["media"]["url"] = str(other.make_url(f"/media/sha256-{sha}"))

        await pull.cycle(session)
    finally:
        await other.close()

    assert elsewhere.authorizations == [None]
    assert _held(http_settings) == {f"sha256-{sha}"}


# -- the heartbeat ---------------------------------------------------------------------------


async def test_the_heartbeat_file_is_posted_when_it_holds_a_new_report_and_not_otherwise(pull, stub, session, http_settings):
    heartbeat = http_settings.heartbeat_root / f"display-heartbeat-{WALL_ID}.json"
    assert heartbeat.parent == http_settings.wall_dir, "the wall's heartbeat file is in its own directory"
    stub.publish("w1")
    heartbeat.write_text(_heartbeat("2026-09-30T12:00:00+00:00"))

    await pull.cycle(session)
    await pull.cycle(session)
    assert [json.loads(body) for body in stub.heartbeats] == [json.loads(_heartbeat("2026-09-30T12:00:00+00:00"))]

    # Rewritten with the same report: not a new heartbeat.
    heartbeat.write_text(json.dumps(json.loads(_heartbeat("2026-09-30T12:00:00+00:00")), indent=2))
    await pull.cycle(session)
    assert len(stub.heartbeats) == 1

    heartbeat.write_text(_heartbeat("2026-09-30T12:01:00+00:00", schema={"major": 1, "minor": 1}))
    await pull.cycle(session)
    assert len(stub.heartbeats) == 2
    assert json.loads(stub.heartbeats[-1])["reported_at"] == "2026-09-30T12:01:00+00:00"


async def test_a_server_that_writes_the_heartbeat_into_the_shared_tree_causes_no_loop(
    pull, stub, session, http_settings, tmp_path
):
    """A host running both: the server keeps each POSTed heartbeat in its ART_ROOT, where its health panel reads it."""
    (tmp_path / "art").mkdir()
    shared = tmp_path / "art" / f"display-heartbeat-{WALL_ID}.json"
    stub.echo_heartbeats_to = shared
    stub.publish("w1")
    (http_settings.heartbeat_root / f"display-heartbeat-{WALL_ID}.json").write_text(_heartbeat("2026-09-30T12:00:00+00:00"))

    for _ in range(5):
        await pull.cycle(session)

    assert len(stub.heartbeats) == 1, "each echo was posted again"
    assert json.loads(shared.read_text())["reported_at"] == "2026-09-30T12:00:00+00:00"


async def test_a_refused_heartbeat_is_posted_again_only_when_it_changes(pull, stub, session, http_settings, caplog):
    """A 4xx is the server's word on this report: posting it again each second changes nothing."""
    stub.publish("w1")
    heartbeat = http_settings.heartbeat_root / f"display-heartbeat-{WALL_ID}.json"
    heartbeat.write_text(_heartbeat("2026-09-30T12:00:00+00:00"))
    stub.heartbeat_status = 400

    with caplog.at_level(logging.WARNING, logger="arrt_player.pull"):
        await pull.cycle(session)
        await pull.cycle(session)
        assert _heartbeat_posts(stub) == 1, "an unchanged, refused heartbeat was posted again"

        heartbeat.write_text(_heartbeat("2026-09-30T12:00:01+00:00"))
        await pull.cycle(session)
    assert _heartbeat_posts(stub) == 2, "a changed heartbeat was not posted after a refusal"
    (refused,) = [record for record in caplog.records if record.__dict__.get("event") == "pull.heartbeat_refused"]
    assert refused.status == 400


async def test_a_heartbeat_the_server_failed_is_posted_again_on_the_next_poll(pull, stub, session, http_settings):
    """A 5xx is the server's trouble, not the report's: the same report goes again."""
    stub.publish("w1")
    heartbeat = http_settings.heartbeat_root / f"display-heartbeat-{WALL_ID}.json"
    heartbeat.write_text(_heartbeat("2026-09-30T12:00:00+00:00"))
    stub.heartbeat_status = 503

    await pull.cycle(session)
    await pull.cycle(session)
    assert _heartbeat_posts(stub) == 2

    stub.heartbeat_status = None
    await pull.cycle(session)
    await pull.cycle(session)
    assert [json.loads(body)["reported_at"] for body in stub.heartbeats] == ["2026-09-30T12:00:00+00:00"]


def _heartbeat_fixtures(kind: str) -> list:
    folder = CONTRACT / "fixtures" / "heartbeat.v1" / kind
    return [pytest.param(path, id=path.stem) for path in sorted(folder.glob("*.json"))]


@pytest.mark.parametrize("fixture", _heartbeat_fixtures("invalid"))
async def test_the_double_refuses_a_wall_heartbeat_the_contract_refuses(stub, session, fixture):
    """So a Player that writes a bad heartbeat fails against the double as it would against Arrt."""
    async with session.post(
        stub.url + HEARTBEAT_ROUTE.format(wall_id=WALL_ID),
        data=fixture.read_bytes(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    ) as response:
        assert response.status == 400
    assert stub.heartbeats == []


@pytest.mark.parametrize("fixture", _heartbeat_fixtures("valid"))
async def test_the_double_takes_a_wall_heartbeat_the_contract_takes(stub, session, fixture):
    async with session.post(
        stub.url + HEARTBEAT_ROUTE.format(wall_id=WALL_ID),
        data=fixture.read_bytes(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    ) as response:
        assert response.status == 204
    assert stub.heartbeats == [fixture.read_bytes()]


async def test_the_pull_stops_when_asked(stub, http_settings):
    stub.publish("w1")
    stop = asyncio.Event()
    running = asyncio.create_task(Pull(http_settings, interval_seconds=0.01).run(stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(running, timeout=5)

    assert _cached(http_settings) is not None


async def test_the_daemon_writes_its_heartbeat_into_the_walls_directory_where_the_pull_looks(
    pull, stub, session, http_settings, tv, state, clock
):
    stub.publish("w1")
    await pull.cycle(session)
    daemon = frame_wall(settings=http_settings, tv=tv, state=state, watcher=_watcher(http_settings), clock=clock.as_clock())

    await tick_until(daemon.tick, lambda: tv.on_the_wall is not None)
    await pull.cycle(session)

    assert (http_settings.wall_dir / f"display-heartbeat-{WALL_ID}.json").is_file()
    assert [heartbeat["current_work_id"] for heartbeat in stub.heartbeats_by_wall[WALL_ID]] == ["w1"]


# -- asking for a major -----------------------------------------------------------------


@pytest.fixture
def next_major(monkeypatch):
    """This Player as one reading a major 3 beside major 2, which is how the next major is served.

    The walk down the majors is in the pull for that day, and nothing in a Player
    reading one major reaches it, so these tests give it a second major to ask
    for. The server double serves major 3 by serving its feed at `v3`.
    """
    monkeypatch.setattr(pull_module, "REQUESTED_MAJORS", (3, 2))


async def test_this_player_asks_for_the_majors_it_reports_and_no_other(pull, stub, session):
    stub.publish("w1")

    await pull.cycle(session)

    assert stub.majors_requested == list(REQUESTED_MAJORS)


def test_this_player_asks_only_for_major_2():
    """It composes every work itself, so it reads no major carrying a server's composed render."""
    assert REQUESTED_MAJORS == (2,)


async def test_a_major_the_server_does_not_publish_falls_back_to_the_next_one_down(next_major, http_settings, stub, session):
    pull = Pull(http_settings)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    stub.publish("w1")

    assert await pull.cycle(session) is True

    assert stub.majors_requested == [3, 2], "the highest major was not asked for first"
    assert _works(http_settings) == ["w1"]


async def test_the_highest_major_published_is_the_one_taken(next_major, http_settings, stub, session):
    pull = Pull(http_settings)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    stub.publish("f1")
    stub.served_at[WALL_ID] = 3

    await pull.cycle(session)

    assert stub.majors_requested == [3]
    assert _works(http_settings) == ["f1"]


async def test_every_major_answering_404_is_nothing_published(pull, stub, session, caplog):
    """The wall misconfigured, said once: no major this Player reads is published for it."""
    with caplog.at_level(logging.ERROR, logger="arrt_player.pull"):
        assert await pull.cycle(session) is True

    assert stub.majors_requested == [2]
    (refused,) = [record for record in caplog.records if record.getMessage().startswith("the server refused")]
    assert refused.status == 404


async def test_every_major_is_asked_before_a_404_is_nothing_published(next_major, http_settings, stub, session):
    pull = Pull(http_settings)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)

    assert await pull.cycle(session) is True

    assert stub.majors_requested == [3, 2]


async def test_a_refused_token_stops_at_the_first_major(next_major, http_settings, stub, session):
    """A token refused at one major is refused at every one; asking the next would only say so again."""
    pull = Pull(http_settings)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    stub.publish("w1")
    stub.tokens.clear()

    await pull.cycle(session)

    assert stub.majors_requested == [3]
    assert _cached(http_settings) is None


class _Ticking:
    """Elapsed time that moves only when the test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_wall_served_a_lower_major_asks_for_the_higher_one_once_a_minute(next_major, http_settings, stub, session):
    """Until the server publishes the higher major, asking for it every poll doubles the requests and logs a 404 a second."""
    clock = _Ticking()
    pull = Pull(http_settings, monotonic=clock)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    stub.publish("w1")

    await pull.cycle(session)
    clock.now += 1.3
    await pull.cycle(session)
    clock.now += HIGHER_MAJOR_SECONDS - 1.3 - 0.7
    await pull.cycle(session)
    assert stub.majors_requested == [3, 2, 2, 2], "the higher major was asked for again before a minute had passed"

    clock.now += 1.4
    await pull.cycle(session)
    assert stub.majors_requested[4:] == [3, 2]


async def test_a_major_that_stops_answering_sends_the_wall_back_to_the_highest(next_major, http_settings, stub, session):
    """Served major 2, and the server now publishes only major 3: found on the next poll, not a minute later."""
    clock = _Ticking()
    pull = Pull(http_settings, monotonic=clock)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    stub.publish("w1")
    await pull.cycle(session)

    stub.publish("f1")
    stub.served_at[WALL_ID] = 3
    clock.now += 1.3
    await pull.cycle(session)

    assert stub.majors_requested == [3, 2, 2, 3]
    assert _works(http_settings) == ["f1"]


async def test_a_wall_served_the_highest_major_asks_only_for_it(next_major, http_settings, stub, session):
    clock = _Ticking()
    pull = Pull(http_settings, monotonic=clock)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    stub.publish("f1")
    stub.served_at[WALL_ID] = 3

    for _ in range(3):
        await pull.cycle(session)
        clock.now += 1.3

    assert stub.majors_requested == [3, 3, 3]


async def test_a_wall_the_server_stops_publishing_for_forgets_which_major_answered(next_major, http_settings, stub, session):
    """Nothing answering is nothing to start from: the next poll asks from the top once, not the old major first."""
    clock = _Ticking()
    pull = Pull(http_settings, monotonic=clock)
    (http_settings.wall_dir / MEDIA_DIRNAME).mkdir(parents=True)
    stub.publish("w1")
    await pull.cycle(session)

    stub.manifest = None
    clock.now += 1.3
    await pull.cycle(session)
    clock.now += 1.3
    await pull.cycle(session)

    assert stub.majors_requested == [3, 2, 2, 3, 2, 3, 2]


async def test_a_feed_pulled_from_the_server_reaches_a_screen_composed(pull, stub, session, http_settings, clock):
    """End to end: v2 asked for and served, its masters cached, each composed for the screen, and the slot's work drawn."""
    stub.publish_feed([("f1", "2026-06-21T08:00:00+00:00", "2026-06-21T13:00:00+00:00")])
    assert await pull.cycle(session) is True
    output = RecordingOutput(screen=(1280, 720))
    wall = screen_wall(wall=http_settings, output=output, watcher=_watcher(http_settings), clock=clock.as_clock())

    await tick_until(wall.tick, lambda: len(output.shown) == 1)

    (shown,) = output.shown
    assert shown.parent == http_settings.composed_root, "the master itself was drawn, not its composition"
    with Image.open(shown) as picture:
        assert picture.size == (1280, 720)
    assert stub.majors_requested == [2]
