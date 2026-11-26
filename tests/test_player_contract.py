"""Arrt Player's side of the Player contract: what it reads, and what it writes.

The manifest fixtures under `contract/` are the documents Arrt may publish.
Every one the contract calls valid must be one this reader adopts. Every invalid
one that the contract's index marks `player_must_refuse` must be one it refuses.
Those are the rules the reader enforces rather than trusts. The other invalid
fixtures break writer obligations the reader deliberately tolerates, and they
are not asserted here in either direction.

**This reader reads major 2 only**, so every valid major 2 fixture is adopted
whole and every one the index marks `player_must_refuse` is refused. **A major
it does not read must be refused as an *unsupported version***, not as a
malformed document, because that is the cutover rule each major relies on: a
Player keeps its wall rather than misreading another shape, and says "this
Player does not read that major" rather than "debug the server". It holds both
ways: major 3 stands in for a future major, built from a valid major 2
fixture, and the contract's major 1 document for the major this Player no
longer reads (`player-contract.md` § The cutover).

The heartbeat runs the other way: what `Health.document()` writes must validate
against the contract's heartbeat schema, for a Player mid-flight and for one
that has only just started.

**The client's two documents are the same pair, one level up.** The Player reads
the client document (`client.v1`): every valid fixture must be read whole and
every invalid one refused. It writes the client heartbeat
(`client-heartbeat.v1`): what it reports must validate, and for the fixture's
own machine must be the fixture.

These tests read JSON files and never import curation, which is the
isolation norm; after the repo split they read a pinned copy of the same
files.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from arrt_contract import MAX_OUTPUTS_PER_REPORT, released, root, semantic_errors, validator, vocabulary
from fakes import RecordingOutput, drm_tree

from arrt_player import manifest
from arrt_player.client import (
    ClientDocumentUnreadable,
    LabelOutputReport,
    OutputReport,
    client_heartbeat,
    client_outputs,
    parse_client_document,
)
from arrt_player.config import DEFAULT_MAT_BOTTOM_WEIGHT
from arrt_player.displays.frame import frame_wall
from arrt_player.displays.screen import screen_wall
from arrt_player.heartbeat import DisplayReport, Health, ScreenState, path_in
from arrt_player.label_rule import STATES, LabelDocumentUnreadable, Outcome, outcome, parse_label_document
from arrt_player.manifest import Watcher
from arrt_player.settings_ui.server import PAIRING_LABELS

CONTRACT = root()
INDEX = json.loads((CONTRACT / "fixtures" / "index.json").read_text(encoding="utf-8"))["fixtures"]
FEEDS = [row for row in INDEX if row["schema"] == "schemas/manifest.v2.schema.json"]


def _parse(row: dict) -> manifest.Feed:
    text = (CONTRACT / row["path"]).read_text(encoding="utf-8")
    return manifest.parse(text)


@pytest.mark.parametrize(
    "row",
    [row for row in FEEDS if not row["valid"] and row["player_must_refuse"]],
    ids=lambda row: row["path"],
)
def test_every_manifest_the_contract_says_to_refuse_is_refused_for_the_rule_its_name_gives(row):
    """For its own rule, not any: a fixture refused by some other check would pass while its rule went unenforced."""
    name = row["path"].rsplit("/", 1)[1]
    assert name in _REFUSED_BECAUSE, f"{row['path']} is to be refused, and this test does not say for what"

    with pytest.raises(manifest.ManifestUnreadable, match=_REFUSED_BECAUSE[name]):
        _parse(row)


#: Why each fixture the index marks `player_must_refuse` is refused, as the
#: reader says it. Keyed by filename, which names the one rule the fixture breaks.
_REFUSED_BECAUSE = {
    "document-missing-schema.json": "carries no schema major version",
    "document-not-an-object.json": "not an object",
    "horizon-not-whole-days.json": "not a whole number of days",
    "horizon-of-no-length.json": "not a whole number of days",
    # A major 1 document under a major 2 schema: a major this Player does not read.
    "major-1.json": "schema major 1 is not supported",
    "media-url-empty.json": "carries no media with a sha256 and a url",
    "scene-ends-before-it-starts.json": "the scene ends before it starts",
    "scene-names-a-work-not-in-works.json": "names a work that is not in its works",
    "scene-neither-null-nor-an-object.json": "the scene is neither null nor an object",
    "schedule-horizon-missing-from.json": "the horizon's from is missing",
    "schedule-horizon-not-an-object.json": "carries no horizon",
    "schedule-missing-horizon.json": "carries no horizon",
    "schedule-not-an-object.json": "carries no schedule",
    "schedule-slot-not-an-object.json": "slot 0 is not an object",
    "schedule-slot-work-id-empty.json": "slot 0 carries no work_id",
    "schedule-slot-work-id-not-text.json": "slot 0 carries no work_id",
    "schedule-slots-not-a-list.json": "carries no slots list",
    "schema-missing-major.json": "carries no schema major version",
    "schema-not-an-object.json": "carries no schema major version",
    "slot-ends-before-it-starts.json": "a slot ends before it starts",
    "slot-missing-until.json": "slot 0's until is missing",
    "slot-names-a-work-not-in-works.json": "names a work that is not in its works",
    "slot-outside-the-horizon.json": "a slot falls outside the horizon",
    "slot-starts-before-the-horizon.json": "a slot falls outside the horizon",
    "slots-overlap.json": "the slots overlap or run out of order",
    "staging-empty.json": "staging is not a list of work ids",
    "staging-names-a-work-not-in-works.json": "names a work that is not in its works",
    "staging-not-a-list.json": "staging is not a list of work ids",
    "staging-not-text.json": "staging is not a list of work ids",
    # A repeated entry is a malformed list in a field this reader acts on, not a writer's slip it tolerates.
    "staging-repeated.json": "staging lists a work more than once",
    "work-media-not-an-object.json": "carries no media with a sha256 and a url",
    "work-media-sha256-malformed.json": "carries no media with a sha256 and a url",
    "work-media-sha256-not-text.json": "carries no media with a sha256 and a url",
    "work-media-url-not-text.json": "carries no media with a sha256 and a url",
    "work-missing-media.json": "carries no media with a sha256 and a url",
    "work-not-an-object.json": "is not an object",
    "works-keyed-by-an-empty-id.json": "works has an empty id",
    "works-not-an-object.json": "carries no works object",
}


def test_every_reason_given_for_a_refusal_is_for_a_fixture_the_contract_says_to_refuse():
    """The other half of the table: a fixture the contract dropped, or no longer marks, leaves no reason standing."""
    must_refuse = {row["path"].rsplit("/", 1)[1] for row in FEEDS if not row["valid"] and row["player_must_refuse"]}

    assert sorted(set(_REFUSED_BECAUSE) - must_refuse) == []


@pytest.mark.parametrize("row", [row for row in FEEDS if row["valid"]], ids=lambda row: row["path"])
def test_every_valid_major_2_feed_in_the_contract_is_adopted_whole(row):
    """Every field the Player acts on, read from the document: the works, the schedule, the scene and staging."""
    document = json.loads((CONTRACT / row["path"]).read_text(encoding="utf-8"))

    adopted = _parse(row)

    assert isinstance(adopted, manifest.Feed)
    assert (adopted.schema_major, adopted.schema_minor) == (2, document["schema"]["minor"])
    assert (adopted.playlist_id, adopted.playlist_name) == (document["playlist"]["id"], document["playlist"]["name"])
    assert {work_id: (work.sha256, work.url, work.label) for work_id, work in adopted.works.items()} == {
        work_id: (work["media"]["sha256"], work["media"]["url"], work["label"]) for work_id, work in document["works"].items()
    }
    horizon = document["schedule"]["horizon"]
    assert (adopted.horizon_start, adopted.horizon_until) == (_at(horizon["from"]), _at(horizon["until"]))
    assert [(slot.work_id, slot.start, slot.until) for slot in adopted.slots] == [
        (slot["work_id"], _at(slot["from"]), _at(slot["until"])) for slot in document["schedule"]["slots"]
    ]
    scene = document.get("scene")
    if scene is None:
        assert adopted.scene is None
    else:
        assert adopted.scene == manifest.Scene(
            scene_id=scene["id"],
            work_id=scene["work_id"],
            start=_at(scene["from"]),
            until=None if scene["until"] is None else _at(scene["until"]),
        )
    assert adopted.staging == tuple(document.get("staging", []))
    assert adopted.settings == document.get("settings", {})


@pytest.mark.parametrize(
    "row",
    [row for row in FEEDS if not row["valid"] and not row["player_must_refuse"]],
    ids=lambda row: row["path"],
)
def test_a_feed_that_breaks_only_a_writer_obligation_is_still_adopted(row):
    """The index's `false` is a promise too: a setting this Player cannot honour costs a setting, never the wall."""
    assert isinstance(_parse(row), manifest.Feed)


