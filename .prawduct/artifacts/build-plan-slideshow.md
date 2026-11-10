---
artifact: build-plan
version: 2
scope: slideshow
branch: feature/slideshow
base: develop
working-branches: []
depends_on:
  - artifact: build-plan-settings-ui
  - artifact: architecture
  - artifact: security-model
  - artifact: nonfunctional-requirements
  - artifact: hdmi-output-findings
  - artifact: project-preferences
  - artifact: platform-and-dependency-findings
  - artifact: observability-strategy
governed_by:
  - artifact: project-preferences
    dispositions:
      - "The manifest and its media are the only channel from the server, and only pull.py opens an HTTP client → exception, owner-requested in #27 and widened 2026-11-10 to media from any host a post can link: one module, sources/fetch.py, may open an HTTP client. Listing and API calls go to a constant host set; media downloads reach public addresses only, and never the Arrt server. See the DECISION in Goals. The guard keeps every other module as before, and planted violations show it still fails."
      - "No secret ever reaches a log line → conforms: the Reddit session cookie is held like CLIENT_TOKEN (write-only on the page, kept out of every repr and log line), and a test covers it."
      - "The Player never requires the server to be reachable → conforms by analogy: a slideshow plays from its own cache, and an unreachable or refusing Reddit never blanks the screen."
      - "Hardware and network access sit behind an interface → conforms: mpv sits behind the screen-output interface, and Reddit behind the Source interface. Both are doubled in the suite."
      - "Catch specific exceptions; async at the I/O boundary, synchronous core → conforms: the dwell rule, the listing parser and the cache's eviction are pure functions."
      - "No hardcoded deployment values → conforms: the listing URL, the dwell times and the output come from the store or .env."
      - "The e-paper label is legible at standing distance → conforms: slideshow captions go through the same LabelText layout, tiers and floors."
      - "A display device renders its own label, and the label travels as metadata → conforms: the post's fields become a LabelText on this device."
      - "The television belongs to whoever is using it → inapplicable because slideshow mode never touches the Frame (HDMI only, owner 2026-11-10)."
      - "Derived artifacts are regenerated, never transported; heartbeat key → inapplicable because slideshow mode composes nothing and sends no heartbeat (there is no server)."
  - artifact: security-model
    dispositions:
      - "No § Direction (the file says no norm is ratified). Descriptive update owed: § Proportion says the Player 'makes outbound requests to two peers' (the server and the television); in slideshow mode it requests from www.reddit.com and api.redgifs.com, and downloads media from any public host. A new § Authentication subsection records the cookie (sent only to www.reddit.com), and a new § Abuse Prevention subsection records the address guard (SSRF through a listing)."
  - artifact: nonfunctional-requirements
    dispositions:
      - "§ Start-up (no server keys means setup mode) → amendment proposed: a Player whose mode is slideshow needs no server keys. It needs a listing URL and a cookie, and lacking either it serves only the page."
partition: serial — 03 extends 02's source, 04 composes 01–03, and 05 deploys all of it; 01 and 02 are independent, but 01 needs the owner at the Pi, and one builder keeps the plane-isolation guard's two amendments coherent
last_validated: 2026-11-10
---

# Build plan: the serverless slideshow (#27)

## Goals

**Response taken:** asked in batches during discovery on 2026-11-10, with the owner answering each one and watching the Pi tests at the set. Every answer is recorded below as the owner's; everything else is marked *Inferred*.

