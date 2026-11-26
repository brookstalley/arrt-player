"""The contract's transport table, replayed row by row against the double and the Player.

`transport()` states, for every route a Player calls, each answer it may meet
and what a Player does on meeting it (its `player` column). Arrt's suite drives a
real server through every row; this file drives the server double through every
row, checks the double answered that row (`ServerDouble.answered`), and checks
the Player reacted as the row says: the wall's Pull for a wall's feed, its media
and its heartbeat, `ClientPull` and the supervisor for the client document and
the client heartbeat, and `LabelPull` for a label.

**One scenario per row, keyed on the row**, so a row a later tag adds fails here
by name until it has one, and a scenario for a row the table dropped fails too.
The conditional-GET cases and the `unanswered` row are replayed the same way, and
the Player's document bounds are quoted against the table's `max_bytes`.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from arrt_contract import released, transport
from conftest import WALL_ID
from server_double import BODY_LIMIT_BYTES, CLIENT_ID, CONTRACT, ServerDouble, contract_etag, row

from arrt_player import pull as pull_module
from arrt_player.client import LabelAssignment, LabelOutputReport, OutputReport, Supervisor
from arrt_player.manifest import MEDIA_DIRNAME, media_name
from arrt_player.pull import CLIENT_ETAG_FILENAME, ETAG_FILENAME, ClientPull, LabelPull, Pull

TABLE = transport()
ROWS = [(route, answer["case"]) for route, spec in TABLE["routes"].items() for answer in spec["answers"]]

#: The frame output the double's default wall is placed on, as the client reports it.
FRAME = OutputReport(name="frame", kind="frame", connected=True, screen=None, identity="uuid:frame-in-the-living-room")

#: Where the Player's settings page is, as its client heartbeat says: every client-heartbeat row is replayed over a
#: report that carries one and the Frame's identity, as a running Player's does.
SETTINGS_URL = "http://192.0.2.10:8771/"
#: The panel a label output is drawn on.
PANEL = LabelOutputReport(name="epd-0", kind="epaper", connected=True, size=(1448, 1072))

#: A label document the contract calls valid.
LABEL_DOCUMENT = json.loads((CONTRACT / "fixtures" / "label.v1" / "valid" / "showing-art.json").read_text())
#: A wall heartbeat the contract calls valid.
WALL_HEARTBEAT = json.loads((CONTRACT / "fixtures" / "heartbeat.v1" / "valid" / "fresh-start-no-panel.json").read_text())


@dataclass
class World:
    """One double and the Player's pieces pointed at it."""

    stub: ServerDouble
    session: aiohttp.ClientSession
    wall: object
    client: object
    caplog: pytest.LogCaptureFixture

    def pull(self) -> Pull:
        (self.wall.wall_dir / MEDIA_DIRNAME).mkdir(parents=True, exist_ok=True)
        return Pull(self.wall)

    def cached_feed(self) -> dict | None:
        path = self.wall.manifest_path
        return json.loads(path.read_text()) if path.exists() else None

    def events(self, event: str) -> list[logging.LogRecord]:
        return [record for record in self.caplog.records if record.__dict__.get("event") == event]

    def posts(self, path: str) -> int:
        return self.stub.requests.count(("POST", path))

    def gets(self, path: str) -> int:
        return self.stub.requests.count(("GET", path))

    def write_heartbeat(self, reported_at: str, **changes: object) -> None:
        body = json.dumps({**WALL_HEARTBEAT, "reported_at": reported_at, **changes})
        self.write_heartbeat_text(body)

    def write_heartbeat_text(self, text: str) -> None:
        (self.wall.heartbeat_root / f"display-heartbeat-{WALL_ID}.json").write_text(text)


@pytest.fixture
async def world(settings, client_settings, caplog):
    stub = ServerDouble(wall_id=WALL_ID, output="frame")
    server = TestServer(stub.app())
    await server.start_server()
    url = str(server.make_url("")).rstrip("/")
    caplog.set_level(logging.INFO, logger="arrt_player")
    async with aiohttp.ClientSession() as session:
        yield World(
            stub=stub,
            session=session,
            wall=replace(settings, server_url=url),
            client=replace(client_settings, server_url=url),
            caplog=caplog,
        )
    await server.close()