def test_the_major_this_player_no_longer_reads_is_refused_as_a_version():
    """Not as a malformed document: the wall keeps what it shows and the journal says which major arrived."""
    (row,) = [row for row in FEEDS if row["path"].endswith("/invalid/major-1.json")]

    with pytest.raises(manifest.ManifestVersionUnsupported) as refused:
        _parse(row)

    assert refused.value.major == 1


def test_a_future_major_is_refused_as_a_version_not_as_a_malformed_document():
    (row,) = [row for row in FEEDS if row["path"].endswith("/channel-feed.json")]
    document = json.loads((CONTRACT / row["path"]).read_text(encoding="utf-8"))
    document["schema"]["major"] = 3

    with pytest.raises(manifest.ManifestVersionUnsupported) as refused:
        manifest.parse(json.dumps(document))

    assert refused.value.major == 3


def _at(instant: str) -> datetime:
    return datetime.fromisoformat(instant)


def _heartbeat_errors(document: dict) -> list[str]:
    return [error.message for error in validator("schemas/heartbeat.v1.schema.json").iter_errors(document)]


def test_a_heartbeat_from_a_running_player_conforms():
    health = Health(
        manifest_schema="2.0",
        theme_id="th-surrealism",
        current_work_id="w-dali-1",
        announced_content_id="MY_F0042",
        television_reachable=True,
        television_showing_art=True,
        has_label_surface=True,
        label_surface_working=False,
        last_error="the panel did not answer",
    )

    assert _heartbeat_errors(health.document(reported_at=datetime(2026, 9, 30, 14, 0, 5, 123456, tzinfo=UTC))) == []


