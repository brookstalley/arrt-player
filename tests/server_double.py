"""Arrt's Player surface, as the contract describes it, and nothing more.

Served on the contract's own routes (`routes()`), and **every answer is a row of
the contract's transport table** (`transport()`): the double decides which case
a request is, and the row gives the status and the kind of body, so the tests
that use it pin the Player to the contract rather than to Arrt's code, which a
Player in another repository will not have. A case the table does not name
cannot be answered (`answer` raises), and `answered` records each row served,
which `tests/test_transport_table.py` replays row by row.

What the double decides for itself is only what the table leaves to a server:
which client a token is, which walls and labels it holds, its body limit
(`BODY_LIMIT_BYTES`, the server's own and not the contract's), and the ETag rule
of `conditional_get` (`etag_for`). Both heartbeats are validated against their
schemas (the client heartbeat's rules a schema cannot state too, as the package
states them), so a Player writing a bad one fails here as it would against Arrt.
Masters are streamed in pieces, so a Player that stops reading is seen to stop.

The forced answers (`manifest_status`, `client_status` and the rest) are the
failure tests' levers: a forced status a row of that route names is answered as
that row, and any other (a `5xx`) bare, as the table's `unanswered` reads it.
"""

import hashlib
import io
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web
from arrt_contract import root, routes, semantic_errors, transport, validator
from PIL import Image

CONTRACT = root()
ROUTES = routes()
TRANSPORT = transport()
CLIENT_HEARTBEAT_SCHEMA = "schemas/client-heartbeat.v1.schema.json"
CLIENT_HEARTBEAT = validator(CLIENT_HEARTBEAT_SCHEMA)
WALL_HEARTBEAT = validator("schemas/heartbeat.v1.schema.json")

#: The largest request body the double reads, as Arrt's `BODY_LIMIT_BYTES` is:
#: a server's own limit, which the contract names no number for (`too_large`).
BODY_LIMIT_BYTES = 256 * 1024


def row(route: str, case: str) -> dict:
    """The transport table's row for one answer of one route, or KeyError naming what the table lacks."""
    for answer in TRANSPORT["routes"][route]["answers"]:
        if answer["case"] == case:
            return answer
    raise KeyError(f"the contract's transport table has no answer {case!r} for route {route!r}")


def contract_etag(body: bytes) -> str:
    """The tag `conditional_get` says this server sends: the quoted lowercase hex SHA-256 of the body."""
    return f'"{hashlib.sha256(body).hexdigest()}"'


#: How much of a master `serve_media` writes at a time.
MEDIA_CHUNK_BYTES = 64 * 1024

#: The client every test's Player is, unless it says otherwise.
TOKEN = "the-clients-token"
CLIENT_ID = "c-the-pi"

#: Where `publish`'s schedule starts: the instant the suite's test clock starts at.
SCHEDULE_STARTS = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


