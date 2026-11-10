"""The slideshow's media cache: what it fetches, in what order, what it keeps and where it stops.

A fake downloader stands in for `fetch.Fetcher`. It writes files the way the real
one does (the item id plus the extension its bytes chose) and can be told to
fail an item each way the real one fails.
"""

from pathlib import Path

import aiohttp
import pytest

from arrt_player.sources import Caption, Item, Kind
from arrt_player.sources.cache import (
    RETRY_AFTER_S,
    UNREACHABLE_RUN,
    MediaCache,
    evictions,
    play_order,
)
from arrt_player.sources.fetch import MAX_FILE_BYTES, PARTIAL_SUFFIX, AddressRefused, Download, NotMedia, TooLarge

CAPTION = Caption(title="t", community="r/pics", author="u/someone", score=1)


def item(item_id: str, url: str | None = None, kind: Kind = Kind.STILL) -> Item:
    return Item(id=item_id, media_url=url or f"https://i.redd.it/{item_id}.jpg", kind=kind, caption=CAPTION, tier="reddit")


class FakeDownloader:
    def __init__(self, sizes: dict[str, int] | None = None, failures: dict[str, Exception] | None = None) -> None:
        self.sizes = sizes or {}
        self.failures = failures or {}
        self.asked: list[tuple[str, int]] = []

    async def download(self, url: str, destination: Path, max_bytes: int = MAX_FILE_BYTES) -> Download:
        item_id = destination.name
        self.asked.append((item_id, max_bytes))
        if item_id in self.failures:
            raise self.failures[item_id]
        size = self.sizes.get(item_id, 100)
        if size > max_bytes:
            raise TooLarge(f"more than {max_bytes}")
        extension = "mp4" if url.endswith(".mp4") else "jpg"
        path = destination.with_name(f"{item_id}.{extension}")
        path.write_bytes(b"\x00" * size)
        return Download(path=path, kind=Kind.CLIP if extension == "mp4" else Kind.STILL, extension=extension, size=size)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_play_order_starts_at_the_position_and_wraps():
    items = [item(f"t3_{n}") for n in "abcde"]

    assert [i.id for i in play_order(items, 3)] == ["t3_d", "t3_e", "t3_a", "t3_b", "t3_c"]
    assert [i.id for i in play_order(items, 8)] == ["t3_d", "t3_e", "t3_a", "t3_b", "t3_c"]
    assert list(play_order([], 2)) == []


def test_evictions_are_what_is_not_kept():
    on_disk = {"a": Path("/c/a.jpg"), "b": Path("/c/b.mp4"), "c": Path("/c/c.jpg")}

    assert evictions(on_disk, {"b"}) == [Path("/c/a.jpg"), Path("/c/c.jpg")]


async def test_a_fill_downloads_in_play_order_from_the_position(tmp_path):
    downloader = FakeDownloader()
    cache = MediaCache(tmp_path, downloader)
    items = [item("t3_a"), item("t3_b", "https://v.redd.it/b.mp4", Kind.CLIP), item("t3_c")]

    report = await cache.fill(items, start=1)

    assert [asked for asked, _ in downloader.asked] == ["t3_b", "t3_c", "t3_a"]
    assert report.downloaded == ["t3_b", "t3_c", "t3_a"]
    clip = cache.get("t3_b")
    assert clip is not None
    assert clip.kind is Kind.CLIP
    assert cache.get("t3_a").path == tmp_path / "t3_a.jpg"


async def test_what_is_here_is_not_fetched_again_even_by_a_new_cache(tmp_path):
    items = [item("t3_a"), item("t3_b")]
    await MediaCache(tmp_path, FakeDownloader()).fill(items)

    downloader = FakeDownloader()
    reopened = MediaCache(tmp_path, downloader)
    await reopened.fill(items)

    assert downloader.asked == []
    assert reopened.get("t3_a") is not None


def test_a_partial_download_left_by_a_dead_process_is_cleared(tmp_path):
    (tmp_path / f"t3_a{PARTIAL_SUFFIX}").write_bytes(b"half")
    (tmp_path / "t3_b.jpg").write_bytes(b"whole")

    cache = MediaCache(tmp_path, FakeDownloader())

    assert sorted(p.name for p in tmp_path.iterdir()) == ["t3_b.jpg"]
    assert cache.get("t3_a") is None
    assert cache.get("t3_b") is not None