@pytest.mark.parametrize("state", list(ScreenState))
def test_a_heartbeat_carrying_each_display_state_conforms(state):
    work_id = "w-dali-1" if state is ScreenState.SHOWING_ART else None
    since = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    health = Health(display_state=DisplayReport(state=state, work_id=work_id, since=since))

    document = health.document(reported_at=datetime(2026, 10, 8, 14, 0, 5, tzinfo=UTC))

    assert _heartbeat_errors(document) == []
    assert document["schema"] == {"major": 1, "minor": 4}


async def test_what_a_wall_on_a_screen_writes_conforms_and_says_what_it_can_do(client_settings, wall_dir, publish, clock):
    """Written by the real loop and driver, so a key the schema does not know, or one it requires and lacks, fails here."""
    wall = client_settings.wall("living-room")
    watcher = Watcher(wall.manifest_path)
    loop = screen_wall(wall=wall, output=RecordingOutput(screen=(3840, 2160)), watcher=watcher, clock=clock.as_clock())
    publish(["w1"])

    await loop.tick()

    document = json.loads(path_in(wall.heartbeat_root, "living-room").read_text())
    assert _heartbeat_errors(document) == []
    assert document["capabilities"] == {
        "screen": {"width_px": 3840, "height_px": 2160},
        "backend": "framebuffer",
        "label_modes": ["none"],
        "manifest_majors": [2],
    }
    assert document["scene_id"] is None


async def test_what_a_wall_with_its_screen_unplugged_writes_conforms_and_says_what_it_reads(
    client_settings, wall_dir, publish, clock
):
    wall = client_settings.wall("living-room")
    watcher = Watcher(wall.manifest_path)
    loop = screen_wall(wall=wall, output=RecordingOutput(screen=None), watcher=watcher, clock=clock.as_clock())
    publish(["w1"])

    await loop.tick()

    document = json.loads(path_in(wall.heartbeat_root, "living-room").read_text())
    assert _heartbeat_errors(document) == []
    assert document["schema"] == {"major": 1, "minor": 4}
    assert document["capabilities"] == {"backend": "framebuffer", "label_modes": ["none"], "manifest_majors": [2]}


