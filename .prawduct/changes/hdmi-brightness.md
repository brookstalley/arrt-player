---
schema: 1
type: feature
scope: hdmi-brightness
date: 2026-11-13
title: An HDMI screen's brightness follows the sun
closes: [brookstalley/arrt-player#34]
---
An HDMI screen that answers DDC/CI now has its brightness set from the sun, as the Frame's has been, between a night level and a day level (`build-plan-hdmi-brightness.md`, chunks 01–03).

### What changed
- **Chunk 01, `ddc.py`:** a monitor's DDC/CI features over its HDMI connector's I2C bus, written directly from Python with no `ddcutil` (owner, 2026-11-13). The tests hold it to the bytes the reference LG exchanged on the Pi. The bus is found from the connector's sysfs `ddc` link. An unsupported feature, nothing answering, a garbled reply and a fault of this host are kept apart.
- **Chunk 02, `displays/screen_brightness.py`:** `ScreenBrightness` follows `brightness.level_at` between `SCREEN_MIN_BRIGHTNESS` and `SCREEN_MAX_BRIGHTNESS` (defaults 5 and 50), on `BRIGHTNESS_INTERVAL_SECONDS`.
  - It writes only when the value changes, so a brightness set by hand stays until the sun moves the value or the Player restarts.
  - It writes nothing to a screen that is dark or absent, and re-probes a screen that comes back.
  - It reads every write back.
  - It reports an unsupported or unanswering screen once, and never stops the wall.
- **`config.py`:** the screens' brightness is off with no location, or with `SCREEN_BRIGHTNESS=off`. The Frame and the screens read the location through one parser.
- **`brightness.py`:** a day with no dawn, dusk, sunrise or sunset (north of about 54.6° near the June solstice, and inside the polar circles) no longer raises from astral. That raise crashed any wall that followed the sun, the Frame's included.
- **Chunk 03, the Pi:** `display.service` adds group `i2c`, and `deploy/modules-load.d/arrt-player.conf` loads `i2c-dev` at boot. The Pi acceptance was observed with the owner at the monitor (`hdmi-output-findings.md`).

## Consumer note
Install `deploy/modules-load.d/arrt-player.conf` and the updated `deploy/display.service` as `deploy/README.md` § An HDMI screen's brightness follows the sun says. With `LATITUDE`, `LONGITUDE` and `LOCATION_NAME` set, every HDMI screen that answers DDC/CI then follows the sun between 5 and 50. Set `SCREEN_MIN/MAX_BRIGHTNESS` for other levels, or `SCREEN_BRIGHTNESS=off` to leave the screens alone. On a client with a Frame north of about 54.6°, the wall no longer crashes around the June solstice.