async def test_eviction_follows_a_listing_that_changes_between_fills(tmp_path):
    """Each refresh keeps a different set, so this cannot pass by deleting nothing or everything."""
    cache = MediaCache(tmp_path, FakeDownloader())
    first = [item("t3_a"), item("t3_b"), item("t3_c")]
    await cache.fill(first)

    second = [item("t3_b"), item("t3_d")]
    removed = cache.evict({i.id for i in second})
    await cache.fill(second)
    assert sorted(p.name for p in removed) == ["t3_a.jpg", "t3_c.jpg"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t3_b.jpg", "t3_d.jpg"]

    third = [item("t3_d"), item("t3_a")]
    cache.evict({i.id for i in third})
    await cache.fill(third)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t3_a.jpg", "t3_d.jpg"]
    assert cache.get("t3_b") is None


def test_eviction_leaves_a_file_the_cache_did_not_write(tmp_path):
    (tmp_path / "notes.txt").write_text("the operator's")
    (tmp_path / "t3_a.jpg").write_bytes(b"x")

    MediaCache(tmp_path, FakeDownloader()).evict(set())

    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


async def test_the_cap_stops_downloads_and_counts_bytes_on_disk(tmp_path):
    """A file already here, and one the cache did not write, count against the cap."""
    (tmp_path / "t3_old.jpg").write_bytes(b"\x00" * 300)
    (tmp_path / "stray.bin").write_bytes(b"\x00" * 100)
    downloader = FakeDownloader(sizes={"t3_a": 400, "t3_b": 150, "t3_c": 100})
    cache = MediaCache(tmp_path, downloader, cap_bytes=1000)

    report = await cache.fill([item("t3_old"), item("t3_a"), item("t3_b"), item("t3_c")])

    assert report.downloaded == ["t3_a", "t3_b"]
    assert report.cap_reached
    assert cache.used_bytes() == 950
    assert cache.get("t3_c") is None
    assert downloader.asked == [("t3_a", 600), ("t3_b", 200), ("t3_c", 50)]


async def test_a_full_cache_asks_for_nothing(tmp_path):
    (tmp_path / "t3_old.jpg").write_bytes(b"\x00" * 1000)
    downloader = FakeDownloader()
    cache = MediaCache(tmp_path, downloader, cap_bytes=1000)

    report = await cache.fill([item("t3_a")])

    assert report.cap_reached
    assert downloader.asked == []


async def test_a_file_too_large_for_any_room_is_skipped_and_the_fill_goes_on(tmp_path):
    downloader = FakeDownloader(sizes={"t3_huge": 5000})
    cache = MediaCache(tmp_path, downloader, cap_bytes=100_000, max_file_bytes=1000)

    report = await cache.fill([item("t3_huge"), item("t3_b")])

    assert list(report.failed) == ["t3_huge"]
    assert report.downloaded == ["t3_b"]
    assert not report.cap_reached


async def test_one_refused_item_does_not_stop_the_rest_and_waits_before_a_retry(tmp_path):
    clock = Clock()
    downloader = FakeDownloader(
        failures={"t3_lan": AddressRefused("10.0.0.1 is not a public address"), "t3_page": NotMedia("text/html")}
    )
    cache = MediaCache(tmp_path, downloader, clock=clock)
    items = [item("t3_lan"), item("t3_page"), item("t3_ok")]

    report = await cache.fill(items)
    assert sorted(report.failed) == ["t3_lan", "t3_page"]
    assert report.downloaded == ["t3_ok"]

    downloader.asked.clear()
    clock.now += RETRY_AFTER_S - 7
    await cache.fill(items)
    assert downloader.asked == []

    clock.now += 11
    await cache.fill(items)
    assert [asked for asked, _ in downloader.asked] == ["t3_lan", "t3_page"]


async def test_a_run_of_unreachable_items_stops_the_fill_and_holds_none_of_them_back(tmp_path):
    """The network is down, not those links: the next fill tries them again at once."""
    names = [f"t3_{n}" for n in "abcdef"]
    down = {name: aiohttp.ClientConnectionError("no route") for name in names}
    downloader = FakeDownloader(failures=down)
    cache = MediaCache(tmp_path, downloader, clock=Clock())

    report = await cache.fill([item(name) for name in names])
    assert report.network_down
    assert [asked for asked, _ in downloader.asked] == names[:UNREACHABLE_RUN]

    downloader.failures.clear()
    downloader.asked.clear()
    report = await cache.fill([item(name) for name in names])
    assert report.downloaded == names


@pytest.mark.parametrize(
    ("between", "downloaded"),
    [
        ({}, ["t3_c", "t3_f"]),
        ({"t3_c": NotMedia("text/html")}, ["t3_f"]),
    ],
)
async def test_unreachable_items_broken_up_by_an_answer_are_broken_links(tmp_path, between, downloaded):
    """More unreachable items than the run in all, but never that many in a row: an answer breaks the run."""
    timeouts = {name: TimeoutError() for name in ("t3_a", "t3_b", "t3_d", "t3_e")}
    downloader = FakeDownloader(failures=timeouts | between)
    cache = MediaCache(tmp_path, downloader, clock=Clock())

    report = await cache.fill([item(f"t3_{n}") for n in "abcdef"])

    assert not report.network_down
    assert report.downloaded == downloaded


async def test_eviction_forgets_a_failure_for_an_item_no_longer_listed(tmp_path):
    """So a post that comes back to the listing later is tried at once, not after the wait."""
    clock = Clock()
    downloader = FakeDownloader(failures={"t3_a": NotMedia("x")})
    cache = MediaCache(tmp_path, downloader, clock=clock)
    await cache.fill([item("t3_a")])

    cache.evict(set())
    downloader.failures.clear()
    await cache.fill([item("t3_a")])

    assert cache.get("t3_a") is not None


def test_get_does_not_answer_for_a_file_removed_underneath_it(tmp_path):
    (tmp_path / "t3_a.jpg").write_bytes(b"x")
    cache = MediaCache(tmp_path, FakeDownloader())

    (tmp_path / "t3_a.jpg").unlink()

    assert cache.get("t3_a") is None