async def test_what_a_wall_on_the_frame_writes_conforms_and_says_what_it_can_do(settings, tv, state, publish, clock):
    """The Frame's configured panel, which this Player composes for, and the one major it reads."""
    watcher = Watcher(settings.manifest_path)
    loop = frame_wall(settings=settings, tv=tv, state=state, watcher=watcher, clock=clock.as_clock())
    publish(["w1"])

    await loop.tick()

    document = json.loads(path_in(settings.heartbeat_root, settings.wall_id).read_text())
    assert _heartbeat_errors(document) == []
    assert document["capabilities"] == {
        "screen": {"width_px": settings.tv_panel_width_px, "height_px": settings.tv_panel_height_px},
        "backend": "frame",
        "label_modes": ["none"],
        "manifest_majors": [2],
    }


def test_a_display_state_naming_a_work_beside_anything_but_art_is_refused_before_it_is_written():
    """The schema refuses it; the writer refuses it first, so it never reaches the server."""
    with pytest.raises(ValueError, match="names no work"):
        DisplayReport(state=ScreenState.IN_USE, work_id="w-dali-1", since=datetime(2026, 10, 8, tzinfo=UTC))


def test_a_heartbeat_from_a_player_that_has_only_just_started_conforms():
    assert _heartbeat_errors(Health().document(reported_at=datetime(2026, 9, 30, 14, 0, 5, tzinfo=UTC))) == []


# -- the client document, which the Player reads -------------------------------------------

CLIENT_DOCUMENTS = [row for row in INDEX if row["schema"] == "schemas/client.v1.schema.json"]
CLIENT_HEARTBEATS = [row for row in INDEX if row["schema"] == "schemas/client-heartbeat.v1.schema.json"]


def test_the_index_holds_client_documents_and_client_heartbeats_of_both_kinds():
    """The vacuity check: the parametrized tests below pass trivially over an empty list."""
    for rows in (CLIENT_DOCUMENTS, CLIENT_HEARTBEATS):
        assert {row["valid"] for row in rows} == {True, False}


@pytest.mark.parametrize("row", [row for row in CLIENT_DOCUMENTS if row["valid"]], ids=lambda row: row["path"])
def test_every_valid_client_document_is_read_whole(row):
    """Every wall, with its id, name, output and display, and every label, read from the document in its order."""
    document = json.loads((CONTRACT / row["path"]).read_text(encoding="utf-8"))

    read = parse_client_document((CONTRACT / row["path"]).read_text(encoding="utf-8"))

    assert (read.client_id, read.name) == (document["client_id"], document["name"])
    assert [(wall.wall_id, wall.name, wall.output, wall.display) for wall in read.walls] == [
        (wall["wall_id"], wall["name"], wall["output"], wall.get("display")) for wall in document["walls"]
    ]
    assert [(label.label_id, label.output, label.wall_id) for label in read.labels] == [
        (label["label_id"], label["output"], label["wall_id"]) for label in document.get("labels", [])
    ]


@pytest.mark.parametrize("row", [row for row in CLIENT_DOCUMENTS if not row["valid"]], ids=lambda row: row["path"])
def test_every_invalid_client_document_is_refused_whole(row):
    """Refused rather than read in part: acting on the readable part would stop
    the walls the unreadable part named."""
    with pytest.raises(ClientDocumentUnreadable):
        parse_client_document((CONTRACT / row["path"]).read_text(encoding="utf-8"))


# -- the client heartbeat, which the Player writes -----------------------------------------


CLIENT_HEARTBEAT_SCHEMA = "schemas/client-heartbeat.v1.schema.json"


def _client_heartbeat_errors(document: dict) -> list[str]:
    """What the contract says is wrong with a client heartbeat: the schema's errors, then the rules a schema cannot state.

    The second is asked only of a document the schema accepts, which is how the
    package states it (`semantic_errors`).
    """
    errors = [error.message for error in validator(CLIENT_HEARTBEAT_SCHEMA).iter_errors(document)]
    return errors or semantic_errors(document, schema=CLIENT_HEARTBEAT_SCHEMA)


@pytest.mark.parametrize("row", CLIENT_HEARTBEATS, ids=lambda row: row["path"])
def test_the_schema_and_the_contracts_semantic_rules_together_judge_the_client_heartbeat_fixtures_as_the_index_says(row):
    """So the check below is one that can say no, to the four reports only the semantic rules refuse."""
    errors = _client_heartbeat_errors(json.loads((CONTRACT / row["path"]).read_text(encoding="utf-8")))

    assert (errors == []) is row["valid"], errors


