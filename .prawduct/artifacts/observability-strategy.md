---
artifact: observability-strategy
version: 1
depends_on:
  - artifact: project-state
  - artifact: architecture
last_validated: 2026-11-25
---

# Observability Strategy — Arrt Player

**What this covers.** What the Player says about itself, where it goes, and how
an operator finds a failure from it. It describes the code as it stood on
2026-11-25, after audit waves 2 and 3; every claim names the `file:symbol` it rests
on.

**Why it matters more here than usual.** The Player runs unattended and its only
output is a picture. A stalled Player looks exactly like a working one showing a
still image (`logs.py` module docstring; `project-state.yaml`, "Failure is silent
by construction"). So it reports two ways: structured lines in the journal, and
heartbeats the server shows.

**What it points at instead of restating.** Arrt owns the reader of every signal
the Player sends, and the rules both sides share:

- Arrt's `observability-strategy.md` § The Health Surface: what the server does
  with a heartbeat, and the panel that shows its age.
- Arrt's `observability-strategy.md` § A client, and a wall on its screen: the two
  levels of report, and the server's view of them.
- Arrt's `observability-strategy.md` § Sensitive Data in Logs: the rule that no
  secret reaches a log line, which binds both repos.
- Arrt's `observability-strategy.md` § Accepted detection latency, stated as a
  number, and § What Each Failure Looks Like: the failure-to-signal table.
- The heartbeat documents are `heartbeat.v1` and `client-heartbeat.v1` in the
  `arrt_contract` package; `player-contract.md` § The heartbeat (minors 2–4)
  is their specification.

<!-- OPTIONAL norm home. No norms are ratified here. "No secret ever reaches a
     log line" is in project-preferences.md § Enforcement. -->

## What You Get

1. **When a wall stops changing**, the server's view of the wall heartbeat says
   whether the set is reachable, what the screen is doing (`display_state`) and
   the last error; the journal says why, in one line per episode
   (`tv.unavailable`, `rotation.wall_unchanged`, `rotation.wall_not_ours`).
2. **When the Player stops entirely**, every heartbeat it sends stops ageing
   forward on the server. On an HDMI screen the kernel hands the screen back to
   the text console (Arrt's `clients.md` § The Player).
3. **When the server is unreachable**, the walls keep rotating and the journal
   says so once when it starts and once when it ends (`pull.unreachable` /
   `pull.reachable`, `client.unreachable` / `client.reachable`,
   `label.server_unreachable` / `label.server_reachable`).
4. **When a wall or label worker crashes**, the journal has an ERROR with the
   traceback (`client.worker_crashed`, `client.label_crashed`,
   `frame.crashed`, `screen.crashed`, `pull.crashed`) and the worker restarts.
5. **When the label is wrong or unreadable**, the journal says what was drawn and
   what had to give (`label.drawn`, `label.shrunk`, `label.name_wrapped`,
   `label.truncated`, `label.unusable`), and the client heartbeat says whether
   the panel is connected.
6. **When the television at the Frame's address is a different set**, the journal
   says so once, with the set a short hash names, how many bindings were set
   aside and how many of the new set's came back (`binding.set_changed`), and
   how many pictures the set lists that the Player's records do not name and it
   therefore leaves where they are (`tv.others_left_alone`, once per set in a
   process); when the set will not say which it is, once (`tv.unidentified`), and
   again if the wall goes on without it after fifteen minutes
   (`tv.unidentified_going_on`).
   **The journal names a set by `set-` and the first eight hex digits of a
   SHA-256 of its `device.duid`, never by the id** (`logs.py:known_as`, which
   every line that names a set goes through, the wall's and the client
   heartbeat's `client.identity_changed` alike), because a journal is what gets
   pasted into an issue in a public repository; the full id goes to the server, in
   the client heartbeat.

## Architecture

```
  Player process ──stderr──> systemd journal (SyslogIdentifier=display)
       │                       └─ journalctl -u display -o cat | jq
       ├─ wall loop ──file──> CACHE_DIR/<wall>/display-heartbeat-<wall>.json
       │                       └─ pull ──POST /walls/{id}/heartbeat──> server
       └─ supervisor ──POST /client/heartbeat──> server
                                                   └─ Arrt's health view
```

The journal is local to the Pi. Nothing is exported, collected or aggregated
(`logs.py` docstring: "there is no collector and no metrics store"). The
journal is bounded at 256 MB by `deploy/journald.conf.d/10-bound-the-journal.conf`.

## Signal Types

### Logging

**Shape.** One JSON object per line, written to stderr by a `StreamHandler` on
the root logger (`logs.configure`, `logs.JsonFormatter`). Every line has `time`
(UTC ISO 8601), `level`, `logger` (the module's `__name__`), `message`; then
`work_id` when one is bound; then every field the call site passed in `extra`;
then `exception` (the formatted traceback, one field) when there is one. A value
that will not serialise is rendered with `str` (`json.dumps(…, default=str)`).
`tests/test_logs.py` holds the one-object-per-line shape, the `work_id`
binding, nesting, and that `configure` twice does not duplicate lines.

**Every call site carries an `event`, as a field.** Held by
`tests/test_logs.py::TestEveryLineIsFindableByItsEvent`, which walks every
`log.debug/info/warning/error/exception` call in `src/arrt_player` with `ast` and
requires an `extra` dict literal holding an `event` key, and fails any module
that logs through a logger bound to a name other than `log`. An event name
written only into the message (`log.info("slideshow.cache.downloaded item=%s",
…)`) is invisible to `jq 'select(.event == …)'`; the slideshow's source, cache
and resolvers were written that way until 2026-11-11, which is why the walk is a
test and not a claim.

**A test that searches the log for what must not be there reads
`conftest.journal(caplog)`**, the records through `logs.JsonFormatter`, never
`caplog.text`, which is the message alone: a secret or an account name in an
`extra` field would pass a `caplog.text` search. `conftest.events(caplog, name)`
finds records by the `event` field.

**Levels, as the code uses them** (derived from the inventory below, not
prescribed anywhere):

| Level | Meaning in this product |
|---|---|
| ERROR | Will not fix itself: a deployment value, a refusal by the server, a document the Player will not read, an assignment it cannot place, a crash (with traceback) |
| WARNING | A degraded condition that may pass, or a device the operator must look at: server or set unreachable, a draw refused, a label set below the legibility floor |
| INFO | A state change or the end of an episode: started, adopted, selected, drawn, recovered |
| DEBUG | One event, `notify.absent`, which is the expected case on a development machine and would never be emitted: `__main__.main` calls `logs.configure()` at INFO. It is declared so the walk sees every name |

**Episodes, not repetition.** A condition that persists is logged when it starts
and when it ends, through `episodes.ReportOnce` (`begin()` / `end()`). This is
why most WARNING/ERROR events below have a paired INFO. The exceptions, logged
on every occurrence because each is about a different work: `label.shrunk`,
`label.name_wrapped`, `label.truncated`, `rotation.selected`, `label.drawn`.

**The event inventory.** Derived, not remembered: an `ast` walk of every log call
in `src/arrt_player` (event, level, enclosing function), plus the places an
event name is built rather than written: `client.Supervisor._keep_running`
(`f"{event}_crashed"`, `f"{event}_ended"`, `f"{event}_retire_failed"`, with
`event` = `client.worker` or `client.label`), `wall.Wall.run` (`f"{name}.started/stopped/crashed"`, with
`name` = `FrameDisplay.journal_name` `frame` or `ScreenDisplay.journal_name`
`screen`), and `programmes/schedule.py:Schedule._collect` (a variable set to one
of four names). A grep for `"event": "` alone misses the built ones.

| Events | Level | Emitted by | Says |
|---|---|---|---|
| **Process** | | | |
| `daemon.misconfigured` | ERROR | `__main__.main` | A `.env` value is missing or wrong; exit 2, also on stderr |
| `files.drawing` | WARNING | `__main__._run` | This Player draws its walls and labels to files in `FILE_OUTPUT_DIR` and drives no screen, television or panel (`directory`, `screen`, `label`); once at start, because on a wall it would be a Player showing nothing |
| `notify.absent` | DEBUG | `notify.Notifier` | `NOTIFY_SOCKET` is not set, so nothing is told to systemd (a Mac, a test, a terminal); never emitted at the INFO root |
| `notify.unusable` | WARNING | `notify.Notifier` | `NOTIFY_SOCKET` names no `AF_UNIX` socket this speaks (`notify_socket`); under the unit systemd will restart a Player that cannot pet it |
| `notify.unsent` / `notify.sent` | WARNING / INFO | `notify.Notifier.send` | systemd's socket refused a message (`notification`), then took one again; once per episode |
| `client.started` / `client.stopped` | INFO | `client.Supervisor.run` | Up, with server URL, cache dir, Frame and panel settings (`ClientSettings.startup_lines`); down |
| **Supervisor** | | | |
| `client.wall_started` / `client.wall_stopped` | INFO | `Supervisor._start`, `_stop` | A wall assigned to an output, or taken away or moved; `retired` is true when the document no longer places the wall on that output (its screen is then drawn black), false when the Player is stopping or the output is gone |
| `client.wall_unplaceable` | ERROR | `Supervisor._unplaceable` | Assigned to an output this client lacks or that another wall holds, or a wall id that is not a plain name; once per (wall, output) |
| `client.label_started` / `client.label_stopped` | INFO | `Supervisor._start_label`, `_stop_label` | A label output mapped, remapped or unmapped |
| `client.wall_cache_stamped` / `client.wall_cache_returned` | INFO | `WallCaches._stamp`, `_returned` | A directory under `CACHE_DIR` is a wall's and no client document read places the wall here, so it is stamped and will be removed in a week (`wall_id`, `path`); or the wall is placed here again and its stamp goes |
| `client.wall_cache_removed` | INFO | `WallCaches._remove` | A departed wall's directory was removed a week after it left (`wall_id`, `path`, `left_at`) |
| `client.wall_cache_unsweepable` / `client.wall_cache_sweepable` | WARNING / INFO | `WallCaches._say_unsweepable`, `sweep`; `Supervisor._sweep_walls` (ERROR with traceback if a sweep raised) | A directory could not be read, stamped or removed, and stays until it can (`path`); then the sweep ran clean |
| `client.cache_entry_unknown` | INFO | `WallCaches._say_unknown` | A name a wall id could take under `CACHE_DIR` that is not a wall's directory this client recognises; left as it is, said once per name (`path`) |
| `client.label_unplaceable` | ERROR | `Supervisor._label_unplaceable` | Mapped to a label output this client lacks, or a second label on one output |
| `client.worker_crashed` / `client.label_crashed` | ERROR (traceback) | `Supervisor._keep_running` | A worker raised; restarting after the backoff |
| `client.worker_ended` / `client.label_ended` | ERROR | `Supervisor._keep_running` | A worker returned without being asked to |
| `client.worker_retire_failed` / `client.label_retire_failed` | ERROR (traceback) | `Supervisor._keep_running` | A wall or label retired while its worker waited to restart was called once more to leave its output, and raised |
| `client.identity_unreadable` / `client.identity_read` | WARNING / INFO | `client.FrameIdentity.refresh` | The Frame's device id could not be read, then could; the last one read is reported meanwhile. The read is one REST GET a minute, and these are said once per episode, not once a minute |
| `client.identity_changed` | WARNING | `client.FrameIdentity.refresh` | The set answering at the Frame's address says another `device.duid` than the last one read (`output`, `set_now`, `set_was`, each a short hash, never the id): it is reported as the new one at once, said once per change |
| **Client routes** (`pull.ClientPull`) | | | |
| `client.unreachable` / `client.reachable` | WARNING / INFO | `ClientPull._report_unreachable`, `fetch` | `GET /client` got no answer, a timeout or a 5xx |
| `client.refused` / `client.accepted` | ERROR / INFO | `ClientPull.fetch` | The server refused the client (with `status`) |
| `client.document_refused` / `client.document_readable` | ERROR / INFO | `ClientPull.fetch` | A client document this reader will not act on |
| `client.cache_unwritable` / `client.cache_writable` | ERROR / INFO | `ClientPull.fetch` | `.client.json` could not be written; the document is still followed |
| `client.cache_unreadable` | WARNING | `ClientPull.cached` | The cached client document could not be read at start |
| `client.heartbeat_refused` / `client.heartbeat_ok` | WARNING / INFO | `ClientPull.report` | The server did not take the client heartbeat (`status`): said once per episode. A refusal in the server's error form is held, sent again only when the report changes or after 300 s; any other answer (a `401`, `408`, `429`, a `5xx`, a body not in the server's form) is sent again at the next poll |
| **A wall's pull** (`pull.Pull`) | | | |
| `pull.started` | INFO | `Pull.run` | The wall's pull began, with `wall_id`, server, cache dir |
| `pull.unreachable` / `pull.reachable` | WARNING / INFO | `Pull._report_unreachable`, `_report_reachable` | The feed route got no answer, a timeout or a 5xx |
| `pull.refused` / `pull.accepted` | ERROR / INFO | `Pull._report_refused`, `_report_accepted` | 401 (token), 403 (not this client's wall), 404 at every major, or another status; once per status |
| `pull.manifest_refused` / `pull.manifest_readable` | ERROR / INFO | `Pull._pull_manifest` | A feed the parser refuses, one that runs past 16 MiB, or one that stages a work twice; the cached one stays, and the next poll asks again |
| `pull.media_failing` / `pull.media_ok` | WARNING / INFO | `Pull._hold_or_adopt`, `_media_settled` | A master could not be fetched in a way that may clear; the feed already cached is kept for up to `hold_seconds` (600) |
| `pull.hold_ended` | WARNING | `Pull._hold_or_adopt` | The hold ran out: the feed is cached without the masters still missing (`work_ids`) and with no ETag, and they are asked for again every poll; said once per episode |
| `pull.work_skipped` | WARNING | `Pull._skip_once` | A master 404s, fails its hash, runs past the bytes its feed declares, or has no size declared; that work is skipped (once per hash) |
| `pull.adopted` | INFO | `Pull._cache_feed` | A new feed cached, with `works` and `media_held` |
| `pull.cache_unwritable` / `pull.cache_writable` | ERROR / INFO | `Pull._report_unwritable`, `_cache_feed` | The cache refused a write while adopting a feed (`cache_dir`, `errno`): the feed already cached stays, no ETag is stored for the new one, and each poll tries again; then it took one |
| `pull.heartbeat_refused` / `pull.heartbeat_ok` | WARNING / INFO | `Pull._heard` | The server refused the wall heartbeat with a 4xx (`status`); it is sent again only when it changes |
| `pull.heartbeat_failed` | WARNING | `Pull._heard` | The server answered the wall heartbeat with a 5xx (`status`); it is sent again on the next poll. Ended by `pull.heartbeat_ok` |
| `pull.crashed` | ERROR (traceback) | `__main__._pull_ended` | The pull raised; its wall's worker is ended to restart both |
| **A wall's loop** | | | |
| `frame.started` / `frame.stopped` / `frame.crashed` | INFO / INFO / ERROR | `wall.Wall.run` (Frame) | A Frame wall loop started (with its settings), stopped, or raised. Filed under `daemon.*` until 2026-11-19 |
| `screen.started` / `screen.stopped` / `screen.crashed` | INFO / INFO / ERROR | `wall.Wall.run` (HDMI) | The same for an HDMI wall |
| `daemon.state_too_new` | ERROR | `__main__.run_frame_wall` | The Frame store is from a newer Player; this wall is parked |
| `store.busy` / `store.failed` | WARNING / ERROR | `FrameDisplay._the_store_failed` | A pass ended on the Frame's store, not the wall: a lock that may clear (`store.busy`), or a full or read-only disk, an I/O error or a damaged file (`store.failed`), with `operation` and SQLite's own `fault` name; once per episode each, and the heartbeat's `last_error` carries it |
| `store.recovered` | INFO | `FrameDisplay.answering` | A pass completed, which ends either of the two |
| `store.feed_time_unreadable` | WARNING | `DisplayState._saved_feed_seconds` | The stored count of the wall's running time with a feed does not read as a number and counts from nought, which can only delay a retirement |
| `display.state` | INFO | `wall.DisplayRecord.moved_to` | The screen's state changed (`showing_art`, `in_use`, `dark`, `no_screen`, `unreachable`) |
| `heartbeat.failed` / `heartbeat.recovered` | WARNING / INFO | `wall.Wall._beat` | The heartbeat file could not be written |
| `manifest.adopted` | INFO | `manifest.Watcher.poll` | The loop adopted a cached feed (theme, works, scene, schema) |
| `manifest.absent`, `manifest.statable` / `manifest.unstatable`, `manifest.unreadable` | INFO, INFO / WARNING, WARNING | `Watcher.poll` | No cached feed yet; the file could not be stat'd or read |
| `manifest.not_text`, `manifest.refused`, `manifest.version_refused` | ERROR | `Watcher.poll` | A cached feed that is not UTF-8, does not parse, or has a major this Player does not read |
| `schedule.media_missing` | WARNING | `Schedule._ready` | A work's master is not in the cache yet |
| `schedule.uncomposable`, `schedule.compose_read_failed`, `schedule.compose_write_failed`, `schedule.compose_failed` | WARNING | `Schedule._collect` | A work could not be composed for this display; `compose_read_failed` is a master that could not be read for a reason that may clear (an errno), tried again |
| `schedule.composed_damaged` | WARNING | `Schedule._intact` | A composed picture found empty, cut short or not a JPEG was removed, so the work is composed again |
| `schedule.composed_removed`, `schedule.tidy_failed` / `schedule.tidy_recovered` | INFO, WARNING / INFO | `Schedule._remove_unwanted`, `_say_tidy_failed` | Old compositions removed, or could not be |
| `compose.done` | INFO | `compose.compose` | A picture composed (`composed_path`) |
| **The Frame** (`displays/frame.py:FrameDisplay`, `tv/samsung.py:SamsungTv`) | | | |
| `tv.unavailable` / `tv.recovered` | WARNING / INFO | `FrameDisplay.went_away`, `answering` | The set cannot be reached; the wall holds |
| `rotation.wall_not_ours` / `rotation.wall_returned` | INFO | `FrameDisplay._learned_showing_art` | The set is not in art mode, so nothing is sent; then it is again, by whatever says so (the gate's read, a read after the set's announcement, a picture announced or a selection that took). Once per episode each |
| `tv.writes_held` / `tv.writes_resumed` | INFO | `FrameDisplay._hold_the_writes`; `_writes`, a `frame._WaitedEpisode`, at `clear` | The seam refused an upload, removal, brightness change or slideshow switch-off (`TvNotOurs`, with the `verb`) because the set is lit and not showing art, or its power cannot be read; said once per hold, and the hold's end once: a write going through, the set read showing art, or the set announcing a change of mode |
| `display.power_unreadable` / `display.power_readable` | INFO | `FrameDisplay._screen_is_not_ours` | `PowerState` could not be read; `in_use` is reported |
| `rotation.selected` | INFO | `FrameDisplay.show` | A work is on the wall, confirmed by the set (`tv_content_id`, `theme_id`) |
| `rotation.wall_unchanged` / `rotation.wall_recovered` | WARNING / INFO | `FrameDisplay._report_wall_unchanged`; `_wall`, a `frame._WaitedEpisode`, at `clear` | The set accepted a selection and displayed nothing (`art_mode`); said once, and its end once, whenever the wall's wait is cleared: a selection that took, or the set announcing a change of mode |
| `binding.uploaded`, `binding.upload_failed` | INFO, WARNING | `FrameDisplay._upload` | A work uploaded to the set, or not |
| `binding.orphaned` | WARNING | `FrameDisplay._rebind_or_reraise`, `_reconcile_with_the_set` | The set no longer lists a content id the store has |
| `tv.unidentified` / `tv.identified` | WARNING / INFO | `frame._IdentityReads.unanswered`, `answered` | **The set would not say which set it is** (a REST read of its device description failed or carried no `device.duid`): the wall uses none of its bindings on it and asks again on its own ladder, said once; then it said. Ends the pass, so it is not an outage (the heartbeat's `television_reachable` is `null`, not `false`) |
| `tv.unidentified_going_on` | WARNING | `FrameDisplay._become` (entering `GoingOn`) | The set has not said for `IDENTITY_PATIENCE_SECONDS` (15 minutes), so the wall shows art on it from an empty store held in memory, uploading the work it shows and then the feed's other works one a pass, and **removing nothing**, until it says (then `tv.identified`, and a reconciliation takes those uploads off a set they were sent toward or that spoke on their connection, and off no other). Said again after an outage, which starts the 15 minutes again |
| `binding.set_adopted` | INFO | `FrameDisplay._met` | The store had recorded no set (written before schema 4), so the first one read is taken as the bindings' own (`set_now`), and its bindings as uploads to it; nothing is removed until one is recorded |
| `binding.set_changed` | WARNING | `FrameDisplay._met` | **The television at this address is not the one the bindings were made on**: they were set aside under that set (`set_was`, `set_now`, `set_aside`, `restored`), the wall starts on this set as on a first run, and the other set keeps the Player's pictures until it is met again |
| `binding.retired` | INFO | `FrameDisplay._retire` | Works no adopted feed has named for 14 days of the wall running were retired (`retired`, `removed` images, `kept_by_the_set`) |
| `binding.retirement_unconfirmed` | WARNING | `FrameDisplay._remove_orphans` | Whether the images of works due to retire were removed could not be established (`requested`); asked again next pass |
| `tv.orphans_removed`, `tv.orphan_removal_unconfirmed` | INFO, WARNING | `FrameDisplay._remove_orphans` | Images this Player put on the set, which its upload records name and no binding wants, removed (`removed`, `surviving`), or the outcome unknown (`requested`) |
| `tv.others_left_alone` | INFO | `FrameDisplay._say_what_is_left_alone` | The set lists pictures the Player's upload records do not name (`left_alone`, `set_now`): the household's, an older Player's, or this Player's own that its records cannot match. They are left where they are; said once per set in this process, at the first reconciliation that finds any |
| `tv.removal_incomplete` | WARNING | `SamsungTv.remove` | Some images survived a removal |
| `tv.upload_misreported` | WARNING | `SamsungTv.upload` | The library's upload reply disagreed with what the set holds |
| `tv.selection_unreadable` | WARNING | `SamsungTv._on_image_selected` | An `image_selected` announcement could not be read |
| `tv.selection_observer_failed` | ERROR (traceback) | `SamsungTv._tell_observers` | An observer of selections raised; the others still ran |
| `tv.native_slideshow_disabled` | INFO | `FrameDisplay._connected` | The set's own slideshow was switched off (once per store) |
| `tv.token_file_open` | WARNING | `SamsungTv._keep_the_token_private` | The token file could not be set to 0600 (`tv_token_file`, never its content); anyone who can read it can drive the art channel |
| `tv.close_failed` | WARNING | `SamsungTv._quietly_close` | Closing the connection raised; the set may hold the abandoned channel and refuse the next connection for minutes |
| `brightness.set` | INFO | `FrameDisplay._apply_brightness` | Brightness set from the sun's angle |
| **HDMI** (`kms.py:KmsOutput`, `displays/screen.py:ScreenDisplay`, `displays/screen_brightness.py:ScreenBrightness`; in slideshow mode the `screen.brightness_*` events come from the slideshow's screen, with `wall_id` `slideshow`) | | | |
| `screen.absent` / `screen.returned` | WARNING / INFO | `KmsOutput._gone`, `_draw` | No screen on the connector, then one (drawn at once) |
| `screen.draw_failed` / `screen.draw_recovered` | WARNING / INFO | `ScreenDisplay._put_up` | The output refused a picture; the display asks for it again on each following pass until the output takes it |
| `screen.blanked` / `screen.blank_failed` | INFO / WARNING | `displays/screen.go_dark` | A wall that left an output (`wall_id`, `output`) had its screen drawn black, or the device refused and the screen keeps its last picture |
| `screen.refresh_failed` / `screen.refresh_recovered` | WARNING / INFO | `ScreenDisplay._refresh` | The per-poll redraw failed |
| `rotation.selected` | INFO | `ScreenDisplay._put_up` | A work drawn (`picture_path`), also when a refused picture is drawn on a later pass |
| `screen.brightness_set` | INFO, or WARNING when `read_back` differs | `ScreenBrightness._said_set` | The screen's brightness set from the sun (`output`, `brightness`, `read_back`, `solar_angle`); written only when the value changes |
| `screen.brightness_unsupported` | INFO | `ScreenBrightness._said_unsupported` | A screen with no brightness control, or a scale of 0 (`reason`); said once, and the screen left alone until it is replaced |
| `screen.brightness_no_answer` / `screen.brightness_answering` | INFO / INFO | `ScreenBrightness._no_answer`, `_answered` | Nothing acknowledged the DDC/CI address (no DDC/CI, or asleep); said once, asked again each interval, and said again when it answers |
| `screen.bus_close_failed` | WARNING | `ScreenBrightness._drop_the_bus` | The DDC/CI device would not close; a descriptor may be leaking |
| `screen.brightness_failed` / `screen.brightness_recovered` | WARNING / INFO | `ScreenBrightness._failed`, `_answered` | This host could not reach the bus (`error`: a missing `i2c` group, no `i2c-dev`, a garbled reply); said once per episode, retried each interval |
| **The label** (`__main__`, `label_renderer.py`, `pull.LabelPull`, `panel/epaper.py`) | | | |
| `panel.type_scale` | INFO | `__main__.label_surface` | The derived type size, floor and border, once at start |
| `panel.unavailable` | WARNING | `__main__.label_panel` | A configured panel will not open, has no text stack, or has no viewing distance; walls unaffected |
| `panel.size_disagrees` | WARNING | `EpaperSurface._warn_if_the_panel_disagrees_about_its_size` | The driver reports a size other than `.env`'s |
| `panel.close_failed` | WARNING | `EpaperSurface.close` | The panel's power-down raised (`output`, the panel's name, when the surface was built for one) |
| `label.renderer_started` | INFO | `LabelRenderer.run` | A renderer captions a wall (`label_id`, `output`, `wall_id`) |
| `label.renderer_retired` | INFO | `LabelRenderer.run` | A label output was unmapped and its renderer, called once more already stopped, leaves the panel blank without starting or fetching anything |
| `label.no_panel` | ERROR | `__main__.run_label` | A label is mapped to an output this client holds no panel for (`label_id`, `output`); said once and not drawn, never on another panel |
| `label.cache_forgotten` | INFO | `LabelCacheSweep.__call__` | A cached label document no mapped label reads was removed (`path`) |
| `label.cache_unsweepable` / `label.cache_sweepable` | WARNING / INFO | `LabelCacheSweep.__call__` | The label cache could not be swept, once per episode; then it could |
| `label.server_unreachable` / `label.server_reachable` | WARNING / INFO | `LabelPull` | The label route got no answer, a timeout or a 5xx |
| `label.id_unusable` | ERROR | `LabelPull.fetch` | The server named a label that is not a plain path component; this client never asks for it and its panel stays as it is |
| `label.cache_unreadable` | WARNING | `LabelPull.cached` | The cached label document could not be read; the label starts blank until the server answers |
| `label.cache_unwritable` / `label.cache_writable` | ERROR / INFO | `LabelPull` | The label document could not be cached or its stamp moved; a restart while the server is down starts blank. Then it could |
| `label.cache_seeded` / `label.cache_other_wall` | INFO | `LabelRenderer._seed` | The renderer starts from the document cached when the server last answered (with the instant); or the cached document is for another wall and is not used |
| `label.refused` / `label.accepted` | ERROR / INFO | `LabelPull.fetch` | The server refused the label (with `status`) |
| `label.document_refused` / `label.document_readable` | ERROR / INFO | `LabelPull.fetch` | A label document this reader will not use |
| `label.drawn` | INFO | `LabelPanel.draw` | The only positive label line: the panel captions a work |
| `label.blanked`, `label.card`, `label.absent` | INFO | `LabelPanel.draw` | Blank on purpose (with `display_state`), the wall's card, or a work with no label text |
| `label.close_abandoned` | WARNING | `LabelPanel.close` | A draw was still in the driver after the label budget when the panel was to be closed; the panel is left open (`output`) |
| `label.failed` / `label.recovered` | WARNING / INFO | `LabelPanel._would_not_take_it`, `draw` | A drawing did not reach the panel (refused, raised, still drawing, or over the 15 s budget) |
| `label.unusable` | WARNING | `LabelPanel.draw` | The geometry leaves no usable area; once per episode |
| `label.shrunk` | WARNING | `LabelPanel.draw` | Identifying facts set below the legibility floor (`floor_px`, `smallest_px`) |
| `label.name_wrapped` | WARNING | `LabelPanel.draw` | The line breaker split a name across rows |
| `label.truncated` | INFO | `LabelPanel.draw` | Lines dropped for room (`dropped`) |
| **The settings page** (`__main__`, `settings_ui/server.py`) | | | |
| `daemon.setup_mode` | WARNING | `__main__.main` | The chosen mode is missing settings (`mode`, `missing`), so only the settings page runs |
| `settings_page.listening` | INFO | `__main__.run_settings_page` | The page is up (bind, port, `setup_mode`); the setup link is printed to stdout, which the unit sends to the journal, and is not in this event |
| `settings_page.unavailable` | ERROR | `__main__.run_settings_page` | The page cannot listen (address in use); the walls run on |
| `settings_page.password_set`, `settings_page.password_changed` | INFO | `server._setup`, `_change_password` | The first password set from the setup link; a password changed |
| `settings_page.login_refused`, `settings_page.password_refused` | WARNING | `server._login`, `_change_password` | A wrong password at sign-in; a wrong current password when changing it. Each carries `remote`, the source address the throttle charges |
| `settings_page.host_refused` | WARNING | `server._known_hosts`, `RefusedHosts` | Requests for a name the page does not answer to were refused with a 421 before any page. Said when a name is first refused, then at most once in 10 minutes while it keeps arriving, and once more, at the next refusal of any name, when it has been quiet 10 minutes with refusals unsaid (no timer: if refusals stop, that last count is not written); `refused` is how many refusals the line accounts for. Past 32 names the rest are counted together, with `host` null (`host`, `refused`, `setting`: the `.env` key that lists more) |
| `settings_page.saved` | INFO | `server._commit` | Settings saved (`changed`: the keys, never the values; `restarting`: whether the Player restarts onto them, which it does only in setup mode or when what it runs on changes; `pairing_forgotten`: whether a new Frame address removed the pairing token) |
| `settings_page.pairing_forgotten` / `settings_page.pairing_not_forgotten` | INFO / ERROR | `server._forget_pairing` | The page removed the television's pairing token because the Frame's address changed (`tv_token_file`, the path); or could not, and saved nothing |
| `settings_page.write_failed` | ERROR | `server._wrote` | The settings file could not be written (full disk, permission) |
| **Slideshow mode: the programme** (`__main__.run_slideshow`, `programmes/slideshow.py:Slideshow`) | | | |
| `slideshow.starting` | INFO | `run_slideshow` | Slideshow mode started, with `SlideshowSettings.startup_lines` (never the cookie) |
| `slideshow.renderer_unavailable` | ERROR | `run_slideshow` | mpv is missing, or the account is not in `render` (`mpv.renderer_problems`, asked once at start); said on the page too, and the slideshow plays on |
| `slideshow.saved_listing` | INFO | `Slideshow.run` | Playing the listing saved by the last run until Reddit answers (`items`) |
| `slideshow.saved_unreadable` | WARNING | `load_listing` | The saved listing could not be read; starting without it |
| `slideshow.listed` | INFO | `Slideshow._take` | A listing taken (`items`), and the files of posts no longer listed deleted |
| `slideshow.listed_nothing` | WARNING | `Slideshow._take` | Reddit answered with nothing showable (`unresolved` by domain); the cache is kept and plays on |
| `slideshow.refresh_failed` | WARNING | `Slideshow._take` | Reddit gave no listing (`outcome`); playing on from the cache and asking again later |
| `slideshow.save_failed` | WARNING | `Slideshow._take` | The listing could not be saved for the next start |
| `slideshow.filled` | INFO | `Slideshow._report_fill` | A fill ended: files downloaded and failed, by tier |
| `slideshow.nothing_ready` | INFO | `Slideshow._show_next` | Nothing is cached to show yet; once per episode |
| `slideshow.shown` | INFO | `Slideshow._show_next` | An item is on the screen (`item`, dwell) |
| `slideshow.not_shown` | WARNING | `Slideshow._not_shown` | An item could not be shown (`item`, `reason`); the next one is tried |
| **Slideshow mode: the source and the cache** (`sources/reddit.py`, `sources/cache.py:MediaCache`, `sources/resolvers/`) | | | |
| `slideshow.reddit.listed` | INFO | `reddit._logged` | Reddit answered with a listing (`items`, `unresolved`: posts with nothing to show, by domain) |
| `slideshow.reddit.not_listed` | WARNING | `reddit._logged` | It did not; `outcome` is `refused`, `cookie_rejected`, `rate_limited` or `unreachable`, with a `detail` |
| `slideshow.reddit.post_unreadable` | WARNING | `reddit.parse_listing` | One post in a shape this does not read (`post`); only that post is lost |
| `slideshow.resolver.fallback` | WARNING | `resolvers._fell_back` | A host resolver gave nothing usable (`resolver`, `item`, `reason`); the item keeps Reddit's copy, or has nothing |
| `slideshow.resolver.redgifs.token`, `…token_refused` | INFO | `RedgifsResolver._current_token`, `resolve` | A temporary token taken (`valid_for_s`, never the token); one refused and renewed once |
| `slideshow.resolver.redgifs.gone`, `…lookup_failed` | INFO, WARNING | `RedgifsResolver.resolve` | The clip is gone (404); the lookup failed otherwise (`status`) |
| `slideshow.cache.downloaded`, `slideshow.cache.fell_back` | INFO, WARNING | `MediaCache._fetch_one` | A file kept (`item`, `tier`, `bytes`); kept from a lesser tier after a better one failed (`reasons`) |
| `slideshow.cache.download_failed` | WARNING | `MediaCache._fail` | Every place the item's media may be failed (`reason`); it waits before being tried again |
| `slideshow.cache.cap_reached`, `slideshow.cache.network_down` | WARNING | `MediaCache.fill` | The fill stopped: the 2 GB cap (`used_bytes`, `cap_bytes`), or enough unreachable items in a row to say the network is down |
| `slideshow.cache.evicted`, `slideshow.cache.partial_cleared` | INFO | `MediaCache.evict`, `__init__` | Files of posts no longer listed removed (`count`); an unfinished download from a previous run removed |
| **mpv** (`mpv.py:MpvOutput`, the slideshow's screen) | | | |
| `mpv.started` | INFO | `MpvOutput._start` | mpv is up on an output (`output`, `pid`) |
| `mpv.stale` | WARNING | `stop_stale` | An mpv a previous run left behind was stopped (`pid`); checked against its socket path, so a reused pid is never killed |
| `mpv.refused` | ERROR | `MpvOutput._load` | A path mpv must not be given (not a regular file under the media cache); never sent |
| `mpv.file_failed` | WARNING | `MpvOutput._failed` | mpv could not play one file (`reason`) |
| `mpv.not_shown` / `mpv.recovered` | WARNING / INFO | `MpvOutput._not_shown`, `_put_up` | mpv is not drawing on the output (`reason`), then is; once per episode |

**Destinations and retention.** stderr, captured by systemd
(`deploy/display.service`: `StandardOutput=journal`, `StandardError=journal`,
`SyslogIdentifier=display`). Retention is journald's, capped at 256 MB system and
runtime (`deploy/journald.conf.d/10-bound-the-journal.conf`). On a dev machine
the same JSON goes to the terminal.

### Heartbeats

These are the Player's state signal, sent to the server. Arrt's health view is
their reader; Arrt § The Health Surface owns what it does with them.

**The wall heartbeat**, one per wall, built by `wall.Wall._beat` as
`heartbeat.Health` and written by `heartbeat.write` to
`CACHE_DIR/<wall_id>/display-heartbeat-<wall_id>.json`. It is written once per
`heartbeat.INTERVAL_SECONDS` (60 s), and at once when the display state or the
capabilities change. It is written on every path through `Wall.tick`, including
a pass with no feed and a pass where the display was unreachable
(`tests/test_heartbeat_wiring.py`). `pull.Pull._heartbeats`, a loop of its own beside
the manifest poll, POSTs it (`_forward_heartbeat`) every poll interval when the
file and its `reported_at` have changed, while the last manifest poll reached
the server, so a manifest backoff never delays it.

| Key (`Health.document`) | What problem it shows |
|---|---|
| `schema` | `1.4` (`heartbeat.SCHEMA_MAJOR`, `SCHEMA_MINOR`) |
| `reported_at` | Its age is the liveness signal: a heartbeat that stops advancing is a stopped loop or a stopped pull |
| `manifest_schema`, `theme_id` | A wall sitting on an old feed (the pull's refusals are not otherwise in the heartbeat) |
| `current_work_id`, `scene_id` | What this wall last confirmed showing |
| `announced_content_id` | Frame only: what the set last announced, the set's own word |
| `television_reachable` | Frame only: `false` is network or pairing; `null` when the pass ended on the Frame's store or for want of the set's identity, which says nothing about whether the set answers (`FrameDisplay.heartbeat_fields`) |
| `television_showing_art` | Frame only: `false` with the set awake means somebody is watching, and the Player is leaving it alone |
| `display_state` | `{state, work_id, since}`. Frame: `showing_art`, `in_use`, `dark`, `unreachable`. HDMI: `showing_art`, `dark`, `no_screen` (`wall.DisplayRecord`) |
| `capabilities` | Backend, label modes, `manifest_majors`, and `screen` when the size is known |
| `last_error` | The latest failure the display recorded, not cleared by a good pass. **Frame: set only by `FrameDisplay.went_away`** (set unreachable, a pass that ended on the store, or a set that would not say which it is). **HDMI: set by `ScreenDisplay.show` and `_refresh`**. Pull failures and worker crashes never reach it |
| `has_label_surface`, `label_surface_working` | Always `false` / `null` now: the panel is the client's (`FrameDisplay.heartbeat_fields` and `Health`'s defaults) |

**The client heartbeat**, one per client, built by `client.client_heartbeat` and
sent by `Supervisor._report_outputs` on each client poll when the outputs changed
or 60 s have passed. It carries `reported_at`, `outputs` (each `name`, `kind`,
`connected`, `screen`, and `identity` for a Frame once read), when the client
has a panel, `label_outputs` (each `name`, `kind`, `connected`, `size`), and, when
the settings page can be reached from the server's network, `settings_url` (absent,
never null, for a page bound to loopback; a change of address alone sends the
report again). A report the server refused is held (`client.heartbeat_refused`).

- An HDMI connector with nothing plugged in is `connected: false`
  (`client.hdmi_outputs`).
- The Frame is always `connected: true` when configured; whether it answers is
  in the wall heartbeat (`client.client_outputs`).
- **A Player that draws to files** (`FILE_OUTPUT_DIR`) reports no HDMI connector
  and no Frame: its one screen is `file-screen-0`, kind `framebuffer`, always
  connected at its configured size, with no identity, and with `FILE_OUTPUT_LABEL=on`
  one label output `file-label-0`, kind `epaper` because the contract's label
  outputs have no other (`__main__.player_outputs`, `displays/file_output.py`).
  It builds no `FrameIdentity`.
- **The Frame's identity is read for this report once per heartbeat interval**,
  one REST GET a minute with no art channel and no key, and the last one read is
  kept through a failed read, so a different set answering at the same address is
  reported within an interval without a restart (`client.identity_changed`,
  `client.FrameIdentity`). The wall's worker reads it separately, on every
  connection. Whether that read reaches the set the art channel is open on is the
  operator's to measure (`build-plan-audit-wave-3.md` § Operator's checklist).
- A panel is `connected: false` when it would not open or its last draw failed
  (`label_renderer.LabelPanel.report`). **Why** it would not open is not sent:
  `LabelPanel.error` is held and never reported. The journal's
  `panel.unavailable` is the only record.

### Metrics

None. No counter, gauge or time series exists in `src/` (searched for
`prometheus`, `statsd`, `opentelemetry`: nothing). The heartbeat's age is the one
quantity the server derives.

### Tracing

Not applicable. Each request is one hop with no fan-out; Arrt's
`observability-strategy.md` § Signals, the tracing row, says the same for the
server side.

## Instrumentation Layers

Layer 1 (automatic): systemd captures stderr and restarts. Layer 3 (contextual):
`work_id` is bound by context variable (below). Layer 4 (manual): every event
above is a hand-placed call. There is no layer 2.

## Correlation Context

- **`work_id`, bound, not passed.** `logs.work_context` sets a `ContextVar`;
  `logs.WorkCorrelationFilter` stamps it on every record. Bound at
  `displays/frame.py:FrameDisplay.show` (around a selection), `programmes/schedule.py`
  (three sites: a step, a composition, a collected composition) and
  `label_renderer.LabelRenderer._show`. Because it is a `ContextVar`, it follows
  into `asyncio.to_thread` work, which copies the context. It is the one id that
  spans the Player and the server (Arrt § Correlation).
- **`wall_id` and `label_id` are bound the same way, around each worker.** The
  supervisor binds a wall's id around everything its worker runs
  (`logs.wall_context`) and a label's id and the wall it captions around everything
  its renderer runs (`logs.label_context`); a task copies the context it is
  created in and `asyncio.to_thread` copies the caller's, so a line three calls
  deep in a pull, a draw thread or the television's reader task carries them.
  `WorkCorrelationFilter` adds them unless the call site passed its own, so a
  line about another wall than the one whose worker logs it says so itself.
  **Every event is declared as one of four kinds**, in lists in `tests/test_client.py`: a
  wall's (it carries the wall by context), a label's, one the supervisor logs
  about a wall (it passes `wall_id` itself, held by an `ast` check of each call
  site), and the process's (no wall); a name added without saying which fails
  there by name, and a structural test drives each kind of worker, a Player
  drawing to files included, and fails for a record with no wall
  (`test_every_line_a_worker_logs_names_its_wall`). The process-level lines in
  slideshow mode, which has no wall, say none.
- **`output`, `tv_content_id`, `status`, `server_url`** are carried where the
  call site has them.
- **Task names are set and then dropped.** Workers run in tasks named
  `wall:<id>`, `pull:<id>`, `label:<output>`, `compose:<wall>:<work>`
  (`client.Supervisor._start`, `_start_label`; `__main__._beside_its_pull`;
  `Schedule._start`), but `logs._BUILT_IN` lists `taskName` among the attributes
  the formatter leaves out.
- No request id. Nothing crosses to the server but the wall id in the path.

## Sensitive Data Filtering

Arrt § Sensitive Data in Logs is the rule. The two secrets on the Player are
`CLIENT_TOKEN` and the Frame's pairing token (`project-state.yaml`,
`handles_sensitive_data`), and, in slideshow mode, the owner's Reddit session
cookie, which signs in as their account. What keeps each out:

| Secret | Mechanism | Held by |
|---|---|---|
| `CLIENT_TOKEN` in a settings repr | `field(repr=False)` on `WallSettings.client_token` and `ClientSettings.client_token` | `tests/test_config.py::test_the_client_token_is_in_no_repr_and_no_startup_line` |
| `CLIENT_TOKEN` in a startup line | `ClientSettings.startup_lines`, `WallSettings.wall_lines`, `FrameSettings.frame_lines` list their fields by hand and omit it | the same test |
| `CLIENT_TOKEN` in a request log | Structural: no call site logs a header. Failures are logged by exception type and message (`Pull._pull_manifest`), which `aiohttp` builds from the URL, not the headers | Critic only |
| `CLIENT_TOKEN` sent elsewhere | Not attached to a media URL on another origin (`Pull._cache_media`, `pull._same_origin`) | `tests/test_pull.py` (a second `TestServer` as the other origin) |
| The TV token, logged by `samsungtvws` at INFO (`New token %s`) | `logs.configure` floors the `samsungtvws` logger at WARNING | `tests/test_logs.py::test_a_token_logged_at_info_by_that_logger_is_not_emitted` and its DEBUG sibling |
| The TV token's value in the startup line | Only its path is logged (`FrameSettings.frame_lines`, `tv_token_file`) | — |
| A token file in git | — | `tests/test_repository.py::test_no_credential_file_is_tracked` |
| The Reddit cookie (`REDDIT_SESSION`) in a repr or the slideshow's startup line | `field(repr=False)`; `SlideshowSettings.startup_lines` lists its fields by hand | `tests/test_config.py::test_the_cookie_is_in_no_repr_and_no_startup_line` |
| The cookie anywhere in a slideshow's journal | Every record of a whole run, message and every field, searched for it | `tests/test_slideshow.py::test_a_whole_slideshow_run_logs_no_cookie` |
| The cookie, or the account it signs in, from the settings page | Never echoed in a page; the account's own data (`/api/me`) is read for its kind only | `tests/test_settings_ui.py::…test_the_cookie_is_never_in_a_page_and_its_name_is_dropped_if_pasted`, `tests/test_reddit_source.py::test_the_accounts_own_data_reaches_no_log_and_no_listing` |
| redgifs' temporary token | Only how long it is valid is logged | — |

Tracebacks go in the `exception` field; `Formatter.formatException` prints no
local variables. Nothing else the Player logs is personal: works, walls, file
paths and the server's URL.

## Alerting

None on the Player. It sends nothing to a person. The server's health view,
reading heartbeat age, is the operator's alert surface; Arrt § Accepted detection
latency is its budget. The journal is read on demand.

## Health Signals

- **Healthy**: each wall heartbeat's `reported_at` advances about once a minute,
  `display_state` is `showing_art` with the expected `current_work_id`, the
  client heartbeat is recent, and the journal's last ERROR is older than the last
  restart.
- **Degraded, by design**: server unreachable (walls rotate from cache, wall
  heartbeats stop reaching the server and age there); set asleep or in use
  (`television_reachable: false`, or `display_state` `dark` / `in_use`); panel
  broken (`label_outputs[].connected: false`).
- **Worse than an error**: a `reported_at` that does not advance with a `null`
  `last_error` means the loop is not running (`deploy/README.md`,
  "Read the heartbeat rather than the wall").
- No health endpoint. The Player serves nothing.

## Infrastructure and Deployment

systemd and journald only, configured by `deploy/display.service` and
`deploy/journald.conf.d/10-bound-the-journal.conf`. Always on; nothing to
configure off. No cost.

## Agent-Accessible Observability

Not applicable beyond reading. The journal is JSON, so an agent with a shell on
the Pi reads it the way an operator does:

```sh
journalctl -u display -o cat --since -1h | jq 'select(.level == "ERROR")'
journalctl -u display -o cat | jq 'select(.work_id == "…")'
journalctl -u display -o cat | jq 'select(.event | startswith("pull."))'
cat "$CACHE_DIR"/<wall_id>/display-heartbeat-<wall_id>.json
```

The `tools/` scripts drive hardware and are not observability tools.

## Verification

- **The shape** is held by `tests/test_logs.py` (JSON per line, `work_id`,
  the `samsungtvws` floor).
- **The heartbeat documents** are validated against the contract's schemas:
  `heartbeat.v1` in `tests/test_player_contract.py` and
  `tests/test_display_state.py`; `client-heartbeat.v1` there and on every POST
  to `tests/server_double.py:ServerDouble.receive_client_heartbeat`. The wall
  heartbeat POST to the double (`ServerDouble.receive_heartbeat`) is not
  validated. `tests/test_heartbeat_key.py` pins `reported_at` in both schemas.
- **When the heartbeat is written** is held by `tests/test_heartbeat_wiring.py`.
- **The event names are pinned as a set.** `tests/test_client.py` walks every
  `log.<level>` call in `src/arrt_player` with `ast` (and the built names by the
  site that builds them) and the inventory above, and requires each name to be
  declared in exactly one of its four lists, and each declared name to be one the
  code or this inventory names. A rename in code fails there, and so does a
  rename here that the code does not follow, so **the inventory above is read by
  that test: a row's first column holds its event names in backticks, and only
  names the code logs may be written there**
  (`test_every_event_is_declared_as_a_walls_a_labels_or_the_processs`,
  `test_every_declared_event_is_one_the_code_or_the_record_names`).
- **On the Pi**, `deploy/README.md` § How to tell your own install worked is the
  operator's check, with the caveat below.

## Known gaps

Observed in the code while writing this. Items 1 and 2 are closed. Items 3–5 are
open as issue #12; item 6 is part of the `deploy/README.md` update due when the
Pi moves.

1. *(Closed 2026-11-19, #10: every line a wall's or a label's worker logs
   carries `wall_id` (and `label_id`) by context; see Correlation Context.)*
2. *(Closed 2026-10-27: `tv.close_failed` logged at DEBUG under an INFO root and
   was never emitted. It logs at WARNING, held by
   `test_samsung_client.py:test_a_close_that_fails_reaches_the_journal`.)*
3. **The Frame's `last_error` carries only unreachability.** Upload failures,
   unconfirmed removals and a wall that will not change are journal-only.
4. **Pull and supervisor failures never reach a heartbeat.** A wall whose feed is
   refused or whose media keep failing shows on the server only as a stale
   `manifest_schema` / `theme_id`; a crash-looping worker shows only as a
   heartbeat ageing.
5. **Why a panel is not connected is not sent** (`LabelPanel.error`).
6. **`deploy/README.md` § How to tell your own install worked is stale for this
   repo**: it reads `/srv/art`, `curation.service` and the monorepo checkout
   path, and its own note says to rewrite it "when wave 3 lands".

## Discrepancies with Arrt's record

For a pull request on Arrt, not an edit here.

- Arrt § What Each Failure Looks Like, the "panel captions the wrong work" row,
  says `label.drawn` carries `tv_content_id`. It carries `label_id` and
  `display_state`, with `work_id` bound (`LabelPanel.draw`); Arrt § Correlation's
  2026-10-08 note already says no label event carries `tv_content_id`.
- The same table's `label.unusable` row says the condition is written to the
  heartbeat's `last_error` and that `label_surface_working` stays `true`. The
  panel is no longer a wall's: no heartbeat carries either for a label, and the
  row two above it already says `label.unusable` is journal-only.
- Arrt § A client, and a wall on its screen lists seven events. The client and
  HDMI sets are now larger: `client.wall_unplaceable`, `client.worker_crashed` /
  `_ended`, `client.document_refused`, `client.cache_unwritable`,
  `client.heartbeat_refused`, `client.identity_*`, `screen.started` /
  `stopped` / `crashed`, and the `client.label_*` events listed in Arrt's own
  § Correlation note.
- Arrt § What Each Failure Looks Like still has the "*Planned, 2026-09-30:* the
  server is unreachable from a Player" row, built in wave 2. The built events are
  `pull.unreachable` / `pull.reachable`, `client.unreachable` /
  `client.reachable` and `label.server_unreachable` / `label.server_reachable`.
  The row's "heartbeat's cache report" does not exist: no heartbeat key reports
  what the cache holds.
- Arrt calls the Player "display" or "the display plane". This repo keeps
  "display" where it is an interface: the unit `display.service`, the journal
  identifier `display`. A Frame wall's loop events were `daemon.*` until 2026-11-19 and are `frame.*`.