**Product:** a mode of the Player that needs no Arrt server. It plays a Reddit listing as a slideshow on an HDMI screen, captions each post on the e-paper label, and is set up from the settings page. It is also the first step toward GIFs and video on Arrt walls: the renderer built here is the shared layer, and Arrt mode gets moving media once the contract carries the capability (Arrt #424).

**What the owner asked for (2026-11-10):**
- **The mode covers the whole Player.** The Player runs either from an Arrt server or as a slideshow, never both at once.
- **HDMI only for now.** The Frame comes later.
- **A Reddit listing URL** and a session cookie, set on the settings page, with instructions on the page for copying the cookie from a logged-in browser tab. The user is technical.
- **Images, video and GIFs, all of them. No NSFW filter:** what the URL points at is the owner's choice.
- **From any host a post can link, not just Reddit's own.** The owner named imgur, redgifs and "imglr", a new imgur clone named as an example of "any host" (owner, 2026-11-10). So the requirement is the generic tier, with per-host resolvers only where they beat it.
- **imgur albums show their cover image** (owner, 2026-11-10, accepting the recommendation).
- **Video pages such as YouTube are played too** (owner, 2026-11-10: "yes to video pages"). *Inferred* reading of that answer: the question offered skipping them, and "yes" was read as wanting them. **Built in a follow-on plan (wave 2b), not this one.** See Decisions.
- **Listing order**, looped.
- **A min and max dwell time.** A still shows for min. *Inferred:* the owner did not answer for stills, and min was the recommendation. A clip of length D plays n = ceil(min / D) times, and the total, n·D, is cut at max. The owner's examples, kept as test cases: D = 120, min 30, max 60 → 60 s; D = 18 → two plays, 36 s.
- **Muted.**
- **The label** shows the post title, r/subreddit, u/author and the upvotes.
- **Sources sit behind an interface**, so another source can replace Reddit.
- **mpv is installed by the install and upgrade flow**, not by hand once.
- **The cache** keeps the current listing's media, deletes what drops off the listing, and stops downloading at 2 GB.

**What was measured (2026-11-10; the full record is in `.prawduct/.handoff-notes.md`, and chunk 05 moves it into `hdmi-output-findings.md`):**
- Only `reddit_session` is needed. It is a JWT whose `exp` the page can read: this one was issued 2026-11-08 and expires 2027-05-08.
- With the cookie, `/r/X.json`, `top`, `controversial` and multireddits answered. `new`, `hot` and `rising` returned 403 "Blocked" even with the full cookie. Whether that is a fixed rule by path or bot detection is unknown.
- Media hosts (`i.redd.it`, `v.redd.it`, `preview.redd.it`) need no cookie.
- **Linked hosts.** Over about 600 link posts in 7 listings (r/gifs, r/pics, r/aww, r/NSFW_GIF top-of-month; r/gifs, r/pics, r/EarthPorn top-of-all-time), Reddit's listing carries its own copy of the linked media for every host but one:
  - redgifs (55 posts): all carry `preview.reddit_video_preview`, a Reddit-hosted mp4. Only 27 of them are 720p or better.
  - imgur: 54 `.gifv`, 18 direct images and 7 pages, all with `preview.images`.
  - flickr, twitter, instagram, discord and news sites: all with `preview.images`.
  - Reddit's copy of an imgur image downloaded without the cookie.
  - **gfycat (27 posts) has nothing.** gfycat shut down in 2023, so those posts are unplayable from anywhere.
  - One `i.reddituploads.com` post has nothing either.
- redgifs' `api.redgifs.com/v2/auth/temporary` gave a token with no account. The gif lookup itself was not confirmed (the search call returned no items).
- The rate limit allows about 100 requests per window.
- On the Pi 4 (Trixie, kernel 6.18.34+rpt, mpv 0.40 with Raspberry Pi's ffmpeg): `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m --gpu-hwdec-interop=drmprime-overlay --drm-drmprime-video-plane=primary --drm-draw-plane=overlay` played a 1080p H.264 clip at 4K with 0 drops and 0 failed commits, and the owner saw it play. The same with the planes the other way round failed every atomic commit (EINVAL) while mpv reported 0 drops. The GPU-scaled path dropped about a third of its frames at 4K.
- The account needs group `render`.
- Running at 1080p makes the LG put up its own "not the recommended resolution" dialog.

**Architecture:**
- **One renderer, mpv, behind the existing screen-output seam.** A new `MpvOutput` shows a local file for a given time, through mpv's JSON IPC. The Player owns mpv's process: its own process group, killed on every way out, and a stale one from a crash cleared at start. mpv is never given a URL: it plays only files in the cache, so it can never become a second network client.
- **Arrt mode keeps the KMS output in this wave.** That output is measured and has worked since 2026-10-02. Moving Arrt mode onto mpv is a one-line change to `screen_output()` once Arrt #424 gives it something to play. *Inferred:* this keeps the risk of this wave on the new mode only.
- **A slideshow programme** reads a playlist from a `Source` and plays it through the renderer at the dwell rule. It draws the label through `LabelPanel`. It has no server, pull, supervisor or heartbeat.
- **`Source`** is the interface: it gives an ordered list of items, each with a stable id, a media URL, a kind (still or clip) and label fields. `RedditSource` is the first implementation. The cache downloads ahead of the play position, so playback never waits on the network unless nothing is cached yet.
- **Media are resolved in tiers, best first, falling back on any failure:**
  1. **A host resolver**, where one exists and gives better than Reddit's copy:
     - redgifs, for its HD mp4, through the temporary-token API;
     - imgur `.gifv`, as the same id's `.mp4` on `i.imgur.com`.

     Resolvers sit behind a `Resolver` interface, keyed by domain, so adding a host is one class.
  2. **Reddit's own media**: `i.redd.it`, `v.redd.it` (`fallback_url`) and galleries.
  3. **Reddit's copy of linked media**, which works for any host:
     - `preview.reddit_video_preview` for anything animated;
     - `preview.images[0].variants.mp4` for a GIF;
     - otherwise `preview.images[0].source`.
  4. **A direct media URL** (the post's `url` ends in a media extension): fetched from its host.

  A post nothing resolves is skipped and counted by domain, in the journal and on the page, so a host worth a resolver shows up by name. That count is how the "imglr"-type gaps become visible.
- **Mode** is a key in the settings store (`PLAYER_MODE`: `arrt` or `slideshow`). Saving it restarts the Player, as every save does.

**Constraints:**
- **One HDMI output in slideshow mode.** Both Pi 4 HDMI connectors are on one DRM card, and only one process can be its master (`kms.py` docstring). So one mpv can drive only one connector. The page lists the connected HDMI outputs and the owner picks one. *Inferred* from the measured architecture; the owner's "one or more" is narrowed by it, and only one screen is connected today.
- **What mpv reports is not evidence of what is on screen.** mpv reported 0 drops while every commit failed. So `MpvOutput` treats mpv's own failure lines (`Failed to commit atomic request`, a VO that fails to start) as "not shown", logs them by name and restarts mpv. It never trusts the drop counter alone. (Learning: confirm a boundary library's success against the remote system's own state.)
- **The cookie goes only to `www.reddit.com`.** Media are fetched without it. The cookie is write-only on the page and never logged or put in a repr.
- **A post can name any URL, so a download must not be able to reach the LAN.** A post linking `http://192.168.1.1/…` would otherwise make the Player, which sits on the home LAN, fetch from the router. That is SSRF through a listing. So the downloader:
  - resolves each host and refuses loopback, private, link-local, multicast and reserved addresses, checking every redirect hop the same way;
  - follows at most 5 redirects;
  - never sends the Arrt server's address or token;
  - caps a single file at 200 MB (*Inferred*);
  - accepts only image and video content types, checked against the bytes, not just the header.

  mpv still plays only files from the cache.
- **A refused listing and a cookie Reddit no longer accepts are different states with different messages.** The page suggests `top` for the first and a new cookie for the second. *Corrected 2026-11-10 by measurement (chunk 02 verify-api):* both answer `403` with an HTML page, and so does no cookie at all; nothing answered `401` or redirected to log in. So after a `403` the source asks `/api/me.json`, which answers `200` with the account (`kind` `t2`) for a session Reddit accepts and `403` otherwise. Only `kind` is read.
- **The page warns about the cookie's expiry** two weeks before its `exp`. *Inferred:* the margin. Whether Reddit renews the cookie is unmeasured, and chunk 05 checks it on the Pi's copy.
- **The settings layout becomes version 2:** v1 plus the slideshow keys. A v1 file is read as v2. An older Player refuses a v2 file and names the version. *Inferred:* the store already refuses any unknown key, so without a version bump an older Player would refuse the file anyway, with a less helpful message. Rolling back across this wave means removing the slideshow keys by hand, and `deploy/README.md` says so.

**Decisions:**
- [DECISION: the plane-isolation guard gets a second named exception, in two parts. (a) One new module, `src/arrt_player/sources/fetch.py`, may open an aiohttp client. It is the only network code in `sources/`, and listing and API calls go only to a constant set of hosts the guard reads (`www.reddit.com`, `api.redgifs.com`). (b) Its media download may reach any public host, through the address guard in Constraints. It imports nothing from `pull.py` or the contract, and only the slideshow's composition imports it. Every other module is held as before, and planted violations prove the guard still fails. | The norm exists to stop a quiet second channel to the *Arrt server*. A slideshow has no server, and the owner wants media from any host a post can link (2026-11-10). That makes a fixed host list for media impossible. So the exception is bounded by what a download can reach (public addresses only, never the server) rather than by a list. | owner asked for it in #27 and widened it on 2026-11-10; can veto the shape]
- *Inferred:* **video pages (YouTube and the like) are wave 2b, a plan of their own drawn when this one merges.** The owner wants them. They need yt-dlp, a dependency unlike anything else here: it changes weekly, video sites break it, and the upgrade flow has to keep it current. Five chunks is already this plan's size limit (about three to four sessions). Until 2b lands, video-page posts are counted by domain like any other unresolvable post. Reddit's thumbnail copy is not shown in their place, because a still from a video reads as a broken slide. Measured for 2b on 2026-11-10: yt-dlp 2026.10.26 resolved a public YouTube video from the owner's Mac with no bot block, and listed H.264 video-only formats up to 1080p over https. The design to carry forward:
  - yt-dlp resolves only; `fetch.py` downloads, so the address guard still holds;
  - H.264 at or below 1080p, video-only;
  - only the first max-dwell seconds are fetched, if the stream turns out to be fragmented MP4 (unmeasured).
- An imgur album shows only what Reddit's copy holds, which is its cover image (owner accepted). The whole album needs imgur's API with a registered Client-ID, an account step for the owner.
- *Inferred:* gfycat posts are skipped and counted. The host is gone.
- *Inferred:* the listing is refreshed every 10 minutes, one page of up to 100 posts. After a refresh, play continues from the post after the current one when that post is still listed, and from the top otherwise.
- *Inferred:* a gallery shows each of its items in `gallery_data` order, each as its own slide with the same caption. A GIF plays as Reddit's mp4 rendition (`preview.images[0].variants.mp4`, or `media_metadata[...].s.mp4` in a gallery), never as the GIF itself.
- *Inferred:* a clip's length D is read from mpv (`duration`) once the file is loaded, rather than trusted from the listing, because GIF renditions carry no duration there. A clip that reports no duration is shown as a still.
- *Inferred:* dwell limits are 5 s ≤ min ≤ max ≤ 3600 s. The page refuses anything else.

**Tradeoffs accepted:**
- 1080p clips are upscaled by the display hardware on a 4K screen. The owner saw this and accepted it ("looks much better than before. upscaled").
- Video and GIFs are not available in Arrt mode until Arrt #424 lands.
- Reading Reddit with a session cookie is probably outside its terms for automated clients. The risk for one household's wall is a rate limit or a revoked cookie. Noted, not blocking (owner).
- mpv brings 148 packages onto the Pi.

**Decisions made mid-build:**
- Chunk 01: mpv answers `property unavailable` for a `duration` it does not have (measured on the Pi, mpv 0.40). That reply is read as "no length", and the item shows as a still, as the Decision above says; it is not a failure. Settled by the plan's own Decision. The chunk review found the gap.
- Chunk 01: the plan's "a cancelled task leaves no mpv" test moves to chunk 04, the first code that calls the renderer from asyncio. Cancellation leaves a `with` block exactly as an exception does, and that case is tested. Settled by the plan's Partition (04 composes the renderer). The chunk review raised it.
- Chunk 03: **resolvers run at download time, not when the listing is read.** Each item carries its post's link and domain. The cache asks for candidates in order: the host resolver's URL first, then Reddit's copy. It keeps the first that downloads and logs the tier that won. Two measured reasons (2026-11-10): (1) a resolver's answer can be checked only by downloading it, since a deleted imgur id's `.mp4` redirects to imgur's HTML home page with a 200, which the downloader's content check refuses; (2) resolving at listing time would spend one redgifs API call per redgifs post on every refresh. A post Reddit carries nothing for, but which a resolver claims (an imgur `.gifv` with no moving copy), becomes an item whose only candidate is the resolver's. Settled by "do it right" and the plan's "any failure falls back".
- Chunk 03, measured 2026-11-10: the redgifs temporary token is a JWT that lasts 24 h (`exp`), bound to the caller's address and user agent (`valid_addr`, `valid_agent`). A gif lookup answers 200 with `gif.urls` {hd, sd, silent, poster, thumbnail, html}; hd is an unsigned `media.redgifs.com/<name>.mp4` (`video/mp4`, ftyp `M4V `). A missing gif is 404 `GifNotFound`; a missing or bad token is 401. No rate-limit headers were seen. Post links are `www.redgifs.com/watch/<id>`, with lowercase ids. imgur `<id>.gifv` → `i.imgur.com/<id>.mp4` answered 200 `video/mp4`, or 301 to the same id in another case.
- Chunk 03, measured live on the Mac 2026-11-10: 6 redgifs posts all came through `host:redgifs` at 1080p to 1920p, and 6 imgur `.gifv` posts through `host:imgur`. That includes one with no moving copy on Reddit (1696p), and originals of 20 s where Reddit's copies were cut at 15 s. **Open, for chunk 05's Pi check:** several redgifs HD files are taller than 1080 (a 1080x1920 portrait, a 1428p). The Pi 4's H.264 decoder is generally documented to top out around 1920x1080, which is *recalled, not measured*. If those files fall back to software decoding and stutter, the redgifs resolver should prefer `sd`, or Reddit's copy, above that size. Chunk 05 plays one on the Pi and decides.
- Chunk 03: `Fetcher.get_api` takes headers (the redgifs `Authorization`). A caller-supplied `Cookie` header is refused, so the cookie rule cannot be bypassed through it.
- Chunk 02: the plan's "a 401 or a redirect to login means the cookie is no good" was wrong (measured; see Constraints). A refused listing is told apart from a refused cookie by one `/api/me.json` call after a `403`. Settled by "do it right": the owner asked for distinct messages, and the status alone cannot give them.
- Chunk 02, measured: about 1,000 posts over 10 listings had no animated gallery entry and no media URL lacking Reddit's copy. Those two readings are unmeasured, are tested on cases derived from real posts, and say so. Reddit refused `/search.json` (403), as it does `/new`.
- Chunk 02, *Inferred*: an imgur `.gifv` with no moving copy on Reddit is unresolved, not shown as Reddit's still of it, by the same rule as video pages. Chunk 03's imgur resolver gives it its mp4.
- Chunk 02: the cache is `src/arrt_player/sources/cache.py`, not `media_cache.py`. It needs `fetch.py`'s error types, and the guard's rule stays simplest as "only `sources/`, the slideshow programme and `__main__` import `fetch.py`". Settled by the plan's own guard Decision (bounded importers).
- Chunk 02: the address guard is enforced in the resolver aiohttp connects through, so a name cannot resolve public when checked and private when connected (DNS rebinding). aiohttp skips the resolver for an address written into the URL (`aiohttp/connector.py` `_resolve_host`, 3.14, read), so those are checked separately, through `yarl`, the parser aiohttp itself uses. `yarl` is now declared. IPv4-mapped and NAT64 addresses are judged by the IPv4 address they carry. Settled by "do it right".
- Chunk 02, *Inferred*: a download that fails is not tried again for 30 minutes, and the fill moves on. Three unreachable items in a row mean the network is down: the fill stops and holds none of them back. Not settled by the goals; inferred from "an unreachable Reddit never blanks the screen" and "one bad item never stops the rest".
- Chunk 02: `test_mpv.py::test_refresh_puts_the_still_back_after_mpv_died` (from chunk 01) failed 8 times in 12 runs on 2026-11-10, including on the Linux runner at bc1f49d. The test assumed mpv would be seen dead within two refreshes. *Inferred cause:* while a killed multithreaded mpv's threads are still exiting, its socket stays open and silent. The test now asserts the promise instead, a restart within bounded refreshes past the longest backoff, and it fails against a refresh that never restarts.
- Chunk 02: `test_a_screen_failure_that_arrived_after_the_last_check_is_not_lost` (chunk 01) failed 1 of 4 runs on the Linux runner. Cause, read from the failure: the fake mpv failed commits the instant a file loaded, so a first `load` could itself be lost and the next load met the backoff. The real mpv's failures began 0.86 s after start, so the fake now waits `COMMIT_FAIL_ONSET_S` (0.2 s) first. This is also the likely cause of the 1-in-22 flake recorded for `test_failed_commits_with_no_dropped_frames_count_as_not_shown`.
- Chunk 04: `listing_url` and the cookie checks moved to `sources/reddit_settings.py`, which imports no network code. `config` and the page judge `SLIDESHOW_URL` and `REDDIT_SESSION` with it, and importing `reddit.py` would have brought `fetch.py`'s client into both. The page reads the slideshow's state from `programmes/slideshow_status.py` for the same reason. Settled by the plan's guard Decision (bounded importers).
- Chunk 04, *Inferred*: **the last listing is kept on disk** (`$CACHE_DIR/.slideshow-listing.json`) and played at start until Reddit answers. Every save on the page restarts the Player, and without the file a restart during a Reddit outage would leave cached media with no items or captions, so the screen would blank. Inferred from "an unreachable or refusing Reddit never blanks the screen".
- Chunk 04, *Inferred*: a listing that answers with nothing playable in it is not taken: no eviction and no blank screen. It is logged as `slideshow.listed_nothing`. Same root as the entry above.
- Chunk 04, *Inferred*: the dwell defaults are min 30 s and max 60 s, the values in the owner's own examples. The output defaults to `hdmi-a-1`. A failed listing is asked for again after 120 s, or after Reddit's `x-ratelimit-reset` if that is longer. The next item after one not shown waits 5 s, the dwell floor.
- Chunk 04: **the caption fields.** The label's fields are named for an artwork, so a post goes in by reading order (`LabelText.candidates`): u/author → `artist`, title → `title` (italic), r/subreddit → `date_created`, ↑score → `medium`. The last two are the optional facts a small surface drops first. Pinned through the real `LabelText` in `tests/test_slideshow.py`.
- Chunk 04: the plan's "a cancelled task leaves no mpv" test (carried from chunk 01) is `tests/test_slideshow.py::test_a_cancelled_slideshow_leaves_no_mpv`, through the real renderer and the fake mpv. The renderer is closed on a worker thread, because a load in flight holds its lock.
- Chunk 04: a cookie pasted as `reddit_session=<value>` is taken without the name. Settled by "do it right": that is how a browser's cookie panel shows it.
- Chunk 04: the settings-file test that refused `{"version": 2}` now refuses `{"version": 3}` (and `true`). Its promise, that a layout this Player does not know is refused by name, is unchanged; version 2 is now the layout it writes.
- Chunk 04, measured on the Mac 2026-11-10: the real Player went from setup mode to slideshow mode through the page (password set, slideshow saved, the Player restarted itself, and the next start read the saved keys). It then played `r/gifs/top/?t=month` with the renderer doubled by a stand-in that reads each file's length with ffprobe: 47 items listed, 0 unresolved, 47 downloaded (45 Reddit's copies, 2 `host:imgur`), 13 shown in 90 s at the dwell rule (a 3.07 s clip for 6.13 s, a 1.36 s clip for 5.44 s). The cookie appeared in neither journal nor in any page or save answer. The JWT reader gives the real cookie's expiry as 2027-05-08, matching the measurement above.
- Chunk 01: failures split into screen failures (mpv stopped, restart backoff) and file failures (that item only), so one bad file never restarts mpv or delays the next item. Not settled by the goals; inferred from "a screen held by another program must not spin".

**Level:** Medium-High. Every boundary was probed live: Reddit with the owner's cookie, mpv on the Pi with the owner watching. What remains unmeasured is named: stills and transitions under the plane-swapped settings (chunk 01's first Pi check), whether the 403s are a fixed rule by path, and whether the cookie is renewed. To raise it: chunk 01's Pi check.

## Scaffolding

### Dependencies

- **mpv** (Debian trixie `mpv` 0.40.0-3+deb13u1), a system package, installed with `--no-install-recommends`. The Player starts it as a separate process and talks to it over a Unix socket. There is no Python binding: the IPC is JSON lines over stdlib `socket` or `asyncio`. *Alternative considered:* python-mpv (libmpv through ctypes), which would put mpv in the Player's own process, so a crash in mpv would take the walls with it.
- No new Python dependencies. aiohttp is already pinned, and the stdlib covers `base64`, `json` and `math`.

### Verification Strategy

- **Each chunk:** `testament run --json` (else `uv run --group raster pytest`), ruff, black.
- **The renderer:** a fake mpv that speaks the IPC protocol over a real Unix socket, and can be told to fail the way the real one did (commit failures with a 0 drop count; a VO that never starts). Then the Pi with the owner watching: stills, still→clip→still, a GIF loop, portrait 720p and landscape 1080p, at 4K.
- **The source:** listing fixtures saved from the live probes (public post data only, no cookie), covering an image, a gallery with an animated item, a GIF, a v.redd.it video, an external link and a crosspost. Then one live fetch through the real module on the Mac with the owner's cookie.
- **The mode:** the whole Player started on the Mac in slideshow mode with a scratch `CACHE_DIR`, the renderer doubled, a real listing fetched and the cache filled. Then on the Pi in chunk 05.

## Build Chunks

### Chunk 01: The mpv renderer and the dwell rule

- **Description:** the renderer, built and proven on the Pi before anything uses it.
  - new `src/arrt_player/mpv.py`: start mpv with the measured options, connect over IPC, show a file for a dwell (`loadfile replace`, with `loop-file` for a clip that loops), read `duration`, and watch mpv's log for the failure lines named in Constraints. It owns the process group, with every exit path covered and a stale mpv from a crash cleared at start.
  - new `src/arrt_player/dwell.py`: the dwell rule as a pure function (still → min; clip → min(ceil(min/D)·D, max); no D → still).
  - The renderer meets the screen-output seam, so a later Arrt-mode switch is the one-line change Architecture names.
- **Depends on:** none
- **Foreign API:** mpv's JSON IPC and command line (0.40)
- **Artifacts consumed:** this plan's Goals (measured options, constraints), `.prawduct/artifacts/hdmi-output-findings.md`
- **Deliverables:** new `src/arrt_player/mpv.py`, new `src/arrt_player/dwell.py`, new `tests/fake_mpv.py`, new `tests/test_mpv.py`, new `tests/test_dwell.py`, and new `tools/mpv_check.py` (a hand tool for the Pi that runs one sequence through `MpvOutput`, as `tools/panel_check.py` does for the panel).
- **Tests:**
  - the dwell rule over the owner's examples, the 25-s case (two plays cut at max 40), D > max, D = min exactly, and no D; plus a property: the result is always ≥ min unless it is cut at max, and always ≤ max;
  - the renderer against the fake: the commands sent for a still and a clip; `duration` read; a clip looped;
  - the fake failing commits while reporting 0 drops → `MpvOutput` says "not shown", logs it by name and restarts mpv; watch this test fail against a renderer that trusts the counter;
  - a VO that never starts → not shown, named;
  - mpv dying mid-show → restarted, and the item counted as not shown;
  - every exit path (stop, an exception, a cancelled task) leaves no mpv process; a stale socket and process from a "crash" are cleared at start;
  - mpv is never handed anything but a path under the cache (a URL is refused before it reaches mpv).
- **Acceptance criteria:**
  - On the Pi at 4K, with the owner watching, through `tools/mpv_check.py`: stills, still→clip→still, a GIF loop, 720p portrait and 1080p landscape all show, with no console between items and no failure lines.
  - The suite passes.
- **Visual change:** yes. The owner watches the Pi run.
- **Done when:**
  0. verify-api: confirm the IPC commands and properties used (`loadfile`, `loop-file`, `duration`, `quit`, the log lines) against mpv 0.40 on the Pi, reading the real output, before the fake is written.
  1. Acceptance criteria met and the suite passes
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed

### Chunk 02: The Reddit source, the safe downloader and the media cache

- **Description:** the `Source` interface, Reddit behind it, the one network module, and the cache.
  - new `src/arrt_player/sources/__init__.py`: `Source`, `Item` (id, media URL, kind, caption fields) and `Resolver`.
  - new `src/arrt_player/sources/fetch.py`: the one module allowed a client.
    - Listing and API calls go only to the guard's constant host set.
    - Media downloads may reach any public host, through the address guard in Constraints: every hop is resolved and checked, at most 5 redirects, the size cap, and content sniffing.
    - The cookie is attached only to `www.reddit.com`.
  - new `src/arrt_player/sources/reddit.py`: the listing parsed into items through tiers 2–4 (Reddit's own media, Reddit's copy of linked media, direct media URLs); unresolvable posts counted by domain; outcomes kept distinct: refused (403), cookie no good (a 403 that `/api/me.json` confirms; see Constraints), rate-limited (429, honouring `x-ratelimit-reset`) and unreachable.
  - new `src/arrt_player/sources/cache.py` (planned as `media_cache.py`; see Decisions made mid-build):
    - downloads ahead of the play position, through `fetch.py`;
    - files are named by item id;
    - eviction keeps the listing's items and deletes the rest;
    - a 2 GB cap stops downloads;
    - an atomic write, so a half-downloaded file is never played.
  - The plane-isolation guard's second exception, with planted violations.
- **Depends on:** none (separate from 01; they meet in 04)
- **Foreign API:** Reddit's listing JSON (`/…/.json?raw_json=1`)
- **Artifacts consumed:** this plan's Goals, `.prawduct/artifacts/security-model.md`
- **Deliverables:** the modules above; new `tests/fixtures/reddit/*.json`; new `tests/test_reddit_source.py`; new `tests/test_fetch.py`; new `tests/test_media_cache.py`; and the guard amendment in `tests/test_plane_isolation.py`.
- **Tests:**
  - each post shape resolves to the right items through the right tier, with one fixture member per shape that would make a wrong parse visible:
    - a gallery whose order differs from `media_metadata`'s key order;
    - a GIF post whose `url` is the .gif;
    - a redgifs post (its `reddit_video_preview`);
    - an imgur `.gifv`;
    - a flickr image;
    - a gfycat post (nothing playable, counted);
    - a YouTube link (counted, not shown as a still);
  - each HTTP outcome maps to its own state, and a 403 is never reported as an expired cookie;
  - the cookie is sent to `www.reddit.com` and to no other host, and appears in no log record or repr;
  - the address guard refuses each private range, a public name that resolves to a private address, and a redirect from a public host to a private one; it admits a public address (learning: a fixture that could falsify the claim);
  - the size cap and the content check stop a download partway, and leave no file;
  - eviction under a listing that changes between runs (learning: vary the inputs between runs);
  - the cache cap stops downloads and is counted in bytes on disk;
  - the guard fails a planted client in another module, a planted API host outside the set, and a planted import of `fetch.py` from anywhere but `src/arrt_player/sources/` and the slideshow's composition.
- **Acceptance criteria:**
  - A live fetch through `RedditSource` on the Mac, with the owner's cookie, over a listing that includes linked hosts, lists items whose media download and open.
  - The unresolved count names its domains.
  - The suite passes.
- **Done when:**
  0. verify-api: re-probe the listing shapes with the owner's cookie (a gallery with an animated item, a crosspost, a v.redd.it post, redgifs, imgur and flickr links), and save the fixtures from the real responses before writing the parser.
  1. Acceptance criteria met and the suite passes
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed

### Chunk 03: Host resolvers: redgifs HD and imgur

- **Description:** tier 1, for the hosts where Reddit's copy is worse than the original.
  - new `src/arrt_player/sources/resolvers/redgifs.py`: a temporary token from `api.redgifs.com/v2/auth/temporary`, cached until it expires; the gif's id from the post URL; its `hd` mp4 from `/v2/gifs/{id}`. Any failure falls back to Reddit's copy.
  - new `src/arrt_player/sources/resolvers/imgur.py`: `.gifv` becomes the same id's `.mp4` on `i.imgur.com`, and a direct image is taken as it is. A missing `.mp4` falls back to Reddit's copy.
  - The resolver registry keyed by domain, so the next host is one class.
  - The page and the journal show which tier each item came from, so a resolver that quietly falls back all the time shows up.
- **Depends on:** Chunk 02
- **Foreign API:** the redgifs v2 API; imgur's direct media URLs
- **Artifacts consumed:** this plan's Goals
- **Deliverables:** the resolvers, new `tests/test_resolvers.py`, and fixtures from the real redgifs responses.
- **Tests:**
  - each resolver's success and each of its failures (no token, token expired, gif gone, rate-limited) falls back to tier 2 or 3, and says so;
  - the token is reused until it expires, then renewed (learning: advance the clock by amounts that are not multiples of the interval);
  - a resolver is used only for its own domain;
  - redgifs and imgur fetches carry no Reddit cookie.
- **Acceptance criteria:**
  - On the Mac, a live listing with redgifs posts gets HD where the API answers, and Reddit's copy where it does not, with the tier logged.
  - The suite passes.
- **Done when:**
  0. verify-api: confirm the redgifs token, the gif-lookup response shape and the `urls` keys against the live API, and imgur's `.gifv` → `.mp4`, before the fakes are written.
  1. Acceptance criteria met and the suite passes
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed

### Chunk 04: The slideshow mode and its page

- **Description:** the mode, end to end.
  - The slideshow programme: playlist from the source; the cache ahead; the renderer at the dwell rule; the label drawn per item (title → `title`, u/author → `artist`, r/subreddit and ↑score as further facts; the field mapping is checked against `LabelText.candidates()`).
  - Refreshes on the interval, keeping the play position.
  - Plays from the cache when Reddit answers anything but a listing.
  - Settings:
    - layout version 2, with `PLAYER_MODE`, `SLIDESHOW_URL`, `REDDIT_SESSION`, `SLIDESHOW_MIN_SECONDS`, `SLIDESHOW_MAX_SECONDS` and `SLIDESHOW_OUTPUT`;
    - config validates them the way the page does.
  - Start-up:
    - in slideshow mode the server keys are not required;
    - a missing URL or cookie means setup mode, naming what is missing;
    - Arrt mode is unchanged.
  - The page:
    - the mode switch and the fields;
    - the connected HDMI outputs to choose from;
    - the cookie write-only, with its expiry date read from the JWT and a warning within two weeks of it;
    - instructions for copying `reddit_session` from DevTools;
    - the source's last outcome, in words (refused, cookie no good, rate-limited, unreachable).
  - The journal: `slideshow.*` events for each item shown or skipped and each refresh outcome, with the cookie in none.
- **Depends on:** Chunks 01, 02 and 03
- **Artifacts consumed:** `.prawduct/artifacts/build-plan-settings-ui.md` (the store and the page), this plan's Constraints
- **Deliverables:**
  - new `src/arrt_player/programmes/slideshow.py`;
  - the composition in `src/arrt_player/__main__.py`;
  - `src/arrt_player/config.py` and `src/arrt_player/settings_ui/` (store v2, page);
  - `.env.example`;
  - the NFR § Start-up amendment and a new security-model section.
- **Tests:**
  - the programme against a doubled source and renderer: order; dwell applied per item; a refresh that reorders, drops the current post, or fails; an item not shown is skipped, not retried in a tight loop; and a Reddit outage plays on from the cache (the test cuts the source mid-run);
  - the label: per item, through the real `LabelText` (learning: test through the caller with non-default values);
  - the resolvers are wired at both ends: `RedditSource(claims=resolvers.claims)` and `MediaCache(candidates=resolvers.candidates)`. Tested through the composition, so that an imgur `.gifv` with no copy on Reddit gets its mp4 and a redgifs post gets `host:redgifs`. Wiring only one end leaves items with nothing to fetch, or resolvers that never run (chunk 03's review, observation 3);
  - config: slideshow mode without server keys starts, a missing cookie is setup mode, and Arrt mode still needs its keys;
  - the store: a v1 file reads as v2; an unknown version is refused by name;
  - the page:
    - the cookie never in a response body;
    - an expiry warning shown inside the margin and absent outside it (learning: assert absence where it must not appear);
    - min > max refused;
    - each source outcome rendered in its own words;
  - no cookie in any log record from a whole slideshow run.
- **Acceptance criteria:**
  - On the Mac, the Player started in slideshow mode with a scratch `CACHE_DIR` fetches the owner's listing, fills the cache, and steps through items at the dwell rule with the renderer doubled.
  - The page sets every key and comes back after the restart.
  - The suite passes.
- **Visual change:** yes. The owner looks at the page once.
- **Done when:**
  1. Acceptance criteria met and the suite passes
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed

### Chunk 05: The install flow, the Pi, and the record

- **Description:** the install and upgrade flow, and the deploy.
  - `deploy/display.service`: add `render` to `SupplementaryGroups`.
  - `deploy/README.md`:
    - `apt-get update && apt-get install --no-install-recommends mpv` and `adduser tvpi render` in the setup steps;
    - the slideshow mode and the cookie;
    - rolling back across the store's v2.
  - `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`: the apt step and a check that mpv is present.
  - The Player at start in slideshow mode: mpv missing, or the account lacking `render`, is named in the journal and on the page. It does not crash-loop.
  - The records:
    - the measurements moved from the handoff notes into `.prawduct/artifacts/hdmi-output-findings.md`;
    - `.prawduct/artifacts/architecture.md` (mpv as a process, the second client);
  - `.prawduct/artifacts/platform-and-dependency-findings.md` (mpv as a system dependency: version, the 148 packages, the `render` group, the measured options);
  - `.prawduct/artifacts/observability-strategy.md` (the `slideshow.*` and renderer events);
    - the change fragment.
  - The deploy on the Pi, by the runbook, with the owner watching.
- **Depends on:** Chunk 04
- **Artifacts consumed:** `deploy/README.md`, `.prawduct/runbooks/moving-the-pi-player-to-a-new-revision.md`, `.prawduct/artifacts/architecture.md`
- **Deliverables:** the records above, current with the code.
- **Tests:**
  - `tests/test_deploy_unit.py` checks the unit's `render` group;
  - the start-up check names a missing mpv and a missing group.
- **Acceptance criteria:**
  - On the Pi: switch to slideshow from the page; the owner's listing plays on the LG at 4K with captions on the panel; switch back to Arrt from the page and the wall returns.
  - The cookie's renewal is checked: is the `exp` on the Pi's copy later than when it was pasted?
- **Type:** cumulative-final
- **Done when:**
  1. Acceptance criteria met and the suite passes
  2. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved

## Early Feedback Milestone

**Milestone chunk:** 01
**What the user can do:** watch the Pi play stills, a GIF and both clips through the Player's own renderer at 4K, and say whether it looks right before anything is built on it.

## Governance Checkpoints

**Commit & PR cadence:** commit per chunk after its review. The cumulative review after chunk 05 makes the branch ready for a PR, which is created when the owner asks.

- After chunk 01: the owner's look at the Pi decides whether the renderer's shape holds. If stills or transitions fail under the plane-swapped settings, that is a design change, made here before 04 builds on it.
- After this wave: Arrt mode moves onto mpv and reports moving media once Arrt #424 is tagged. A backlog item records it.
- After this wave: wave 2b, video pages through yt-dlp, gets its own plan and branch. A backlog item records it, carrying the measurement and design notes in Goals › Decisions.
