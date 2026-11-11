---
schema: 1
type: feature
scope: slideshow
date: 2026-11-11
title: Slideshow mode, which plays a Reddit listing on an HDMI screen through mpv with no Arrt server
---
A Player can now run without an Arrt server. In slideshow mode it plays one
Reddit listing, in order and looped, on one HDMI screen at 4K. It shows stills,
GIFs and clips, muted, and captions each post on the e-paper label with its
title, subreddit, author and upvotes. The mode, the listing and the session
cookie are set on the settings page. The Player runs in one mode or the other,
never both (backlog #27).

### What changed
- **`PLAYER_MODE`** (`arrt`, the default, or `slideshow`), with `SLIDESHOW_URL`,
  `REDDIT_SESSION`, `SLIDESHOW_OUTPUT` (default `hdmi-a-1`) and
  `SLIDESHOW_MIN_SECONDS` / `SLIDESHOW_MAX_SECONDS`. A still shows for the
  minimum. A clip repeats until it reaches the minimum, and the total is cut at
  the maximum.
- **mpv draws the slideshow**, as a process the Player owns. It runs in its own
  process group and is stopped on every way out. One left behind by a Player
  that was killed is stopped at the next start. It is given files from the cache,
  never addresses.
- **A second HTTP client** (`sources/fetch.py`), which the plane-isolation guard
  bounds by what it can reach. API calls go only to `www.reddit.com` and
  `api.redgifs.com`, and the cookie goes only to Reddit. Media comes from any
  host a post links, but only at a public address, checked when the connection
  is made. A download is kept only if its bytes are a picture or a clip.
- **Media from any linked host.** Reddit's own copy of a post's media is used,
  and per-host resolvers fetch better copies where one exists: redgifs HD and
  imgur. A resolver that fails leaves the post with Reddit's copy.
- **A 2 GB media cache** under `$CACHE_DIR/.slideshow/`. It keeps what the
  current listing names and deletes the rest. The last listing is saved, so a
  restart while Reddit is down still plays.
- **Reddit answering anything but a listing never blanks the screen.** The
  cache plays on, and the page says why: listing refused, cookie rejected, rate
  limited or unreachable. The page also shows when the cookie expires.
- **The settings file is now layout version 2**, which adds the slideshow's keys.
- **Every log line is findable by its event field.** The source, cache and
  resolvers wrote their event names into the message text, where a query on the
  `event` field cannot see them. A test now walks every log call. Tests that
  check something is absent from the log now read the formatted journal line,
  fields included, through `conftest.journal`, so a secret in a field cannot
  slip past them.
- **Deploy:** the unit gains group `render`. `deploy/README.md` § The slideshow
  covers installing mpv, switching modes and rolling back.

### Confirmed by the owner (2026-11-10)
- The mode covers the whole Player; HDMI only for now.
- No NSFW filter: what the URL points at is the owner's choice.
- imgur albums show their cover image.
- 1080p clips upscaled by the display hardware on the 4K screen ("looks much
  better than before").
- mpv installed by the install and upgrade flow.

## Consumer note
**Install mpv before deploying this revision.** The runbook checks for it before
the sync. A wall in Arrt mode never runs it, and needs no new `.env` key.

    sudo apt-get update && sudo apt-get install --no-install-recommends mpv

The unit's `SupplementaryGroups` gains `render`. Install the unit as the runbook
says.

**Rolling back to an older revision** after saving any setting on this one needs
the settings file put back to layout 1 by hand. Delete the slideshow keys from
`$CACHE_DIR/.settings.json` and set `version` to 1, or the older Player refuses
the file and names the version (`deploy/README.md` § The slideshow).

The event `slideshow.reddit.<outcome>` is now `slideshow.reddit.not_listed`,
with `outcome` as a field. The source, cache and resolver events carry their
details as fields, not inside the message.