@pytest.mark.parametrize("row", [row for row in CLIENT_HEARTBEATS if not row["valid"]], ids=lambda row: row["path"])
def test_each_invalid_client_heartbeat_is_refused_by_the_judge_the_index_names(row):
    """`breaks` says whether the schema or a semantic rule refuses it: the schema's check alone must not hold the second kind."""
    document = json.loads((CONTRACT / row["path"]).read_text(encoding="utf-8"))
    by_schema = [error.message for error in validator(CLIENT_HEARTBEAT_SCHEMA).iter_errors(document)]

    assert row["breaks"] in {"schema", "semantics"}, f"{row['path']} breaks {row['breaks']!r}, a kind this test does not judge"
    assert (by_schema != []) is (row["breaks"] == "schema"), by_schema
    if row["breaks"] == "semantics":
        assert semantic_errors(document, schema=CLIENT_HEARTBEAT_SCHEMA) != []


def test_what_the_player_writes_for_two_connectors_one_unplugged_is_the_fixture(tmp_path, client_settings):
    """The Player's own report of the fixture's machine, compared output for output."""
    fixture = json.loads((CONTRACT / "fixtures/client-heartbeat.v1/valid/two-connectors-one-unplugged.json").read_text())
    drm = drm_tree(
        tmp_path / "drm",
        {"card1-HDMI-A-1": ("connected", "1920x1080\n1280x720\n"), "card1-HDMI-A-2": ("disconnected", "")},
    )

    written = client_heartbeat(
        client_outputs(replace(client_settings, frame=None), drm_root=drm),
        reported_at=datetime(2026, 10, 2, 14, 0, 5, tzinfo=UTC),
    )

    assert _client_heartbeat_errors(written) == []
    assert written["outputs"] == fixture["outputs"]


def test_what_a_player_with_a_frame_writes_conforms(tmp_path, client_settings):
    drm = drm_tree(tmp_path / "drm", {"card1-HDMI-A-1": ("connected", "1280x1024\n")})

    written = client_heartbeat(
        client_outputs(client_settings, drm_root=drm), reported_at=datetime(2026, 10, 2, 14, 0, 5, 250000, tzinfo=UTC)
    )

    assert _client_heartbeat_errors(written) == []
    assert [output["name"] for output in written["outputs"]] == ["frame", "hdmi-a-1"]


def test_what_a_player_with_nothing_to_draw_on_writes_conforms(tmp_path, client_settings):
    written = client_heartbeat(
        client_outputs(replace(client_settings, frame=None), drm_root=tmp_path / "no-drm"),
        reported_at=datetime(2026, 10, 2, 14, 0, 5, tzinfo=UTC),
    )

    assert written["outputs"] == []
    assert _client_heartbeat_errors(written) == []


def _panel(connected: bool = True) -> LabelOutputReport:
    return LabelOutputReport(name="epd-0", kind="epaper", connected=connected, size=(1448, 1072))


def test_what_a_player_with_an_identified_frame_and_a_panel_writes_is_the_fixtures_shape(tmp_path, client_settings):
    """The fixture `frame-with-identity-and-a-panel`, written by the Player's own code."""
    fixture = json.loads((CONTRACT / "fixtures/client-heartbeat.v1/valid/frame-with-identity-and-a-panel.json").read_text())
    frame_fixture = next(output for output in fixture["outputs"] if output["kind"] == "frame")

    written = client_heartbeat(
        client_outputs(client_settings, drm_root=tmp_path / "no-drm", frame_identity=frame_fixture["identity"]),
        label_outputs=[_panel()],
        reported_at=datetime(2026, 10, 8, 14, 0, 5, tzinfo=UTC),
    )

    assert _client_heartbeat_errors(written) == []
    # The Player reports no size for the Frame (its render arrives composed for
    # it), where the fixture's writer knew one.
    assert written["outputs"] == [{**frame_fixture, "screen": None}]
    assert written["label_outputs"] == [{"name": "epd-0", "kind": "epaper", "connected": True, "size": [1448, 1072]}]