# -- the supervisor, for the client routes ------------------------------------------------------


async def _idle(wall, output, stop) -> None:
    await stop.wait()


class _Labels:
    def __init__(self) -> None:
        self.started: list[LabelAssignment] = []

    async def __call__(self, assignment, stop) -> None:
        self.started.append(assignment)
        await stop.wait()


class _Supervised:
    """A supervisor on the real `ClientPull`, idle workers, a Frame and one panel, driven a cycle at a time."""

    def __init__(self, world: World, outputs: Callable[[], list[OutputReport]] = lambda: [FRAME]) -> None:
        self.link = ClientPull(world.client)
        loop = asyncio.get_running_loop()
        self.supervisor = Supervisor(
            settings=world.client,
            link=self.link,
            worker=_idle,
            outputs=outputs,
            now=lambda: datetime.now(UTC),
            monotonic=loop.time,
            label_worker=_Labels(),
            label_outputs=lambda: [PANEL],
            settings_url=lambda: SETTINGS_URL,
        )

    async def cycles(self, count: int = 1) -> None:
        for _ in range(count):
            await self.supervisor.cycle()
            await asyncio.sleep(0)

    async def close(self) -> None:
        await self.supervisor.stop_all()
        await self.link.close()


async def _supervised(world: World, body: Callable[[_Supervised], Awaitable[None]], **kwargs) -> None:
    supervised = _Supervised(world, **kwargs)
    try:
        await body(supervised)
    finally:
        await supervised.close()


# -- the rows -------------------------------------------------------------------------------------


async def client_ok(world: World) -> None:
    """Adopt it: a worker for each wall and a renderer for each label it lists, and neither for one it stops listing."""
    world.stub.map_label("l-hall", "epd-0", WALL_ID, LABEL_DOCUMENT)

    async def body(supervised: _Supervised) -> None:
        await supervised.cycles()
        assert supervised.supervisor.running == {WALL_ID: "frame"}
        assert supervised.supervisor.labels == {"epd-0": LabelAssignment("l-hall", "epd-0", WALL_ID)}
        world.stub.unassign(WALL_ID)
        world.stub.unmap_label("l-hall")
        await supervised.cycles()
        assert supervised.supervisor.running == {}, "a wall the document stopped listing kept its worker"
        assert supervised.supervisor.labels == {}, "a label the document stopped listing kept its renderer"

    await _supervised(world, body)


async def client_unchanged(world: World) -> None:
    """Keep the document it holds."""

    async def body(supervised: _Supervised) -> None:
        await supervised.cycles()
        await supervised.cycles()
        assert supervised.supervisor.running == {WALL_ID: "frame"}
        assert supervised.link.cached() is not None

    await _supervised(world, body)


async def client_no_valid_token(world: World) -> None:
    """A configuration error: said once, what it has kept, and asked again."""

    async def body(supervised: _Supervised) -> None:
        await supervised.cycles()
        world.stub.tokens = {}
        await supervised.cycles(3)
        assert supervised.supervisor.running == {WALL_ID: "frame"}
        assert world.gets("/client") == 4, "the client stopped asking"
        assert [record.status for record in world.events("client.refused")] == [401]

    await _supervised(world, body)


async def client_heartbeat_accepted(world: World) -> None:
    """Nothing to do until the report changes or its interval comes round."""

    async def body(supervised: _Supervised) -> None:
        await supervised.cycles(3)
        assert world.posts("/client/heartbeat") == 1
        assert len(world.stub.client_heartbeats) == 1
        taken = world.stub.client_heartbeats[0]
        assert (taken["settings_url"], taken["outputs"][0]["identity"]) == (SETTINGS_URL, FRAME.identity)

    await _supervised(world, body)


