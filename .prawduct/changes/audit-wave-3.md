---
schema: 1
type: fix
scope: audit-wave-3
date: 2026-11-25
title: The Player's share of the 2026-11-19 audit, wave 3
closes: []
---
What waves 1 and 2 left, built as chunks 01–09 of `build-plan-audit-wave-3.md` (no
open issue is closed by it; the plan names none, and #2, #12, #23 and #31 stay open
for the reasons it gives).

**The Player removes from a television only what it recorded uploading** (the owner's
ruling, 2026-11-19; its costs accepted 2026-11-25). Every upload is written to the
wall's store before it is sent, with the set it goes to and the `image_date` it
carries, and a reconciliation removes, of what the set lists, only the pictures those
records name there. The household's own pictures, an older Player's that it never
recorded and any of this Player's its records cannot match are left where they are,
and the journal says how many, once per set (`tv.others_left_alone`). An upload whose
record cannot be written is not sent. An upload made while the set will not say which
it is is recorded with no set and found only on a set it was sent toward or that spoke
on its connection. The store moves to schema 5 (`tv_upload`). The settings page's
sentences for a new Frame address say the other set's own pictures are left alone.

**One model of which set the wall is on.** The wall's state with its set is one of
four phases, changed in one place, so an outage, a reconnect to another set and a
store fault cannot leave it half in one and half in another; the wall's running time
with a feed is counted on the device's store whichever store its bindings come from,
and a work refused on a set the connection has since left is put up on the new one in
the same pass.

**The server hears more.** The client heartbeat carries the address of the settings
page (`settings_url`), so the server's Players page can link to it, and the Frame's
identity is read again once a minute, so another set at the Frame's address is
reported within a minute (`client.identity_changed`). A report the server refuses in
its error form is held until it changes or five minutes pass. The settings page says
the Player's state in the server's words, and a screen in its picker is "Screen off"
or "No screen".

**The contract is `contract-v1.3.0`.** The test double answers from the contract's
transport table; a feed is read to 16 MiB; a feed that stages one work twice is
refused, and the wall keeps its last good feed; every invalid fixture has its reason.

**Smaller.** Every file the Player writes whole goes through one helper
(`safe_write.py`), and a failed write leaves no temporary file. A screen that refuses
a picture is asked for it again; a Player that draws to files writes again until its
picture and record have both landed. Shipped prose names no chunk, and a scan refuses
one. The two panel journal lines `panel.close_failed` and `label.close_abandoned` name
their output.

## Consumer note
An operator upgrading: **the owner is told first that pictures an older Player put
on the television and never recorded stay there for good** (they can delete them on
the set), and a store deleted at runbook step 6a leaves that set's earlier pictures
the same way. **A set that does not echo the `image_date` it is sent** matches none of
the Player's records, and then nothing the Player uploads there is ever removed; check
it before deploying (the plan's operator's item 14). The Frame wall's store moves to
schema 5; **going back across it needs the owner's say-so each time**: the release
before (v0.8.0) refuses the store, parks only that wall and touches nothing on the
set, and only after the runbook's step 19b sets the store's version to 2, or the store
is deleted, does its sweep remove every uploaded picture its own records do not name,
the household's and the one on screen included. The Frame's identity is read once a
minute (one REST GET a minute); whether that keeps a sleeping set awake is unmeasured.
None of this has run against the television or on a Pi.