def test_what_a_player_with_a_panel_and_no_display_writes_conforms(tmp_path, client_settings):
    written = client_heartbeat(
        client_outputs(replace(client_settings, frame=None), drm_root=tmp_path / "no-drm"),
        label_outputs=[_panel(connected=False)],
        reported_at=datetime(2026, 10, 8, 14, 0, 5, tzinfo=UTC),
    )

    assert _client_heartbeat_errors(written) == []
    assert written["outputs"] == []
    assert written["label_outputs"][0]["connected"] is False


def test_a_frame_whose_id_could_not_be_read_is_written_without_the_key(tmp_path, client_settings):
    """Absent, never empty or null: the schema refuses an empty identity, and the
    server keys an output with none on the client and its name."""
    written = client_heartbeat(
        client_outputs(client_settings, drm_root=tmp_path / "no-drm", frame_identity=None),
        reported_at=datetime(2026, 10, 8, 14, 0, 5, tzinfo=UTC),
    )

    assert _client_heartbeat_errors(written) == []
    assert "identity" not in written["outputs"][0]
    assert "label_outputs" not in written, "a client with no panel wrote a label_outputs key"


def _output(name: str) -> OutputReport:
    return OutputReport(name=name, kind="framebuffer", connected=True, screen=(1920, 1080))


def _report_of(outputs: list[OutputReport], label_outputs: list[LabelOutputReport] | None = None) -> dict:
    return client_heartbeat(outputs, label_outputs=label_outputs, reported_at=datetime(2026, 10, 8, 14, 0, 5, tzinfo=UTC))


def test_the_writers_check_refuses_two_outputs_of_one_name_and_two_label_outputs_of_one_name():
    """Seeded: a Player that wrote either would be refused whole, so the check below must be able to say so."""
    twice = _output("hdmi-a-1")

    assert any("two outputs share a name" in error for error in _client_heartbeat_errors(_report_of([twice, twice])))
    assert any(
        "two label outputs share a name" in error for error in _client_heartbeat_errors(_report_of([], [_panel(), _panel()]))
    )


def test_the_writers_check_refuses_one_output_past_the_contracts_cap_and_not_the_cap_itself():
    """Seeded at the boundary: `MAX_OUTPUTS_PER_REPORT` of each is a report the server takes, one more is refused."""
    names = [f"hdmi-a-{number}" for number in range(1, MAX_OUTPUTS_PER_REPORT + 2)]
    panels = [LabelOutputReport(name=f"epd-{n}", kind="epaper", connected=True, size=(1448, 1072)) for n in names]

    at_the_cap = _report_of([_output(name) for name in names[:-1]], panels[:-1])
    past_the_cap = _report_of([_output(name) for name in names], panels)

    assert _client_heartbeat_errors(at_the_cap) == []
    assert any("more than" in error and "label outputs" not in error for error in _client_heartbeat_errors(past_the_cap))
    assert any("more than" in error and "label outputs" in error for error in _client_heartbeat_errors(past_the_cap))


#: The connectors a host can show the kernel's listing, by what a person would call the machine.
_DRM_TREES = {
    "no display listing": {},
    "one connected connector": {"card1-HDMI-A-1": ("connected", "1920x1080\n")},
    "two connectors, one unplugged": {"card1-HDMI-A-1": ("connected", "1920x1080\n"), "card1-HDMI-A-2": ("disconnected", "")},
    "one connector number on two cards": {
        "card0-HDMI-A-1": ("connected", "1280x720\n"),
        "card1-HDMI-A-1": ("connected", "1920x1080\n"),
    },
    "a connector that is not HDMI": {"card1-DP-1": ("connected", "1920x1080\n"), "card1-HDMI-A-1": ("disconnected", "")},
}


@pytest.mark.parametrize("with_panel", [False, True], ids=["no panel", "a panel"])
@pytest.mark.parametrize("with_frame", [False, True], ids=["no frame", "a frame"])
@pytest.mark.parametrize("tree", list(_DRM_TREES))
def test_what_the_player_writes_for_every_machine_it_can_be_is_held_to_the_schema_and_the_semantic_rules(
    tmp_path, client_settings, tree, with_frame, with_panel
):
    """Every configuration `client_outputs` and the panels make: valid to the schema, no name twice, at most the cap of each."""
    drm = drm_tree(tmp_path / "drm", _DRM_TREES[tree])

    written = client_heartbeat(
        client_outputs(replace(client_settings, frame=client_settings.frame if with_frame else None), drm_root=drm),
        label_outputs=[_panel()] if with_panel else None,
        reported_at=datetime(2026, 10, 8, 14, 0, 5, tzinfo=UTC),
    )

    assert _client_heartbeat_errors(written) == []


