# HDMI output: what the Pi does when the Player draws on a connector

**What this is.** The evidence `arrt-player/src/arrt_player/kms.py` is built on: how a
headless Pi lets a service user put a picture on an HDMI connector, what the
kernel reports as screens come and go, and what the screen showed. It records
what was *observed*; where a row is inference, it says so.

**Host under test:** Raspberry Pi 4 Model B Rev 1.5, 8 GB, Raspberry Pi OS
trixie, kernel `6.18.34+rpt-rpi-v8`, `dtoverlay=vc4-kms-v3d`, booting to
`multi-user.target` with no display server. The `vc4` card is `/dev/dri/card1`
(`card0` is `v3d`, the 3D engine, with no connectors). The Player's interpreter
is uv's CPython 3.14 with Pillow 12.3; `libdrm.so.2` is installed with the OS.

**Screen under test:** an LG 27" 4K monitor (EDID: `GSM` "LG HDR 4K", product
`0x7706`, 2023; 600×340 mm), on HDMI-A-1 (the micro-HDMI socket nearest the
USB-C power) by a micro-HDMI→HDMI cable.

**Measured 2026-10-02**, with a throwaway `ctypes` spike and then the built
`KmsOutput` itself, both run on the Pi.

## The technology

| Option | Verdict | Why |
|---|---|---|
| `libdrm` through `ctypes`, legacy KMS, dumb buffers | **chosen** | Present with the OS; five calls plus three ioctls; worked first time as the service user |
| `python3-kms++` (the Pi's packaged binding) | out | Built for the system Python (`python3 << 3.14`); the Player runs 3.14 from uv |
| A display server (X, Wayland, a kiosk browser) | out | A desktop's worth of packages and a login session to draw one still picture |
| The fbdev emulation (`/dev/fb0`) | out | Fixed at the console's mode, no hotplug, and deprecated on KMS drivers |
| Atomic KMS with planes | not needed | Only a crossfade would need composition, and `clients.md` asks only "fitted to the screen" |

The `ctypes` structure layouts (`drmModeRes`, `drmModeConnector`,
`drmModeModeInfo`, `drmModeEncoder`) and the dumb-buffer ioctl numbers
(`0xB2` create, `0xB3` map, `0xB4` destroy, from `drm.h`) were checked by use:
the spike enumerated both connectors with their right names, mode counts and
physical sizes, and the pictures drawn were the right size and colour.

## What was measured

| Question | Observed |
|---|---|
| Can the service user open the card? | Not until it is in group `video` (`crw-rw---- root video`). `tvpi` was added on the owner's yes; the built output then ran as `tvpi` with no root |
| Which mode? | 27 modes, **none flagged preferred** (all `DRIVER`). The EDID's native 3840×2160@60 is filtered without `hdmi_enable_4kp60`, and the preferred flag goes with it. The first mode listed is 3840×2160@30, the same line `/sys/class/drm/card1-HDMI-A-1/modes` starts with, which is what the client heartbeat reports |
| How long does a picture take? | Spike, as root: decode and fit 0.84 s, copy into the buffer 0.04 s, `SetCrtc` 0.025 s, for a 3840×2160 JPEG. Built output, as `tvpi`: `show` 0.46–0.59 s end to end for two 3840×2160 renders and a 1200×2000 portrait |
| Buffer layout | pitch 15360 = 4 × 3840: rows are packed, so the copy is one slice |
| Colour order and range | Owner, on a test pattern of greys 0/8/16/28/48/96/255 and a yellow block: black is black, 0, 8 and 16 distinct, yellow is yellow. The connector sends full-range RGB, 8 bpc (`broadcast_rgb=Automatic`, `is_limited_range=n` in debugfs) |
| When the program exits | The kernel restores the text console: the owner saw art, then the login prompt. The Player must hold the device for its life, and a Player that has stopped shows the console on the wall |
| Cable pulled, then pushed back (~24 s) | One kernel uevent per edge on `/devices/platform/gpu/drm/card1`, `ACTION=change HOTPLUG=1 CONNECTOR=35`; the sysfs `status` follows. The CRTC stayed enabled on the program's buffer throughout, and **the picture came back by itself** (owner) |
| Brightness | DDC/CI answers on `/dev/i2c-20` (needs `i2c-dev` loaded, root): brightness 0–100 and contrast read and set with `ddcutil`; display mode (`0xDC`) unsupported. Set to 10 at the owner's request. Nothing in the Player does this, and nothing asks it to. The full set, and power, in § Controlling the monitor over the cable |

## A screen the Pi cannot see

For about fifteen minutes before the screen was found, both connectors read
`disconnected` with no modes. A forced probe (`detect` written to `status`)
agreed, the EDID address `0x50` did not answer on either DDC bus (`EREMOTEIO` on
`i2c-20` and `i2c-21`), and the firmware's own log had given up on EDID at two
boots (`HDMI0:EDID error reading EDID block 0 … giving up`). The monitor's screen
was black, with no "no signal" message. The owner moved cables and the screen
was found; the cause was not isolated. A loose micro-HDMI plug and a monitor's
deep sleep both fit (inference).

What it means for the Player: **an HDMI wall whose screen is not seen looks
exactly like an empty socket**, and the Player says so (`screen.absent`) and
reports `connected: false` for the output. It does not try to wake the screen:
nothing over CEC, which is a power act (Arrt's `nonfunctional-requirements.md`
§ The television belongs to whoever is using it). The firmware's log
(`sudo vclog --msg`) is the place to look when a screen that is plugged in is
not seen.

## What the output does with this

- Draws the render fitted whole to the first listed mode, centred on black.
- Holds the card open for the life of the process, shared by every connector on it.
- Two dumb buffers per connector: the next picture is written into the one not
  on screen, then made current with `SetCrtc`.
- Reads presence and size from sysfs on every poll, as the heartbeat does, and
  draws the current render again when a screen comes back or changes size,
  although the kernel kept the picture through a replug of the same screen,
  because the screen that comes back need not be the one that left.
- A screen that is absent costs one line and nothing else. A device that refuses
  (no `video` group, another program holding the card) raises, and the wall
  logs it, keeps rotating and tries again on every poll.

## Controlling the monitor over the cable

**Measured 2026-11-13** on the same Pi and the same LG, with the owner watching
the screen and the Player stopped (`systemctl stop`) for the power tests. Nothing
in the Player does any of this, and nothing asks it to; it is the evidence a
requirement would be written from.

| Question | Observed |
|---|---|
| What DDC/CI offers | `ddcutil --bus 20 capabilities` (root, `i2c-dev` loaded): MCCS 2.1. Brightness `0x10`, contrast `0x12`, colour preset `0x14` (6500 K, 9300 K, User 1), RGB gain `0x16`/`0x18`/`0x1A`, sharpness `0x87`, input source `0x60` (HDMI-1, HDMI-2, DP-1, DP-2), speaker volume `0x62` and mute `0x8D`, power mode `0xD6` (`0x01` on, `0x04` off), the factory resets `0x04`/`0x05`/`0x08`, and LG codes `0xF4`–`0xFF` and `0x15` that nothing documents |
| Values found | Brightness 10, contrast 70, 6500 K, gains 50, sharpness 50, input HDMI-1, volume 28, unmuted |
| Brightness set live | 10 → 60 → 10, each write read back; the owner saw it change |
| DDC/CI from plain Python, no `ddcutil` | A throwaway spike, run as a non-root user in group `i2c` (the bus is `root:i2c 0660`). The bus was found from the connector's sysfs `ddc` link (`card1-HDMI-A-1/ddc` → `i2c-20`; HDMI-A-2's is `i2c-21`). With `I2C_SLAVE` 0x37, Get VCP `0x10` read 10 of max 100, and Set `0x10`=20 read back 20 and was restored to 10, at about 50 ms per exchange (a 50 ms wait between write and read). Request checksum: XOR seeded with `0x6E`. Reply checksum: XOR seeded with `0x50`, and it held on every reply. An unsupported code (`0xDC`) is answered with result byte `0x01` and a valid checksum (`6e880201dc01ffff000068`), so 'unsupported' is told apart from a garbled reply. The Player was running throughout, holding the card. Then the built `ddc.py`, on the Pi: the same reading on HDMI-A-1. **HDMI-A-2, with nothing plugged in, raised `OSError` errno 121 (`EREMOTEIO`)**: nothing acknowledged the address, which `ddc.NoAnswer` names as the screen's answer, not a fault of this host. Not yet seen: a garbled reply, or a screen that has DDC/CI and refuses it |
| Power off over DDC/CI (`0xD6` ← `0x04`) | The write succeeded and **the screen went dark**; the owner could not tell powered-off from blanked. **Read back while dark, `0xD6` still said `0x01` (on)**, so the monitor's report of its own power mode is not evidence of it. `0xD6` ← `0x01` 10 s later succeeded; the screen was lit afterwards, but whether it lit at that write was not timed |
| Power by signal (the Pi stops scanning out) | With the Player stopped, `4` written to `/sys/class/graphics/fb0/blank` took the HDMI-A-1 pipe (`pixelvalve-2` in `/sys/kernel/debug/dri/1/state`) to `active=0`; **the owner saw the monitor go to standby**. `0` written back after 20 s returned it to `active=1`, and **the monitor woke by itself** within the 30 s that followed, with no DDC/CI and no button |
| An ambient light reading | **None found.** MCCS defines no light-sensor code, and of the LG's undocumented ones (`0x15`, `0x4D`–`0x4F`, `0xEF`, `0xF4`–`0xF9`, `0xFE`), with brightness and contrast, none changed across 27 samples in the room's ordinary light, 42 with it as dark as the owner could make it and 15 with it much brighter than ordinary: every value was identical. The Pi has none either: no IIO devices, no camera, and the header's I2C bus (`i2c_arm`) not enabled; `/dev/i2c-20` and `-21` are the two HDMI DDC buses |
| HDMI-CEC | Both adapters present (`/dev/cec0` on HDMI-A-1, physical address `1.0.0.0`; `/dev/cec1` on HDMI-A-2). A playback-device scan from `cec0` found **no other device**: this LG monitor takes no part in CEC. The scan's claimed logical address was released afterwards (`cec-ctl --clear`) |

What it means: **switching this screen off and on is done by stopping and
resuming the signal**, which it follows on its own, and which only the process
holding the card (the Player) can do while it runs. DDC/CI can dark the screen
but cannot say whether it is dark. Brightness and the rest work over DDC/CI and
need root or a group with access to `/dev/i2c-20`. The pipe-to-connector mapping
is read from debugfs, never assumed: HDMI-A-1 is on `pixelvalve-2`, not
`pixelvalve-0`.

## Not observed

- A second screen on HDMI-A-2, or two walls on one card at once.
- A television rather than a monitor: whether a TV switched to another input
  drops hot-plug the way an unplugged cable does.
- A screen changing to a different size while the Player runs (the test covers it
  with a double).
- A 4K60 mode (`hdmi_enable_4kp60=1`); 4K30 is ample for a still picture.
- How long the LG takes to reach standby once the signal stops, and whether
  DDC/CI still answers (brightness, input) while it is there.
