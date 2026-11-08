---
schema: 1
type: fix
scope: pillow-13
date: 2026-11-08
title: Pillow 13 and certifi, for their security fixes
---

The Player locked Pillow 12.3.0, the version Dependabot flagged on Arrt with 5
critical and 23 high alerts, among them JPEG and TIFF decoder buffer overflows and
decompression-bomb bypasses. The Player decodes every picture its server hands it
(`compose.py`, `kms.py`, `panel/epaper.py`), so it carries the same exposure. Pillow
goes to 13.0.0, with `pillow>=13.0.0` declared and a comment saying it is a security
floor; certifi to 2026.10.23 (one critical alert on Arrt); black to 26.10.1, matching
Arrt and the contract package. Pillow 13's incompatible changes were read against every
call site: the Player uses none. The whole suite, the raster group included, passes
under 13.0.0, and `tools/label_preview.py` starts. Arrt took the same upgrade in its
#411.

## Consumer note

No visible change. The image library the Player uses to read pictures is patched
against the vulnerabilities GitHub reported.