# -- the label document, which a label renderer reads -------------------------------------------

LABEL_DOCUMENTS = [row for row in INDEX if row["schema"] == "schemas/label.v1.schema.json"]

#: The invalid label fixtures this reader refuses, each with the reader's own words.
REFUSED_LABEL_DOCUMENTS = {
    "fixtures/label.v1/invalid/display-state-not-an-object.json": "carries no display state",
    "fixtures/label.v1/invalid/display-state-since-not-text-or-null.json": "since is not a string",
    "fixtures/label.v1/invalid/display-state-work-id-not-text-or-null.json": "work_id is not an id",
    "fixtures/label.v1/invalid/document-not-an-object.json": "not an object",
    "fixtures/label.v1/invalid/schema-major-2.json": "is major 2",
    "fixtures/label.v1/invalid/schema-missing-major.json": "is major None",
    "fixtures/label.v1/invalid/schema-not-an-object.json": "is major None",
    "fixtures/label.v1/invalid/since-without-offset.json": "carries no offset",
    "fixtures/label.v1/invalid/wall-id-empty.json": "carries no wall_id or wall_name",
    "fixtures/label.v1/invalid/wall-id-not-text.json": "carries no wall_id or wall_name",
    "fixtures/label.v1/invalid/wall-name-empty.json": "carries no wall_id or wall_name",
    "fixtures/label.v1/invalid/wall-name-missing.json": "carries no wall_id or wall_name",
    "fixtures/label.v1/invalid/wall-name-not-text.json": "carries no wall_id or wall_name",
}

#: The invalid label fixtures this reader reads all the same, each with why it
#: may: they break writer obligations in a field the rule does not act on, or
#: that a later minor may change. Read, never refused, so a writer's slip there
#: costs a caption's detail and not the label.
TOLERATED_LABEL_DOCUMENTS = {
    # The rule never acts on a work id: a state that names none reads it as None.
    "fixtures/label.v1/invalid/display-state-missing-work-id.json",
    # A key a later minor adds is ignored, as every document's unknown keys are.
    "fixtures/label.v1/invalid/display-state-with-an-unknown-key.json",
    # A work named beside a state that shows none is carried and ignored by the rule.
    "fixtures/label.v1/invalid/in-use-naming-a-work.json",
    # A label without a title is drawn as the facts it does carry.
    "fixtures/label.v1/invalid/label-missing-title.json",
    # Only the schema's major is read; a minor is additive and never a reason to refuse.
    "fixtures/label.v1/invalid/schema-minor-negative.json",
    "fixtures/label.v1/invalid/schema-minor-not-a-whole-number.json",
    # A state a later minor adds is read as unreachable (the contract's instruction).
    "fixtures/label.v1/invalid/state-unknown.json",
}


def test_the_index_holds_label_documents_of_both_kinds():
    assert {row["valid"] for row in LABEL_DOCUMENTS} == {True, False}


def test_every_invalid_label_document_is_either_refused_or_tolerated_and_none_is_both():
    """Together equal to the index's invalid rows: one the contract adds with no reason here fails by name."""
    invalid = {row["path"] for row in LABEL_DOCUMENTS if not row["valid"]}

    assert sorted(invalid - set(REFUSED_LABEL_DOCUMENTS) - set(TOLERATED_LABEL_DOCUMENTS)) == []
    assert sorted((set(REFUSED_LABEL_DOCUMENTS) | set(TOLERATED_LABEL_DOCUMENTS)) - invalid) == []
    assert sorted(set(REFUSED_LABEL_DOCUMENTS) & set(TOLERATED_LABEL_DOCUMENTS)) == []


