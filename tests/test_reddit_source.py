"""Reddit as a slideshow source: each post shape to its items, and each answer to its outcome.

The shapes come from `tests/fixtures/reddit/shapes.json`, real posts trimmed for a
public repository (its README says how). Where a shape was not found in the
probe, a case is derived from a real post and says so.
"""

import copy
import json
import logging
from pathlib import Path

import pytest
from conftest import events, journal

from arrt_player.sources import Kind, Outcome
from arrt_player.sources.fetch import ApiAnswer, FetchRefused
from arrt_player.sources.reddit import ME_URL, RedditSource, items_of, listing_url, parse_listing

SHAPES = json.loads((Path(__file__).parent / "fixtures" / "reddit" / "shapes.json").read_text())
LISTING_URL = "https://www.reddit.com/r/pics/top/?t=week"


def post(title: str) -> dict:
    """The fixture post whose (replaced) title names its shape."""
    return copy.deepcopy(next(child["data"] for child in SHAPES["data"]["children"] if child["data"]["title"] == f"A {title}"))


def only(found: list) -> tuple[str, Kind, str]:
    assert len(found) == 1, found
    return found[0].media_url, found[0].kind, found[0].tier


# -- each shape to its items ---------------------------------------------------


def test_a_still_on_reddit_is_shown_as_reddit_hosts_it():
    url, kind, tier = only(items_of(post("still on i.redd.it")))

    assert (kind, tier) == (Kind.STILL, "reddit")
    assert url.startswith("https://i.redd.it/")


def test_a_gallery_is_one_item_per_picture_in_the_galleries_own_order():
    gallery = post("gallery in an order of its own")
    order = [entry["media_id"] for entry in gallery["gallery_data"]["items"]]
    assert order != list(gallery["media_metadata"]), "the fixture no longer tells the two orders apart"

    found = items_of(gallery)

    assert [item.id for item in found] == [f"{gallery['name']}-{media_id}" for media_id in order]
    assert all(item.kind is Kind.STILL and item.media_url.startswith("https://preview.redd.it/") for item in found)
    assert {item.caption for item in found} == {found[0].caption}


def test_a_gif_plays_as_its_mp4_rendition_not_the_gif():
    gif = post("gif on i.redd.it")
    assert gif["url"].endswith(".gif")

    url, kind, tier = only(items_of(gif))

    assert (kind, tier) == (Kind.CLIP, "reddit")
    assert "format=mp4" in url


def test_a_video_plays_from_its_fallback_url():
    video = post("video on v.redd.it")

    url, kind, _ = only(items_of(video))

    assert kind is Kind.CLIP
    assert url == video["secure_media"]["reddit_video"]["fallback_url"]


@pytest.mark.parametrize(("shape", "count"), [("crosspost of a video", 1), ("crosspost of a gallery", 2)])
def test_a_crosspost_shows_the_originals_media_under_its_own_caption(shape, count):
    crosspost = post(shape)
    assert not crosspost.get("secure_media"), "the fixture's crosspost carries media itself"
    assert not crosspost.get("is_gallery"), "the fixture's crosspost carries media itself"

    found = items_of(crosspost)

    assert len(found) == count
    assert {item.caption.title for item in found} == {f"A {shape}"}
    assert all(item.id.startswith(crosspost["name"]) for item in found)


def test_a_gifv_with_a_moving_copy_plays_it():
    url, kind, tier = only(items_of(post("gifv on imgur with a copy")))

    assert (kind, tier) == (Kind.CLIP, "reddit-copy")
    assert "format=mp4" in url


def test_redgifs_plays_reddits_moving_copy():
    url, kind, tier = only(items_of(post("redgifs clip")))

    assert (kind, tier) == (Kind.CLIP, "reddit-copy")
    assert url.startswith("https://v.redd.it/")


@pytest.mark.parametrize("shape", ["page on imgur", "picture on flickr"])
def test_a_linked_picture_is_reddits_copy_of_it(shape):
    url, kind, tier = only(items_of(post(shape)))

    assert (kind, tier) == (Kind.STILL, "reddit-copy")
    assert url.startswith("https://external-preview.redd.it/")