async def _sent_once_then_on_change(world: World, outputs: list[list[OutputReport]]) -> None:
    """A refused report: sent once, said once, and sent again only when it changes."""
    current = [outputs[0]]

    async def body(supervised: _Supervised) -> None:
        await supervised.cycles(3)
        assert world.posts("/client/heartbeat") == 1, "a refused, unchanged report was sent again"
        current[0] = outputs[1]
        await supervised.cycles(3)
        assert world.posts("/client/heartbeat") == 2, "a changed report was not sent once"
        assert len(world.events("client.heartbeat_refused")) == 1

    await _supervised(world, body, outputs=lambda: current[0])


def _hdmi(name: str) -> OutputReport:
    return OutputReport(name=name, kind="framebuffer", connected=True, screen=(1920, 1080))


async def client_heartbeat_not_json(world: World) -> None:
    # The Player writes JSON, so the double is made to answer as for a body that is not.
    world.stub.client_heartbeat_status = 400
    await _sent_once_then_on_change(world, [[FRAME], [FRAME, _hdmi("hdmi-a-1")]])


async def client_heartbeat_not_a_client_heartbeat(world: World) -> None:
    # Two outputs of one name, which the schema cannot refuse and the server does.
    twice = [_hdmi("hdmi-a-1"), _hdmi("hdmi-a-1")]
    await _sent_once_then_on_change(world, [twice, [*twice, _hdmi("hdmi-a-2")]])