@pytest.mark.parametrize("row", [row for row in LABEL_DOCUMENTS if row["valid"]], ids=lambda row: row["path"])
def test_every_valid_label_document_is_read_whole(row):
    document = json.loads((CONTRACT / row["path"]).read_text(encoding="utf-8"))

    read = parse_label_document((CONTRACT / row["path"]).read_text(encoding="utf-8"))

    assert (read.wall_id, read.wall_name, read.state, read.work_id, read.label) == (
        document["wall_id"],
        document["wall_name"],
        document["display_state"]["state"],
        document["display_state"]["work_id"],
        document["label"],
    )
    assert (read.since is None) is (document["display_state"]["since"] is None)


@pytest.mark.parametrize(("path", "reason"), sorted(REFUSED_LABEL_DOCUMENTS.items()))
def test_every_label_document_this_reader_enforces_is_refused_for_the_rule_its_name_gives(path, reason):
    with pytest.raises(LabelDocumentUnreadable, match=reason):
        parse_label_document((CONTRACT / path).read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", sorted(TOLERATED_LABEL_DOCUMENTS))
def test_every_label_document_this_reader_tolerates_is_read_and_ruled_on(path):
    """Read whole, and the rule gives it an outcome at any hour rather than failing on it."""
    text = (CONTRACT / path).read_text(encoding="utf-8")

    read = parse_label_document(text)

    assert (read.wall_id, read.wall_name, read.state) == (
        json.loads(text)["wall_id"],
        json.loads(text)["wall_name"],
        json.loads(text)["display_state"]["state"],
    )
    assert outcome(read, datetime(2100, 1, 1, tzinfo=UTC)) in set(Outcome)


def test_a_label_document_in_a_state_this_reader_does_not_know_is_read_as_unreachable():
    document = parse_label_document((CONTRACT / "fixtures/label.v1/invalid/state-unknown.json").read_text(encoding="utf-8"))

    assert document.state not in STATES
    assert outcome(document, datetime(2100, 1, 1, tzinfo=UTC)) is Outcome.BLANK


# -- what an older server sent -----------------------------------------------------------------

#: Each reader of a document this Player reads, by the schema of the document.
_READERS = {
    "schemas/manifest.v2.schema.json": manifest.parse,
    "schemas/client.v1.schema.json": parse_client_document,
    "schemas/label.v1.schema.json": parse_label_document,
}


def _released_rows() -> list:
    rows = []
    for version, directory in released().items():
        index = json.loads((directory / "fixtures" / "index.json").read_text(encoding="utf-8"))["fixtures"]
        rows += [
            pytest.param(directory, row, id=f"{version}:{row['path']}")
            for row in index
            if row["valid"] and row["schema"] in _READERS
        ]
    return rows


RELEASED_DOCUMENTS = _released_rows()


def test_every_release_before_this_one_has_valid_documents_of_each_kind_this_player_reads():
    """The vacuity check for the test below: a release with none of a kind would pass it over nothing."""
    assert released(), "the contract carries no earlier release"
    for version, directory in released().items():
        held = {row.values[1]["schema"] for row in RELEASED_DOCUMENTS if row.values[0] == directory}
        assert held == set(_READERS), f"release {version} holds valid documents of only {sorted(held)}"


@pytest.mark.parametrize(("directory", "row"), RELEASED_DOCUMENTS)
def test_every_valid_document_an_earlier_release_held_is_still_read(directory, row):
    """A server on an earlier contract still sends these, and this Player reads each without refusing it."""
    read = _READERS[row["schema"]]((directory / row["path"]).read_text(encoding="utf-8"))

    assert read is not None


# -- the values this Player and the server state alike ---------------------------------------------

MAT_GEOMETRY = json.loads((CONTRACT / "vectors" / "mat-geometry.json").read_text(encoding="utf-8"))


def test_the_default_bottom_weight_is_the_contracts():
    """What the server judges a work's size against, so a Player with its own default would draw a mat it did not judge."""
    assert MAT_GEOMETRY["defaults"]["bottom_weight"] == DEFAULT_MAT_BOTTOM_WEIGHT


def test_the_settings_page_names_its_pairing_fields_in_the_contracts_words():
    """The server's token dialog tells a person to fill in fields by these names."""
    words = vocabulary()["pairing"]

    assert sorted(words) == ["server_address", "token"], "the contract names a pairing field this page does not ask for"
    assert dict(PAIRING_LABELS) == {"SERVER_URL": words["server_address"], "CLIENT_TOKEN": words["token"]}