@pytest.mark.parametrize("shape", ["gifv on imgur without a copy", "youtube video"])
def test_something_that_moves_is_never_shown_as_a_still_of_itself(shape):
    """Both carry Reddit's still copy, so a parser that fell through to it would show one."""
    moving = post(shape)
    assert moving["preview"]["images"][0]["source"]["url"]

    assert items_of(moving) == []


@pytest.mark.parametrize("shape", ["gfycat link", "text post"])
def test_a_post_with_nothing_to_show_gives_no_item(shape):
    assert items_of(post(shape)) == []


def test_a_media_url_with_no_copy_on_reddit_is_fetched_from_its_host():
    """Derived: no real post in the probe lacked Reddit's copy, so this is the flickr post without it."""
    direct = post("picture on flickr")
    del direct["preview"]

    url, kind, tier = only(items_of(direct))

    assert (url, kind, tier) == (direct["url"], Kind.STILL, "direct")


def _animated(entry: dict) -> dict:
    """Derived: the probe found no animated gallery entry, so this gives the real gallery one in Reddit's documented shape."""
    gallery = post("gallery in an order of its own")
    first = gallery["gallery_data"]["items"][0]["media_id"]
    gallery["media_metadata"][first] = {"status": "valid", "e": "AnimatedImage", "m": "image/gif", "id": first, "s": entry}
    return gallery


def test_an_animated_gallery_entry_plays_its_mp4_before_its_gif():
    found = items_of(
        _animated({"x": 1, "y": 1, "gif": "https://i.redd.it/a.gif", "mp4": "https://preview.redd.it/a.gif?format=mp4"})
    )

    assert (found[0].media_url, found[0].kind) == ("https://preview.redd.it/a.gif?format=mp4", Kind.CLIP)
    assert [item.kind for item in found[1:]] == [Kind.STILL, Kind.STILL]


def test_an_animated_gallery_entry_with_only_its_gif_plays_the_gif():
    found = items_of(_animated({"x": 1, "y": 1, "gif": "https://i.redd.it/a.gif"}))

    assert (found[0].media_url, found[0].kind) == ("https://i.redd.it/a.gif", Kind.CLIP)


def test_a_gallery_entry_that_is_not_valid_is_left_out_and_a_gallery_of_none_is_unresolved():
    gallery = post("gallery in an order of its own")
    first = gallery["gallery_data"]["items"][0]["media_id"]
    gallery["media_metadata"][first]["status"] = "failed"
    assert len(items_of(gallery)) == 2

    for entry in gallery["media_metadata"].values():
        entry["status"] = "unprocessed"
    _, unresolved = parse_listing({"data": {"children": [{"kind": "t3", "data": gallery}]}})
    assert unresolved == {"reddit.com": 1}


# -- the listing as a whole ----------------------------------------------------


def test_the_listing_keeps_reddits_order_and_counts_what_it_cannot_show_by_domain():
    items, unresolved = parse_listing(SHAPES)

    posts = [child["data"]["name"] for child in SHAPES["data"]["children"]]
    item_posts = [item.id.split("-")[0] for item in items]
    assert item_posts == sorted(item_posts, key=posts.index)
    assert unresolved == {"i.imgur.com": 1, "gfycat.com": 1, "youtube.com": 1, "self.EarthPorn": 1}


def _unsafe_media_id(gallery: dict) -> None:
    """A media id that would not be a safe file name: `Item` refuses it."""
    first = gallery["gallery_data"]["items"][0]
    gallery["media_metadata"]["../a"] = gallery["media_metadata"][first["media_id"]]
    first["media_id"] = "../a"


@pytest.mark.parametrize(
    "breakage",
    [
        lambda gallery: gallery.update(gallery_data={"items": [{"no_media_id": True}]}),
        lambda gallery: gallery.update(gallery_data={"items": "not a list of entries"}),
        _unsafe_media_id,
    ],
)
def test_a_post_in_a_shape_not_known_costs_only_that_post(caplog, breakage):
    broken = copy.deepcopy(SHAPES)
    breakage(broken["data"]["children"][1]["data"])

    items, unresolved = parse_listing(broken)

    assert len(items) == len(parse_listing(SHAPES)[0]) - 3
    assert unresolved["reddit.com"] == 1
    assert [record.post for record in events(caplog, "slideshow.reddit.post_unreadable")] == [
        broken["data"]["children"][1]["data"]["name"]
    ]