async def client_heartbeat_too_large(world: World) -> None:
    many = [_hdmi(f"hdmi-a-{n}") for n in range(1, BODY_LIMIT_BYTES // 60)]
    await _sent_once_then_on_change(world, [many, [*many, _hdmi("hdmi-b-1")]])


async def client_heartbeat_no_valid_token(world: World) -> None:
    """A configuration error: said once, and the report goes on being sent."""
    world.stub.tokens = {}

    async def body(supervised: _Supervised) -> None:
        await supervised.cycles(3)
        assert world.posts("/client/heartbeat") == 3, "a report refused for its token stopped being sent"
        assert [record.status for record in world.events("client.heartbeat_refused")] == [401]

    await _supervised(world, body)


async def manifest_major_ok(world: World) -> None:
    """Adopted when it reads, at the major asked for; a feed it refuses keeps the last good one."""
    pull = world.pull()
    published = world.stub.publish("w1")
    assert await pull.cycle(world.session) is True
    assert world.cached_feed() == published
    assert world.stub.majors_requested == [2]
    world.stub.manifest = {"schema": {"major": 2, "minor": 0}}
    await pull.cycle(world.session)
    assert world.cached_feed() == published, "a feed the reader refuses replaced the last good one"


async def manifest_major_unchanged(world: World) -> None:
    pull = world.pull()
    published = world.stub.publish("w1")
    await pull.cycle(world.session)
    media_asked = len([path for _, path in world.stub.requests if path.startswith("/media/")])
    await pull.cycle(world.session)
    assert world.cached_feed() == published
    assert len([path for _, path in world.stub.requests if path.startswith("/media/")]) == media_asked


async def _feed_kept_and_said_once(world: World, status: int, change: Callable[[], None]) -> None:
    pull = world.pull()
    published = world.stub.publish("w1")
    await pull.cycle(world.session)
    change()
    for _ in range(3):
        assert await pull.cycle(world.session) is True
    assert world.cached_feed() == published, "a refusal cost the wall its feed"
    assert [record.status for record in world.events("pull.refused")] == [status]


async def manifest_major_no_valid_token(world: World) -> None:
    await _feed_kept_and_said_once(world, 401, lambda: setattr(world.stub, "tokens", {}))


async def manifest_major_not_this_clients_wall(world: World) -> None:
    await _feed_kept_and_said_once(world, 403, lambda: world.stub.unassign(WALL_ID))


async def manifest_major_nothing_hung(world: World) -> None:
    def take_it_down() -> None:
        world.stub.manifest = None

    await _feed_kept_and_said_once(world, 404, take_it_down)


async def manifest_major_major_not_published(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The next major down is asked; only every major answering 404 is a misconfigured wall."""
    monkeypatch.setattr(pull_module, "REQUESTED_MAJORS", (3, 2))
    pull = world.pull()
    published = world.stub.publish("w1")
    await pull.cycle(world.session)
    assert world.stub.majors_requested == [3, 2]
    assert world.cached_feed() == published
    assert world.events("pull.refused") == [], "a major not published was taken for a misconfigured wall"

    world.stub.served_at[WALL_ID] = 4
    for _ in range(2):
        await pull.cycle(world.session)
    assert world.cached_feed() == published
    assert [record.status for record in world.events("pull.refused")] == [404]


async def media_ok(world: World) -> None:
    """Checked against the feed's hash and kept."""
    pull = world.pull()
    published = world.stub.publish("w1")
    await pull.cycle(world.session)
    sha = published["works"]["w1"]["media"]["sha256"]
    assert hashlib.sha256((world.wall.wall_dir / MEDIA_DIRNAME / media_name(sha)).read_bytes()).hexdigest() == sha


async def media_not_held(world: World) -> None:
    """The work is skipped and the wall keeps rotating; the hash is not asked for again under the same feed."""
    pull = world.pull()
    published = world.stub.publish("w1", "w2")
    del world.stub.media[published["works"]["w2"]["media"]["sha256"]]
    await pull.cycle(world.session)
    assert world.cached_feed() == published, "a master the server does not hold kept the feed from the wall"
    missing = published["works"]["w2"]["media"]["url"]
    asked = world.gets(missing)
    await pull.cycle(world.session)
    assert world.gets(missing) == asked


async def media_no_valid_token(world: World) -> None:
    """Said once, and the wall keeps what it shows."""
    pull = world.pull()
    published = world.stub.publish("w1")
    await pull.cycle(world.session)
    world.stub.publish("w2")
    world.stub.media_status = 401
    for _ in range(3):
        await pull.cycle(world.session)
    assert world.cached_feed() == published
    assert len(world.events("pull.media_failing")) == 1


async def _heartbeat_answered(world: World) -> Pull:
    pull = world.pull()
    world.stub.publish("w1")
    return pull


async def heartbeat_accepted(world: World) -> None:
    pull = await _heartbeat_answered(world)
    world.write_heartbeat("2026-09-30T12:00:00+00:00")
    await pull.cycle(world.session)
    await pull.cycle(world.session)
    assert world.posts(f"/walls/{WALL_ID}/heartbeat") == 1
    assert len(world.stub.heartbeats) == 1


async def _heartbeat_sent_once_then_on_change(world: World, first: str, second: str, status: int) -> None:
    pull = await _heartbeat_answered(world)
    world.write_heartbeat_text(first)
    await pull.cycle(world.session)
    await pull.cycle(world.session)
    assert world.posts(f"/walls/{WALL_ID}/heartbeat") == 1, "a refused, unchanged heartbeat was sent again"
    world.write_heartbeat_text(second)
    await pull.cycle(world.session)
    assert world.posts(f"/walls/{WALL_ID}/heartbeat") == 2, "a changed heartbeat was not sent"
    assert [record.status for record in world.events("pull.heartbeat_refused")] == [status]


async def heartbeat_not_a_heartbeat(world: World) -> None:
    invalid = json.dumps({**WALL_HEARTBEAT, "reported_at": "not an instant"})
    await _heartbeat_sent_once_then_on_change(world, invalid, json.dumps({**WALL_HEARTBEAT, "reported_at": 7}), 400)


async def heartbeat_not_an_object(world: World) -> None:
    await _heartbeat_sent_once_then_on_change(world, "not json at all", "[1, 2]", row("heartbeat", "not_an_object")["status"])


async def heartbeat_too_large(world: World) -> None:
    padding = "x" * BODY_LIMIT_BYTES
    first = json.dumps({**WALL_HEARTBEAT, "reported_at": "2026-09-30T12:00:00+00:00", "padding": padding})
    second = json.dumps({**WALL_HEARTBEAT, "reported_at": "2026-09-30T12:01:00+00:00", "padding": padding})
    await _heartbeat_sent_once_then_on_change(world, first, second, 413)


async def heartbeat_no_valid_token(world: World) -> None:
    """Said once, and sent again on its interval: each interval the wall writes a new report, and it goes."""
    pull = await _heartbeat_answered(world)
    await pull.cycle(world.session)
    world.stub.tokens = {}
    for minute in range(3):
        world.write_heartbeat(f"2026-09-30T12:0{minute}:00+00:00")
        await pull.cycle(world.session)
        await pull.cycle(world.session)
    assert world.posts(f"/walls/{WALL_ID}/heartbeat") == 3
    assert [record.status for record in world.events("pull.heartbeat_refused")] == [401]


async def heartbeat_not_this_clients_wall(world: World) -> None:
    """Said once; the supervisor stops the wall when its client document stops listing it (`client_ok`)."""
    pull = await _heartbeat_answered(world)
    await pull.cycle(world.session)
    world.stub.unassign(WALL_ID)
    world.write_heartbeat("2026-09-30T12:00:00+00:00")
    await pull.cycle(world.session)
    await pull.cycle(world.session)
    assert world.posts(f"/walls/{WALL_ID}/heartbeat") == 1
    assert [record.status for record in world.events("pull.heartbeat_refused")] == [403]


async def _label(world: World, label_id: str = "l-hall") -> LabelPull:
    return LabelPull(world.client, label_id)


async def label_ok(world: World) -> None:
    """The document goes to the renderer, which runs the label rule over it."""
    world.stub.map_label("l-hall", "epd-0", WALL_ID, LABEL_DOCUMENT)
    link = await _label(world)
    try:
        answer = await link.fetch()
    finally:
        await link.close()
    assert answer.reachable is True
    assert answer.document is not None
    assert answer.document.wall_id == LABEL_DOCUMENT["wall_id"]


async def label_unchanged(world: World) -> None:
    """The document it holds is kept, so the rule's hold runs on it: no new document, and the server reachable."""
    world.stub.map_label("l-hall", "epd-0", WALL_ID, LABEL_DOCUMENT)
    link = await _label(world)
    try:
        await link.fetch()
        answer = await link.fetch()
        cached = link.cached()
    finally:
        await link.close()
    assert (answer.reachable, answer.document) == (True, None)
    assert cached is not None
    assert cached.document.wall_id == LABEL_DOCUMENT["wall_id"]


async def _label_refused(world: World, status: int, refuse: Callable[[], None], label_id: str = "l-hall") -> None:
    """A refusal: the last document kept as it is (never read as the server gone), and said once."""
    world.stub.map_label("l-hall", "epd-0", WALL_ID, LABEL_DOCUMENT)
    link = await _label(world, label_id)
    try:
        await link.fetch()
        refuse()
        answers = [await link.fetch() for _ in range(3)]
    finally:
        await link.close()
    assert {(answer.reachable, answer.document) for answer in answers} == {(True, None)}
    assert [record.status for record in world.events("label.refused")] == [status]


async def label_no_valid_token(world: World) -> None:
    await _label_refused(world, 401, lambda: setattr(world.stub, "tokens", {}))


async def label_not_this_clients_label(world: World) -> None:
    world.stub.map_label("l-other", "epd-0", "a-wall", LABEL_DOCUMENT, client_id="c-another")
    await _label_refused(world, 403, lambda: None, label_id="l-other")


async def label_captions_no_wall(world: World) -> None:
    await _label_refused(world, 404, lambda: world.stub.unmap_label("l-hall"))


SCENARIOS: dict[tuple[str, str], Callable[..., Awaitable[None]]] = {
    ("client", "ok"): client_ok,
    ("client", "unchanged"): client_unchanged,
    ("client", "no_valid_token"): client_no_valid_token,
    ("client_heartbeat", "accepted"): client_heartbeat_accepted,
    ("client_heartbeat", "not_json"): client_heartbeat_not_json,
    ("client_heartbeat", "not_a_client_heartbeat"): client_heartbeat_not_a_client_heartbeat,
    ("client_heartbeat", "too_large"): client_heartbeat_too_large,
    ("client_heartbeat", "no_valid_token"): client_heartbeat_no_valid_token,
    ("manifest_major", "ok"): manifest_major_ok,
    ("manifest_major", "unchanged"): manifest_major_unchanged,
    ("manifest_major", "no_valid_token"): manifest_major_no_valid_token,
    ("manifest_major", "not_this_clients_wall"): manifest_major_not_this_clients_wall,
    ("manifest_major", "major_not_published"): manifest_major_major_not_published,
    ("manifest_major", "nothing_hung"): manifest_major_nothing_hung,
    ("media", "ok"): media_ok,
    ("media", "not_held"): media_not_held,
    ("media", "no_valid_token"): media_no_valid_token,
    ("heartbeat", "accepted"): heartbeat_accepted,
    ("heartbeat", "not_a_heartbeat"): heartbeat_not_a_heartbeat,
    ("heartbeat", "not_an_object"): heartbeat_not_an_object,
    ("heartbeat", "too_large"): heartbeat_too_large,
    ("heartbeat", "no_valid_token"): heartbeat_no_valid_token,
    ("heartbeat", "not_this_clients_wall"): heartbeat_not_this_clients_wall,
    ("label", "ok"): label_ok,
    ("label", "unchanged"): label_unchanged,
    ("label", "no_valid_token"): label_no_valid_token,
    ("label", "not_this_clients_label"): label_not_this_clients_label,
    ("label", "captions_no_wall"): label_captions_no_wall,
}


def test_every_scenario_is_a_row_of_the_table():
    """The other half of the parametrization below: a row the table dropped leaves no scenario behind."""
    assert sorted(set(SCENARIOS) - set(ROWS)) == []


@pytest.mark.parametrize("route_case", ROWS, ids=[f"{route}-{case}" for route, case in ROWS])
async def test_every_row_is_answered_by_the_double_and_met_by_the_player_as_the_row_says(world, monkeypatch, route_case):
    assert route_case in SCENARIOS, f"the table's row {route_case} has no scenario here"
    scenario = SCENARIOS[route_case]
    if scenario is manifest_major_major_not_published:
        await scenario(world, monkeypatch)
    else:
        await scenario(world)

    assert route_case in world.stub.answered, f"the double never answered {route_case}; it answered {world.stub.answered}"


async def test_a_wall_heartbeat_that_is_not_an_object_is_answered_as_the_corrected_row_says(world):
    """400 in the error shape, as the client heartbeat's route answers it, where 1.2.0's row was the framework's 422."""
    answer = row("heartbeat", "not_an_object")
    assert answer["corrected"]["was"] == {"status": 422, "body": "framework"}, "the row is not the one 1.3.0 corrected"

    async with world.session.post(
        world.client.server_url + f"/walls/{WALL_ID}/heartbeat",
        data=b"[1, 2]",
        headers={"Authorization": f"Bearer {world.client.client_token}", "Content-Type": "application/json"},
    ) as response:
        status, body = response.status, await response.json()

    assert (status, answer["body"]) == (answer["status"], "error")
    assert list(body) == ["error"], f"answered {body}, which is not the error shape"
    assert world.stub.answered == [("heartbeat", "not_an_object")]


async def test_a_wall_heartbeat_answered_as_1_2_0s_row_did_is_met_as_the_current_row_says(world):
    """A server on 1.2.0 answers 422 here; both rows say a Player reacts as for 400, so nothing it does changes."""
    old = next(
        answer
        for answer in json.loads((released()["1.2.0"] / "transport.json").read_text())["routes"]["heartbeat"]["answers"]
        if answer["case"] == "not_an_object"
    )
    assert old["player"] == row("heartbeat", "not_an_object")["player"] == "As for 400."
    world.stub.heartbeat_status = old["status"]

    await _heartbeat_sent_once_then_on_change(world, "not json at all", "[1, 2]", old["status"])


# -- the double holds a Player to the heartbeat schemas ------------------------------------------------

_CLIENT_HEARTBEATS = [
    pytest.param(fixture, id=fixture.stem)
    for kind in ("valid", "invalid")
    for fixture in sorted((CONTRACT / "fixtures" / "client-heartbeat.v1" / kind).glob("*.json"))
]


@pytest.mark.parametrize("fixture", _CLIENT_HEARTBEATS)
async def test_the_double_takes_a_client_heartbeat_only_when_the_contract_calls_it_valid(world, fixture):
    """So a Player that writes a bad report fails here as it would against Arrt.

    Stricter than Arrt on the fixtures the index marks `server_must_refuse:
    false` (a bad `settings_url`, which Arrt keeps the rest of the report
    beside): refused here, so a Player writing one fails a test rather than
    passing with no link on the server's page.
    """
    async with world.session.post(
        world.client.server_url + "/client/heartbeat",
        data=fixture.read_bytes(),
        headers={"Authorization": f"Bearer {world.client.client_token}", "Content-Type": "application/json"},
    ) as response:
        status = response.status

    if fixture.parent.name == "valid":
        assert (status, world.stub.answered) == (204, [("client_heartbeat", "accepted")])
    else:
        assert (status, world.stub.answered) == (400, [("client_heartbeat", "not_a_client_heartbeat")])


# -- unanswered -------------------------------------------------------------------------------------


async def test_a_5xx_on_the_manifest_keeps_the_feed_and_backs_off(world):
    pull = world.pull()
    published = world.stub.publish("w1")
    await pull.cycle(world.session)
    world.stub.manifest_status = 503

    assert await pull.cycle(world.session) is False, "a 5xx was not read as no answer"
    assert world.cached_feed() == published
    assert ("manifest_major", "5xx") in world.stub.answered


async def test_a_5xx_on_a_label_reads_the_server_as_gone(world):
    world.stub.map_label("l-hall", "epd-0", WALL_ID, LABEL_DOCUMENT)
    link = await _label(world)
    try:
        await link.fetch()
        world.stub.label_status = 503
        answer = await link.fetch()
    finally:
        await link.close()
    assert answer.reachable is False


async def test_a_5xx_on_either_heartbeat_sends_it_again(world):
    pull = await _heartbeat_answered(world)
    world.write_heartbeat("2026-09-30T12:00:00+00:00")
    world.stub.heartbeat_status = 503
    await pull.cycle(world.session)
    world.stub.heartbeat_status = None
    await pull.cycle(world.session)
    assert len(world.stub.heartbeats) == 1, "a heartbeat the server failed was not sent again"

    world.stub.client_heartbeat_status = 503

    async def body(supervised: _Supervised) -> None:
        await supervised.cycles()
        world.stub.client_heartbeat_status = None
        await supervised.cycles()
        assert len(world.stub.client_heartbeats) == 1, "a client heartbeat the server failed was not sent again"

    await _supervised(world, body)


# -- a status no row names is read by its class -----------------------------------------------------


async def test_a_4xx_no_row_names_on_a_get_is_a_configuration_error(world):
    pull = world.pull()
    published = world.stub.publish("w1")
    await pull.cycle(world.session)
    world.stub.manifest_status = 418

    assert await pull.cycle(world.session) is True
    assert world.cached_feed() == published
    assert [record.status for record in world.events("pull.refused")] == [418]


# -- conditional GET ---------------------------------------------------------------------------------

CONDITIONAL = TABLE["conditional_get"]


def test_the_routes_with_conditional_gets_are_the_three_this_file_replays():
    assert sorted(CONDITIONAL["routes"]) == ["client", "label", "manifest_major"]


class _Conditional:
    """One route's documents A and B, and the Player's piece that asks for it."""

    def __init__(self, world: World, route: str) -> None:
        self.world, self.route = world, route
        self.pull = world.pull() if route == "manifest_major" else None
        self.client_link = ClientPull(world.client) if route == "client" else None
        self.label_link = LabelPull(world.client, "l-hall") if route == "label" else None

    def serve(self, which: str) -> bytes:
        """Have the double hold document `which`, returning the bytes it serves."""
        stub = self.world.stub
        if self.route == "manifest_major":
            if which == "A":
                stub.manifests[WALL_ID] = self._a_feed
            else:
                stub.manifests[WALL_ID] = self._b_feed
            return json.dumps(stub.manifests[WALL_ID]).encode()
        if self.route == "client":
            if which == "B":
                stub.assign("study", "hdmi-a-1")
            else:
                stub.unassign("study")
            return json.dumps({"client_id": CLIENT_ID, **stub.clients[CLIENT_ID]}).encode()
        document = LABEL_DOCUMENT if which == "A" else {**LABEL_DOCUMENT, "label": {"title": "Another title"}}
        stub.map_label("l-hall", "epd-0", WALL_ID, document)
        return json.dumps(document).encode()

    def prepare(self) -> None:
        stub = self.world.stub
        if self.route == "manifest_major":
            self._b_feed = stub.publish("w2")
            self._a_feed = stub.publish("w1")

    async def ask(self) -> object:
        """One request by the Player's own piece; what it adopted, or None when it kept what it had.

        A wall adopts a feed by caching it and then its tag, so a feed whose tag
        moved was adopted, even when its document is the one already cached.
        """
        if self.pull is not None:
            before = (self.world.cached_feed(), self.stored_tag())
            await self.pull.cycle(self.world.session)
            after = (self.world.cached_feed(), self.stored_tag())
            return None if after == before else after[0]
        if self.client_link is not None:
            return await self.client_link.fetch()
        answer = await self.label_link.fetch()
        return answer.document

    def stored_tag(self) -> str | None:
        if self.route == "manifest_major":
            path = self.world.wall.wall_dir / ETAG_FILENAME
        elif self.route == "client":
            path = self.world.client.cache_dir / CLIENT_ETAG_FILENAME
        else:
            return None  # A label's tag is held in memory only, and the next request shows it.
        return path.read_text() if path.exists() else None

    async def close(self) -> None:
        for link in (self.client_link, self.label_link):
            if link is not None:
                await link.close()


@pytest.mark.parametrize("route", CONDITIONAL["routes"])
@pytest.mark.parametrize("case", CONDITIONAL["cases"], ids=[case["name"] for case in CONDITIONAL["cases"]])
async def test_every_conditional_get_case_is_answered_by_the_double_and_met_by_the_player(world, route, case):
    """The double answers each case as the table does, and the Player adopts a 200 with its tag and keeps on a 304."""
    replay = _Conditional(world, route)
    try:
        replay.prepare()
        bodies = {"A": replay.serve("A")}
        if case["if_none_match"] is not None:
            # The Player holds document A, under the tag the case names it sent.
            sent = case["if_none_match"]
            if not sent.startswith("etag("):
                world.stub.etag_for = lambda body, sent=sent: sent
            assert await replay.ask() is not None, "the Player did not take document A to begin with"
            world.stub.etag_for = contract_etag
        bodies[case["document"]] = replay.serve(case["document"])
        tags = {name: world.stub.etag_for(body) for name, body in bodies.items()}
        world.stub.answered.clear()

        adopted = await replay.ask()

        expected_case = "ok" if case["status"] == 200 else "unchanged"
        assert world.stub.answered[0] == (route, expected_case)
        assert row(route, expected_case)["status"] == case["status"]
        expected_tag = tags[case["etag"].removeprefix("etag(").removesuffix(")")]
        if case["status"] == 304:
            assert adopted is None, "a 304 replaced the document the Player holds"
        else:
            assert adopted is not None, "a 200 was not adopted"
        if route != "label":
            assert replay.stored_tag() == expected_tag, "the tag stored is not the one the document was served with"
    finally:
        await replay.close()


# -- the bounds, quoted ------------------------------------------------------------------------------


def test_the_routes_with_a_size_bound_are_the_three_documents_this_player_bounds():
    bounded = {route for route, spec in TABLE["routes"].items() if "max_bytes" in spec}
    assert bounded == {"client", "label", "manifest_major"}


def test_the_player_reads_each_document_to_the_bound_its_route_states():
    """The Player's bounds are the contract's: one past them and a document the server may send is refused."""
    assert TABLE["routes"]["manifest_major"]["max_bytes"] == pull_module.MANIFEST_MAX_BYTES
    assert TABLE["routes"]["client"]["max_bytes"] == pull_module.DOCUMENT_MAX_BYTES
    assert TABLE["routes"]["label"]["max_bytes"] == pull_module.DOCUMENT_MAX_BYTES
