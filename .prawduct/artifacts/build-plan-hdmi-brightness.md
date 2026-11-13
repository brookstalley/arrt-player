---
artifact: build-plan
version: 2
scope: hdmi-brightness
branch: feature/hdmi-brightness
base: develop
working-branches: []
depends_on:
  - artifact: hdmi-output-findings
  - artifact: architecture
  - artifact: observability-strategy
  - artifact: data-model
  - artifact: project-preferences
governed_by:
  - artifact: project-preferences
    dispositions:
      - "Hardware and network access sit behind an interface → conforms: the DDC/CI bus sits behind a protocol, which the suite doubles. Nothing in the suite opens /dev/i2c-*."
      - "Catch specific exceptions → conforms: a bus failure is an OSError, reported once per episode and never allowed to stop the wall."
      - "Every log line carries its event as a field → conforms: screen.brightness_set, screen.brightness_unsupported, screen.brightness_failed and screen.brightness_recovered."
      - "No hardcoded deployment values → conforms: the levels and the location come from .env."
      - "The television belongs to whoever is using it → inapplicable: an HDMI screen the Player holds has no television state, and brightness is not a power act. The Frame's brightness is untouched."
      - "The Player never requires the server to be reachable → conforms: brightness is computed on the device from the clock and the location."
partition: serial — 02 wires 01's bus into the screen display, and 03 deploys and accepts both on the Pi
last_validated: 2026-11-13
---

# Build plan: an HDMI screen's brightness follows the sun (#34)

## Goals

**Response taken:** proceed, citing #34. The owner settled its requirements on 2026-11-13 in answer to one batch of questions, with the monitor in front of them. Everything not in that list is marked *Inferred*.

**What the owner asked for (2026-11-13):**
- The sun drives the brightness of every HDMI screen that answers DDC/CI brightness, using the curve the Frame already follows (`brightness.sun_state`).
- **Night 5, day 50** by default, both overridable in `.env`.
- **A change made by hand sticks** until the curve's computed value next changes. As on the Frame, the Player writes only when its value changes, on `BRIGHTNESS_INTERVAL_SECONDS` (300 s).
- **Written directly from Python**, not through `ddcutil`.
- **On by default** for a screen that answers. A screen that does not answer is left alone and said once.

**Out of scope:** dark hours (#35, Arrt #425), which stop the signal and are not brightness. Contrast and the monitor's other controls. The Frame, which already follows the sun.

**Inferred (the builder's, flagged in #34 for the owner to challenge):**
- **Location is needed only once a screen answers.** With no `LATITUDE`/`LONGITUDE`/`LOCATION_NAME`, the Player leaves every screen's brightness alone and says so once at start. It does not refuse to run.
- **The levels are in the monitor's own units** (`SCREEN_MIN_BRIGHTNESS`, `SCREEN_MAX_BRIGHTNESS`, matching the Frame's `TV_MIN/MAX_BRIGHTNESS`), and a value above the maximum the monitor reports is clamped to it.
- **No write while the screen is not showing art** (dark or absent). A brightness write must never be what wakes a screen, and #35 relies on that.
- **Slideshow mode** (`feature/slideshow`, not on develop) draws through mpv, not `ScreenDisplay`. Whichever of the two branches lands second wires brightness into the slideshow's screen as well. That is recorded here so the merge does not drop it.

### Decisions
- **DDC/CI over `/dev/i2c-N` from Python** (owner, 2026-11-13). The spike on the Pi measured it working as a non-root user in group `i2c`: Get and Set VCP `0x10` at about 50 ms each, a reply checksum seeded with `0x50`, and an unsupported code answered with result `0x01` and a valid checksum (`hdmi-output-findings.md` § Controlling the monitor over the cable).
- **The bus is found from the connector**, through the `ddc` link in the connector's sysfs directory (`card1-HDMI-A-1/ddc` → `i2c-20`), never assumed.
- **Nothing acknowledging the address is the screen's answer, not a fault of this host** (decided building chunk 01; measured: the empty HDMI-A-2 raises `EREMOTEIO`, errno 121). `ddc.NoAnswer` is said once as `screen.brightness_unsupported` and asked again each interval, quietly, because a screen asleep may answer later. `ddc.Unsupported` (the monitor says it has no such control) is not asked again until the screen is replaced.
- **Every write is read back** (decided building chunk 02). The monitor sends nothing in reply to a set, so `screen.brightness_set` carries `read_back`, and a monitor that keeps its old value logs it at WARNING instead of looking applied.
- **A screen is asked afresh when it comes back.** Unplugged and replugged, it is probed and written at once, whatever the interval, because the screen that comes back need not be the one that left (as `kms.py` redraws).

## Build Chunks

### Chunk 01: The DDC/CI bus
- `ddc.py`: the request encoding and reply parsing as pure functions, held to the spike's real reply bytes. The parse distinguishes a valid value, an unsupported code and a garbled reply. `bus_for(connector)` follows the sysfs `ddc` link. `I2cBus` opens the device and sets the address with `I2C_SLAVE`, then writes, waits, reads and retries a garbled reply, behind a `DdcBus` protocol.
- Tests: encoding and checksums against the spike's bytes; a garbled reply retried, then refused; unsupported told apart from garbled; `bus_for` on a fake sysfs tree, including a connector with no `ddc` link.
- **Done when:** the suite is green, ruff and black are clean, and the module's tests were seen red against a re-break.

### Chunk 02: The screen follows the sun
- `config.py`: `ScreenBrightnessSettings` on `ClientSettings`, with the levels, the interval and an optional location. `None` when no location is set.
- `displays/screen.py`: `ScreenDisplay` applies brightness on `after` and `idle`, on the interval, only while showing art, and writes only on a change of value. It probes the screen once and re-probes after the screen goes away and comes back. Each episode of failure, and an unsupported screen, is said once. The DDC exchange runs in a thread.
- `__main__.py`: passes the settings and the connector's bus through `run_screen_wall`. The client's start line says whether screen brightness is on, and why not.
- Tests, through the wall with a clock and a fake bus: the curve's value written at noon and at night; a value changed by hand left alone until the curve moves; nothing written while dark or absent; an unsupported screen said once and never written; a failing bus said once, then recovered; no location means no writes and one start line. Each is seen red against its re-break.
- Artifacts: `.env.example`, `data-model.md` (the keys), `observability-strategy.md` (the events), `architecture.md` (the module).
- **Done when:** as for 01, plus the Critic's review of 01 and 02.

### Chunk 03: The Pi
- `deploy/display.service` adds `i2c` to `SupplementaryGroups`, with a test beside the unit's existing ones. `deploy/README.md` gains the module line (`/etc/modules-load.d/i2c-dev.conf`), the group, the keys and how to tell it worked.
- On the Pi, with the owner watching: deploy, see `screen.brightness_set` move the LG on the curve, and change the brightness by hand and see it stay. Record the result in `hdmi-output-findings.md`.
- **Done when:** the Pi acceptance is observed, and the cumulative Critic review and the PR are done.

## Governance Checkpoints
- A Critic review after chunk 02 (inner), and the cumulative review at the PR.
- The owner at the Pi for chunk 03.