def test_a_field_sent_as_the_wrong_type_costs_only_that_post(caplog):
    """One post with a string where an object goes must not end the listing, or every restart reads it and stops again."""
    broken = copy.deepcopy(SHAPES)
    # A hosted video, read from its media.
    post = broken["data"]["children"][3]["data"]
    post["secure_media"] = "a string where an object goes"

    items, _ = parse_listing(broken)

    assert [i.id for i in items] == [i.id for i in parse_listing(SHAPES)[0] if i.id != post["name"]]
    assert [record.post for record in events(caplog, "slideshow.reddit.post_unreadable")] == [post["name"]]


def test_a_child_that_is_not_an_object_is_passed_over():
    broken = copy.deepcopy(SHAPES)
    broken["data"]["children"][1:1] = ["a string", None, {"kind": "t3", "data": "not an object"}]

    items, _ = parse_listing(broken)

    assert [i.id for i in items] == [i.id for i in parse_listing(SHAPES)[0]]


@pytest.mark.parametrize("document", [[], {"data": {}}, {"kind": "t2", "data": {"name": "someone"}}, "Blocked"])
def test_what_is_not_a_listing_is_refused(document):
    with pytest.raises(ValueError, match="not a listing"):
        parse_listing(document)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("https://www.reddit.com/r/pics/top/?t=week", "https://www.reddit.com/r/pics/top.json?t=week&limit=100&raw_json=1"),
        ("https://reddit.com/r/EarthPorn", "https://www.reddit.com/r/EarthPorn.json?limit=100&raw_json=1"),
        (
            "https://old.reddit.com/r/A+B/top.json?t=all&limit=5",
            "https://www.reddit.com/r/A+B/top.json?t=all&limit=100&raw_json=1",
        ),
        ("  http://new.reddit.com/user/x/m/walls/  ", "https://www.reddit.com/user/x/m/walls.json?limit=100&raw_json=1"),
    ],
)
def test_a_listing_url_as_copied_from_a_browser_becomes_its_json(written, expected):
    assert listing_url(written) == expected


@pytest.mark.parametrize(
    "written",
    [
        "https://www.reddit.com/",
        "https://www.reddit.com/r",
        "https://evil.example/r/pics",
        "https://www.reddit.com.evil.example/r/pics",
        "ftp://www.reddit.com/r/pics",
        "r/pics",
    ],
)
def test_what_is_not_a_reddit_listing_is_refused(written):
    with pytest.raises(ValueError, match=r"reddit\.com address|names no listing"):
        listing_url(written)


# -- each answer to its outcome -------------------------------------------------


ACCOUNT = json.dumps({"kind": "t2", "data": {"name": "the-owners-account-name", "total_karma": 12345}}).encode()
BLOCKED = b"<!doctype html><html><head><title>Blocked</title></head></html>"


