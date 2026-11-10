"""A stand-in for mpv that speaks its JSON IPC over a real Unix socket.

**Every behaviour here was measured from mpv 0.40 on the wall's Pi (2026-11-10)**,
because a double that fails earlier, or more politely, than the real one makes the
renderer's tests pass against a program that does not exist:

* `loadfile` answers `success` at once, whatever happens next. The outcome comes
  later as events: `start-file`, then `file-loaded` and `playback-restart`, or two
  error-level `log-message`s and `end-file` with `reason: error` for a missing file.
* A video output that cannot start (another process holds the screen) says so in
  a `fatal` log line, then `end-file` with `reason: error` and `file_error: "no
  audio or video data played"`.
* **A normal start logs an error-level line too** (`Can't open TTY for VT control`,
  under systemd), so "any error line" cannot mean "failed".
* With the overlay planes the wrong way round, every atomic commit fails with a
  warn-level `Failed to commit atomic request` while `frame-drop-count` stays 0
  and nothing at all reaches the screen.
* `duration` is 0.0 for an image. An unknown property answers `property not found`.

Run as `python fake_mpv.py <mpv options>`. A media file's text is its `duration`
(`"18.0"`), `still`, or `corrupt` (mpv opens it and finds nothing to play, which
it reports exactly as it reports a missing file). `FAKE_MPV_BEHAVIOUR` picks failures (comma-separated):
`commit_fail`, `vo_fail`, `die_on_load`, `stubborn` (ignores `quit` and SIGTERM),
`no_duration` (a loaded file whose `duration` mpv cannot give: it answers
`property unavailable`, as it does for any property with no value now, measured).
`FAKE_MPV_RECORD` names a file the command line, then each received command, is
appended to, as JSON.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

BEHAVIOUR = set(filter(None, os.environ.get("FAKE_MPV_BEHAVIOUR", "").split(",")))
RECORD = os.environ.get("FAKE_MPV_RECORD")
#: How long after a file loads `commit_fail` starts failing commits: after the
#: load has answered, as with the real mpv, and well inside the tests' waits.
COMMIT_FAIL_ONSET_S = 0.2


def option(name: str) -> str | None:
    for argument in sys.argv[1:]:
        if argument.startswith(f"--{name}="):
            return argument.split("=", 1)[1]
    return None


class Fake:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.lock = threading.Lock()
        self.log_level: str | None = None
        self.entry = 0
        self.path: str | None = None
        self.duration = 0.0
        self.loop_file = "no"
        self.vo_started = False
        self.committing = threading.Event()

    def send(self, message: dict) -> None:
        with self.lock:
            self.connection.sendall(json.dumps(message).encode() + b"\n")

    def log(self, prefix: str, level: str, text: str) -> None:
        if self.log_level is not None:
            self.send({"event": "log-message", "prefix": prefix, "level": level, "text": text + "\n"})

    def handle(self, request: dict) -> None:
        command, rid = request.get("command", []), request.get("request_id", 0)
        if RECORD:
            with Path(RECORD).open("a") as record:
                record.write(json.dumps(command) + "\n")
        name = command[0] if command else None
        if name == "request_log_messages":
            self.log_level = command[1]
            self.send({"request_id": rid, "error": "success"})
        elif name == "loadfile":
            self.committing.clear()
            self.entry += 1
            self.send({"data": {"playlist_entry_id": self.entry}, "request_id": rid, "error": "success"})
            if "die_on_load" in BEHAVIOUR:
                os._exit(1)
            self.load(command[1])
        elif name == "get_property":
            self.get(command[1], rid)
        elif name == "set_property" and command[1] == "loop-file":
            self.loop_file = str(command[2])
            self.send({"request_id": rid, "error": "success"})
        elif name == "quit":
            self.send({"data": None, "request_id": rid, "error": "success"})
            if "stubborn" not in BEHAVIOUR:
                self.send({"event": "end-file", "reason": "quit", "playlist_entry_id": self.entry})
                os._exit(0)
        else:
            self.send({"request_id": rid, "error": "invalid parameter"})

    def load(self, path: str) -> None:
        self.send({"event": "start-file", "playlist_entry_id": self.entry})
        file = Path(path)
        if not file.is_file() or file.read_text().strip() == "corrupt":
            self.log("file", "error", f"Cannot open file '{path}': No such file or directory")
            self.log("stream", "error", f"Failed to open {path}.")
            self.send({"event": "end-file", "reason": "error", "playlist_entry_id": self.entry, "file_error": "loading failed"})
            self.send({"event": "idle"})
            return
        if not self.vo_started:
            self.log("vo/gpu/drm", "error", "Can't open TTY for VT control: No such device or address")
            self.log("vo/gpu/drm", "warn", "Failed to set up VT switcher. Terminal switching will be unavailable.")
        if "vo_fail" in BEHAVIOUR:
            self.log("vo/gpu/drm", "warn", "Failed to acquire DRM master: Permission denied")
            self.log("vo/gpu", "error", "Failed initializing any suitable GPU context!")
            self.log("cplayer", "fatal", "Error opening/initializing the selected video_out (--vo) device.")
            self.log("cplayer", "fatal", "No video or audio streams selected.")
            self.send(
                {
                    "event": "end-file",
                    "reason": "error",
                    "playlist_entry_id": self.entry,
                    "file_error": "no audio or video data played",
                }
            )
            self.send({"event": "idle"})
            return
        self.vo_started = True
        text = file.read_text().strip()
        self.duration = 0.0 if text == "still" else float(text)
        self.path = path
        self.send({"event": "file-loaded"})
        self.send({"event": "playback-restart"})
        if "commit_fail" in BEHAVIOUR:
            self.committing.set()

    def get(self, name: str, rid: int) -> None:
        values = {"duration": self.duration, "path": self.path, "loop-file": self.loop_file, "frame-drop-count": 0}
        if name == "duration" and ("no_duration" in BEHAVIOUR or self.path is None):
            self.send({"request_id": rid, "error": "property unavailable"})
        elif name in values:
            self.send({"data": values[name], "request_id": rid, "error": "success"})
        else:
            self.send({"request_id": rid, "error": "property not found"})

    def commit_failures(self) -> None:
        # The real mpv's first failed commit came 0.86 s after it started, on the
        # Pi on 2026-11-10, well after a load had answered. Failing at once would
        # sometimes fail the load itself, which is not the case the tests ask about.
        self.committing.wait()
        time.sleep(COMMIT_FAIL_ONSET_S)
        while True:
            if self.committing.wait(timeout=0.5):
                self.log("vo/gpu", "warn", "Failed to commit atomic request: Error number 22 occurred")
                time.sleep(0.03)


def main() -> None:
    if "stubborn" in BEHAVIOUR:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if RECORD:
        with Path(RECORD).open("a") as record:
            record.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid()}) + "\n")
    path = option("input-ipc-server")
    if path is None:
        sys.exit("fake mpv: no --input-ipc-server")
    Path(path).unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(1)
    while True:
        connection, _ = server.accept()
        fake = Fake(connection)
        threading.Thread(target=fake.commit_failures, daemon=True).start()
        buffer = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    fake.handle(json.loads(line))


if __name__ == "__main__":
    main()