class ServerDouble:
    """One server, any number of clients and walls, every document held in memory."""

    def __init__(self, *, wall_id: str = "living-room", output: str = "frame") -> None:
        #: Each wall's manifest, or absent for a wall nothing has been published for.
        self.manifests: dict[str, dict] = {}
        self.media: dict[str, bytes] = {}
        #: token -> client id. A token mapped to a client this server does not
        #: hold is a valid token for a client with no walls.
        self.tokens: dict[str, str] = {TOKEN: CLIENT_ID}
        self.clients: dict[str, dict] = {
            CLIENT_ID: {"name": "The Pi in the hall", "walls": [{"wall_id": wall_id, "name": wall_id, "output": output}]}
        }
        #: Each wall heartbeat POSTed, as bytes, in order, and by wall.
        self.heartbeats: list[bytes] = []
        self.heartbeats_by_wall: dict[str, list[dict]] = {}
        #: Each client heartbeat accepted.
        self.client_heartbeats: list[dict] = []
        #: Forced answers, for the failure tests: every master, or one master by its hash.
        self.media_status: int | None = None
        self.media_status_by_sha: dict[str, int] = {}
        #: Masters whose request is answered by dropping the connection, as a
        #: server that has gone away mid-poll does.
        self.media_dropped: set[str] = set()
        #: A forced answer for every manifest request.
        self.manifest_status: int | None = None
        #: How many bytes of each master were written before the transfer ended,
        #: by hash, one entry per request.
        self.media_sent: dict[str, list[int]] = {}
        #: A forced answer for every wall heartbeat.
        self.heartbeat_status: int | None = None
        #: Every request that reached this server, routed or not, as (method, raw path).
        self.seen: list[tuple[str, str]] = []
        #: False serves manifests with no ETag, as a server or proxy may, and never 304.
        self.manifest_etags = True
        self.client_status: int | None = None
        self.client_body: bytes | None = None
        self.authorizations: list[str | None] = []
        self.requests: list[tuple[str, str]] = []
        self.echo_heartbeats_to: Path | None = None
        #: Each label's document, by label id.
        self.label_documents: dict[str, dict] = {}
        #: A forced answer for every label request, for the failure tests.
        self.label_status: int | None = None
        self._default_wall = wall_id
        #: Each manifest major requested, in order, as the integer asked for.
        self.majors_requested: list[int] = []
        #: The major a wall's document is served at, where it is not the one the
        #: document names: a server that serves the wrong document at a route.
        self.served_at: dict[str, int] = {}
        #: Where `publish`'s schedule starts. The suite's test clock by default;
        #: a test of the running process, on the system clock, moves it to now.
        self.schedule_starts = SCHEDULE_STARTS
        self._generation = itertools.count(1)
        #: Each answer given, in order, as (route, case): the transport table's
        #: row it was. A forced status no row names is recorded as its class.
        self.answered: list[tuple[str, str]] = []
        #: The ETag each document is served with. The contract's rule unless a
        #: test stands in a server that sent some other tag.
        self.etag_for = contract_etag
        #: The client each label output belongs to, mapped or not: an output its
        #: client holds that captions no wall answers 404, any other 403.
        self.label_owners: dict[str, str] = {}
        #: A forced answer for every client heartbeat.
        self.client_heartbeat_status: int | None = None

    # -- answering from the table ------------------------------------------------------

    def answer(
        self,
        route: str,
        case: str,
        *,
        sentence: str = "",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> web.Response:
        """The row's status, with the kind of body the row names; `sentence` is an error's, `body` a document's."""
        answer = row(route, case)
        self.answered.append((route, case))
        status, kind = answer["status"], answer["body"]
        if kind == "error":
            return web.json_response({"error": sentence}, status=status, headers=headers)
        if kind == "document":
            return web.Response(body=body, content_type="application/json", status=status, headers=headers)
        if kind == "none":
            return web.Response(status=status, headers=headers)
        raise ValueError(f"row {route}/{case} has a body of kind {kind!r}, which is not answered this way")

    def _forced(self, route: str, status: int) -> web.Response:
        """A failure test's forced status: the first row of the route that names it, else bare, as a status of its class."""
        for answer in TRANSPORT["routes"][route]["answers"]:
            if answer["status"] == status and answer["body"] in ("error", "none"):
                return self.answer(route, answer["case"], sentence=f"forced {status}")
        self.answered.append((route, f"{status // 100}xx"))
        return web.Response(status=status)

    def _conditional(self, route: str, request: web.Request, body: bytes) -> web.Response:
        """`ok` with the body's tag, or `unchanged` when If-None-Match names it (`conditional_get`)."""
        etag = self.etag_for(body)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("If-None-Match") == etag:
            return self.answer(route, "unchanged", headers=headers)
        return self.answer(route, "ok", body=body, headers=headers)

    # -- the app ---------------------------------------------------------------------

    def app(self) -> web.Application:
        @web.middleware
        async def record(request: web.Request, handler):
            self.seen.append((request.method, request.raw_path))
            return await handler(request)

        application = web.Application(middlewares=[record])
        for key, handler in (
            ("client", self.serve_client),
            ("client_heartbeat", self.receive_client_heartbeat),
            ("manifest_major", self.serve_manifest_major),
            ("media", self.serve_media),
            ("heartbeat", self.receive_heartbeat),
            ("label", self.serve_label),
        ):
            application.router.add_route(ROUTES[key]["method"], ROUTES[key]["path"], handler)
        return application

    def _note(self, request: web.Request) -> None:
        self.authorizations.append(request.headers.get("Authorization"))
        self.requests.append((request.method, request.path))

    def _admit(self, route: str, request: web.Request, wall_id: str | None) -> tuple[dict | None, web.Response | None]:
        """The client a token is, or the row refusing it: `no_valid_token`, or `not_this_clients_wall` on a wall's route."""
        header = request.headers.get("Authorization")
        client_id = self.tokens.get((header or "").removeprefix("Bearer "))
        if client_id is None:
            return None, self.answer(route, "no_valid_token", sentence="A valid client token is required.")
        client = self.clients.get(client_id, {"name": client_id, "walls": []})
        if wall_id is not None and wall_id not in {wall["wall_id"] for wall in client["walls"]}:
            return None, self.answer(route, "not_this_clients_wall", sentence="That wall is not this client's.")
        return {"client_id": client_id, **client}, None

    async def _read_body(self, route: str, request: web.Request) -> tuple[bytes | None, web.Response | None]:
        """A POST's body, or `too_large` past `BODY_LIMIT_BYTES`, refused before the token is read, as Arrt does."""
        body = bytearray()
        async for chunk in request.content.iter_chunked(MEDIA_CHUNK_BYTES):
            body += chunk
            if len(body) > BODY_LIMIT_BYTES:
                return None, self.answer(route, "too_large", sentence="The body is over this server's limit.")
        return bytes(body), None

    async def serve_client(self, request: web.Request) -> web.Response:
        self._note(request)
        client, refused = self._admit("client", request, None)
        if refused is not None:
            return refused
        if self.client_status is not None:
            return self._forced("client", self.client_status)
        return self._conditional("client", request, self.client_body or json.dumps(client).encode())

    async def receive_client_heartbeat(self, request: web.Request) -> web.Response:
        self._note(request)
        body, refused = await self._read_body("client_heartbeat", request)
        if refused is not None:
            return refused
        _client, refused = self._admit("client_heartbeat", request, None)
        if refused is not None:
            return refused
        if self.client_heartbeat_status is not None:
            return self._forced("client_heartbeat", self.client_heartbeat_status)
        try:
            document = json.loads(body)
        except ValueError:
            return self.answer("client_heartbeat", "not_json", sentence="The body is not JSON.")
        errors = [error.message for error in CLIENT_HEARTBEAT.iter_errors(document)]
        # The rules a schema cannot state are the package's, asked only of a report the schema accepts.
        errors = errors or semantic_errors(document, schema=CLIENT_HEARTBEAT_SCHEMA)
        if errors:
            return self.answer("client_heartbeat", "not_a_client_heartbeat", sentence="; ".join(errors))
        self.client_heartbeats.append(document)
        return self.answer("client_heartbeat", "accepted")

    async def serve_label(self, request: web.Request) -> web.Response:
        self._note(request)
        client, refused = self._admit("label", request, None)
        if refused is not None:
            return refused
        if self.label_status is not None:
            return self._forced("label", self.label_status)
        label_id = request.match_info["label_id"]
        document = self.label_documents.get(label_id)
        if self.label_owners.get(label_id) != client["client_id"] or document is None:
            return self.answer("label", "not_this_clients_label", sentence="That label output is not this client's.")
        if label_id not in {label["label_id"] for label in client.get("labels", [])}:
            return self.answer("label", "captions_no_wall", sentence="That label output captions no wall.")
        return self._conditional("label", request, json.dumps(document).encode())

    async def serve_manifest_major(self, request: web.Request) -> web.Response:
        """A wall's manifest at one major, as Arrt mounts it: 404 for a major it does not publish for that wall."""
        self._note(request)
        major = int(request.match_info["major"])
        self.majors_requested.append(major)
        wall_id = request.match_info["wall_id"]
        _client, refused = self._admit("manifest_major", request, wall_id)
        if refused is not None:
            return refused
        if self.manifest_status is not None:
            return self._forced("manifest_major", self.manifest_status)
        manifest = self.manifests.get(wall_id)
        if manifest is None:
            return self.answer("manifest_major", "nothing_hung", sentence="Nothing is hung on this wall.")
        if self.served_at.get(wall_id, manifest["schema"]["major"]) != major:
            return self.answer(
                "manifest_major", "major_not_published", sentence=f"No manifest of major {major} is published for this wall."
            )
        body = json.dumps(manifest).encode()
        if not self.manifest_etags:
            # A server or a proxy that strips the tag, which the contract's own server never does.
            return self.answer("manifest_major", "ok", body=body)
        return self._conditional("manifest_major", request, body)

    async def serve_media(self, request: web.Request) -> web.StreamResponse:
        self._note(request)
        _client, refused = self._admit("media", request, None)
        if refused is not None:
            return refused
        sha = request.match_info["sha256"]
        if sha in self.media_dropped:
            # No answer at all, which the table's `unanswered` reads as the server gone.
            request.transport.close()
            return web.Response(status=200)
        status = self.media_status_by_sha.get(sha, self.media_status)
        if status is not None:
            return self._forced("media", status)
        data = self.media.get(sha)
        if data is None:
            return self.answer("media", "not_held", sentence="No render with that hash is held.")
        self.answered.append(("media", "ok"))
        response = web.StreamResponse(
            status=row("media", "ok")["status"],
            headers={"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=31536000, immutable"},
        )
        response.content_length = len(data)
        await response.prepare(request)
        sent = self.media_sent.setdefault(sha, [])
        sent.append(0)
        try:
            for start in range(0, len(data), MEDIA_CHUNK_BYTES):
                await response.write(data[start : start + MEDIA_CHUNK_BYTES])
                sent[-1] = min(start + MEDIA_CHUNK_BYTES, len(data))
            await response.write_eof()
        except ConnectionError:
            # The Player stopped reading and closed the connection: what the
            # count up to here is for.
            pass
        return response

    async def receive_heartbeat(self, request: web.Request) -> web.Response:
        self._note(request)
        wall_id = request.match_info["wall_id"]
        body, refused = await self._read_body("heartbeat", request)
        if refused is not None:
            return refused
        _client, refused = self._admit("heartbeat", request, wall_id)
        if refused is not None:
            return refused
        if self.heartbeat_status is not None:
            return self._forced("heartbeat", self.heartbeat_status)
        try:
            document = json.loads(body)
        except ValueError:
            document = None
        if not isinstance(document, dict):
            return self.answer("heartbeat", "not_an_object", sentence="The body is not a JSON object.")
        errors = [error.message for error in WALL_HEARTBEAT.iter_errors(document)]
        if errors:
            return self.answer("heartbeat", "not_a_heartbeat", sentence="; ".join(errors))
        self.heartbeats.append(body)
        self.heartbeats_by_wall.setdefault(wall_id, []).append(document)
        if self.echo_heartbeats_to is not None:
            # What Arrt does with a POSTed heartbeat on a host it shares.
            self.echo_heartbeats_to.write_bytes(body)
        return self.answer("heartbeat", "accepted")

    # -- curating ----------------------------------------------------------------------

    @property
    def manifest(self) -> dict | None:
        """The default wall's manifest."""
        return self.manifests.get(self._default_wall)

    @manifest.setter
    def manifest(self, document: dict | None) -> None:
        if document is None:
            self.manifests.pop(self._default_wall, None)
        else:
            self.manifests[self._default_wall] = document

    def assign(self, wall_id: str, output: str, *, client_id: str = CLIENT_ID) -> None:
        """Place a wall on one of a client's outputs, taking it from wherever it was."""
        self.unassign(wall_id)
        self.clients.setdefault(client_id, {"name": client_id, "walls": []})["walls"].append(
            {"wall_id": wall_id, "name": wall_id, "output": output}
        )

    def map_label(self, label_id: str, output: str, wall_id: str, document: dict, *, client_id: str = CLIENT_ID) -> None:
        """Map one of a client's label outputs to a wall, serving `document` for it."""
        client = self.clients.setdefault(client_id, {"name": client_id, "walls": []})
        client["labels"] = [label for label in client.get("labels", []) if label["output"] != output]
        client["labels"].append({"label_id": label_id, "output": output, "wall_id": wall_id})
        self.label_documents[label_id] = document
        self.label_owners[label_id] = client_id

    def unmap_label(self, label_id: str) -> None:
        """Take a label output off its wall: still its client's, so it answers `captions_no_wall`."""
        for client in self.clients.values():
            client["labels"] = [label for label in client.get("labels", []) if label["label_id"] != label_id]

    def unassign(self, wall_id: str) -> None:
        for client in self.clients.values():
            client["walls"] = [wall for wall in client["walls"] if wall["wall_id"] != wall_id]

    def publish(
        self,
        *work_ids: str,
        wall_id: str | None = None,
        interval_seconds: int = 180,
        media: dict[str, bytes] | None = None,
    ) -> dict:
        """A major 2 feed for one wall: these works in order, each up for `interval_seconds`, from the test clock's start.

        The slots go round the works again, a hundred of them, and the rest of
        the one-day horizon is a gap. Each work's master is held behind its hash, a small real JPEG unless
        `media` gives its bytes. Every publish is a new document, as every
        republish of Arrt's is (its `generated_at` moves), so a Player asking
        with the last ETag is served it again.
        """
        works: dict[str, dict] = {}
        for work_id in work_ids:
            data = (media or {}).get(work_id, _a_master(work_id))
            sha = hashlib.sha256(data).hexdigest()
            self.media[sha] = data
            works[work_id] = {
                "media": {"url": f"/media/sha256-{sha}", "sha256": sha, "bytes": len(data), "content_type": "image/jpeg"},
                "mat_color": "#222222",
                "label": {"title": f"Title of {work_id}"},
            }
        starts = self.schedule_starts
        horizon_until = starts + timedelta(days=1)
        slots = []
        start = starts
        for position in range(100):
            if not work_ids or start >= horizon_until:
                break
            until = min(start + timedelta(seconds=interval_seconds), horizon_until)
            slots.append({"work_id": work_ids[position % len(work_ids)], "from": start.isoformat(), "until": until.isoformat()})
            start = until
        document = {
            "schema": {"major": 2, "minor": 0},
            "generated_at": (starts + timedelta(seconds=next(self._generation))).isoformat(),
            "playlist": {"id": "pl-1", "name": "A playlist"},
            "works": works,
            "schedule": {
                "horizon": {"from": starts.isoformat(), "until": horizon_until.isoformat()},
                "slots": slots,
            },
        }
        self.manifests[wall_id or self._default_wall] = document
        return document

    def publish_feed(
        self,
        slots: list[tuple[str, str, str]],
        *,
        staging: tuple[str, ...] = (),
        wall_id: str | None = None,
        media: dict[str, bytes] | None = None,
    ) -> dict:
        """A one-day major 2 feed for one wall, each work's master held behind its hash.

        `slots` are (work, from, until) as full RFC 3339 instants on 2026-06-21,
        the test clock's day. `media` overrides a work's bytes; a work whose
        bytes are given as b"" is published with a hash but nothing held for it.
        """
        works: dict[str, dict] = {}
        for work_id in dict.fromkeys([work for work, _, _ in slots] + list(staging)):
            data = (media or {}).get(work_id, _a_master(work_id))
            sha = hashlib.sha256(data or work_id.encode()).hexdigest()
            if data:
                self.media[sha] = data
            works[work_id] = {
                "media": {"url": f"/media/sha256-{sha}", "sha256": sha, "bytes": len(data), "content_type": "image/jpeg"},
                "mat_color": "#222222",
                "label": {"title": f"Title of {work_id}"},
            }
        document = {
            "schema": {"major": 2, "minor": 0},
            "generated_at": "2026-06-21T00:00:00+00:00",
            "playlist": {"id": "pl-1", "name": "A playlist"},
            "works": works,
            "schedule": {
                "horizon": {"from": "2026-06-21T00:00:00+00:00", "until": "2026-06-22T00:00:00+00:00"},
                "slots": [{"work_id": work, "from": start, "until": until} for work, start, until in slots],
            },
            "scene": None,
            "staging": list(staging),
        }
        self.manifests[wall_id or self._default_wall] = document
        return document


def _a_master(work_id: str) -> bytes:
    """A small real JPEG, its colour drawn from the work's id, so each work's hash differs and a wall can compose it."""
    shade = hashlib.sha256(work_id.encode()).digest()
    buffer = io.BytesIO()
    Image.new("RGB", (120, 80), (shade[0], shade[1], shade[2])).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()