class FakeClient:
    def __init__(self, answers: dict[str, ApiAnswer | Exception]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def get_api(self, url: str) -> ApiAnswer:
        self.asked.append(url)
        key = "me" if url == ME_URL else "listing"
        answer = self.answers[key]
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _outcome(answers: dict) -> tuple:
    client = FakeClient(answers)
    listing = await RedditSource(LISTING_URL, client).listing()
    return listing, client


async def test_a_listing_is_listed():
    listing, client = await _outcome({"listing": ApiAnswer(200, json.dumps(SHAPES).encode())})

    assert listing.outcome is Outcome.LISTED
    assert len(listing.items) == len(parse_listing(SHAPES)[0])
    assert listing.unresolved["gfycat.com"] == 1
    assert client.asked == [listing_url(LISTING_URL)]


async def test_a_403_with_an_accepted_session_is_a_refused_listing_never_a_cookie():
    listing, client = await _outcome({"listing": ApiAnswer(403, BLOCKED), "me": ApiAnswer(200, ACCOUNT)})

    assert listing.outcome is Outcome.REFUSED
    assert "cookie" not in listing.detail
    assert client.asked == [listing_url(LISTING_URL), ME_URL]


@pytest.mark.parametrize(
    "me",
    [ApiAnswer(403, BLOCKED), ApiAnswer(200, b"{}"), ApiAnswer(200, b"<html>"), ApiAnswer(403, ACCOUNT)],
)
async def test_a_403_with_a_session_reddit_does_not_accept_is_the_cookie(me):
    listing, _ = await _outcome({"listing": ApiAnswer(403, BLOCKED), "me": me})

    assert listing.outcome is Outcome.COOKIE_REJECTED


async def test_a_403_then_a_rate_limit_on_the_session_is_a_rate_limit_never_the_cookie():
    listing, _ = await _outcome({"listing": ApiAnswer(403, BLOCKED), "me": ApiAnswer(429, b"", ratelimit_reset_s=93.0)})

    assert (listing.outcome, listing.retry_after_s) == (Outcome.RATE_LIMITED, 93.0)
    assert "cookie" not in listing.detail


@pytest.mark.parametrize("status", [500, 503])
async def test_a_403_then_an_outage_on_the_session_is_unreachable_never_the_cookie(status):
    listing, _ = await _outcome({"listing": ApiAnswer(403, BLOCKED), "me": ApiAnswer(status, b"")})

    assert listing.outcome is Outcome.UNREACHABLE
    assert "cookie" not in listing.detail


async def test_a_429_is_rate_limited_for_as_long_as_reddit_says():
    listing, _ = await _outcome({"listing": ApiAnswer(429, b"", ratelimit_reset_s=187.0)})

    assert (listing.outcome, listing.retry_after_s) == (Outcome.RATE_LIMITED, 187.0)


@pytest.mark.parametrize(
    "answers",
    [
        {"listing": ApiAnswer(500, b"")},
        {"listing": ApiAnswer(302, b"", location="https://www.reddit.com/elsewhere")},
        {"listing": ApiAnswer(200, BLOCKED)},
        {"listing": ConnectionResetError()},
        {"listing": TimeoutError()},
        {"listing": FetchRefused("too large")},
        {"listing": ApiAnswer(403, BLOCKED), "me": TimeoutError()},
    ],
)
async def test_anything_else_is_unreachable(answers):
    listing, _ = await _outcome(answers)

    assert listing.outcome is Outcome.UNREACHABLE
    assert listing.items == ()


@pytest.mark.parametrize(
    ("answers", "outcome"),
    [
        ({"listing": ApiAnswer(403, BLOCKED), "me": ApiAnswer(200, ACCOUNT)}, "refused"),
        ({"listing": ApiAnswer(403, BLOCKED), "me": ApiAnswer(403, BLOCKED)}, "cookie_rejected"),
        ({"listing": ApiAnswer(429, b"", ratelimit_reset_s=5.0)}, "rate_limited"),
        ({"listing": TimeoutError()}, "unreachable"),
    ],
)
async def test_a_listing_not_listed_is_one_event_that_names_its_outcome(answers, outcome, caplog):
    """One name for every way a listing fails, so a journal query for it does not
    have to know each outcome's spelling; the outcome rides as a field."""
    await _outcome(answers)

    assert [record.outcome for record in events(caplog, "slideshow.reddit.not_listed")] == [outcome]
    assert events(caplog, "slideshow.reddit.listed") == []


async def test_a_listing_listed_says_so_and_never_not_listed(caplog):
    caplog.set_level(logging.INFO)
    await _outcome({"listing": ApiAnswer(200, json.dumps(SHAPES).encode())})

    (listed,) = events(caplog, "slideshow.reddit.listed")
    assert listed.items == len(parse_listing(SHAPES)[0])
    assert listed.unresolved["gfycat.com"] == 1
    assert events(caplog, "slideshow.reddit.not_listed") == []


async def test_the_accounts_own_data_reaches_no_log_and_no_listing(caplog):
    caplog.set_level(logging.DEBUG)
    listing, _ = await _outcome({"listing": ApiAnswer(403, BLOCKED), "me": ApiAnswer(200, ACCOUNT)})

    assert caplog.records, "nothing logged, so this checks nothing"
    assert "the-owners-account-name" not in journal(caplog)
    assert "the-owners-account-name" not in repr(listing)
