# Moving the Pi's Arrt Player to a new revision

Owner: the operator · Last executed: 2026-10-27, § The first time, F1–F7, from
the monorepo checkout to f7937da in the standalone one (F8 is the owner's call
and the way back, F9, was not exercised). § Steps last ran 2026-11-10,
f7937da → 9ca66b2, in the standalone checkout: **steps 1–6, 8, 9 and 12–16 are
the only ones that have run on a Pi.** Every other step in this runbook has no
recorded run on a Pi, whenever it was written: 7 and 10–11 (that release had no
required new setting and no unit change), every lettered step (6a, 7a, 7b, 7c,
9a, 12a, 13a, 13b, 14a, 14b, 14c, 19a, 19b) and all of § Going back. Read their
*Expected* lines as what the code and systemd's documentation predict, not as
output anybody has seen; a step that prints something else may be the runbook's
mistake, not the release's. That release's dated section had one Pi step,
checking `.env` against the new start-up refusals, and it ran between steps 8
and 12.

## When to use this
You have a newer commit of this repo and the Pi beside the wall runs an older
one. Check on the Pi which checkout the running unit uses:

    systemctl show -p WorkingDirectory display.service

- `WorkingDirectory=/opt/arrt-player` → read the commit with
  `sudo -u tvpi git -C /opt/arrt-player rev-parse --short HEAD` (as your own
  user, git refuses another account's checkout as "dubious ownership"). If it
  already prints the commit you want → nothing to do. Otherwise → § Steps.
- `WorkingDirectory=/opt/samsung-frame-art-loader/arrt-player` → the Pi still
  runs from the checkout of Arrt's repository both products shared before
  2026-10-26. That checkout cannot reach this repo's commits → § The first time,
  then stop: it ends on a running Player.

**If the server on the NAS is also moving:** do it first (the homelab repo's
Arrt deploy runbook), unless step 2 says this release goes Player-first.

## The first time: off the monorepo checkout

Once per Pi. **`/opt/arrt-player` is the path `deploy/display.service` names**
(confirmed by the owner 2026-10-26). If the path changes, the unit and this
section change together first.

Nothing here touches the old checkout, so it stays the way back (F9).

F1. Stop the Player, as in step 3, and check it as in step 4.

F2. Clone this repo as the account that runs it:

        sudo install -d -o tvpi -g tvpi /opt/arrt-player
        sudo -u tvpi git clone https://github.com/brookstalley/arrt-player /opt/arrt-player
        sudo -u tvpi git -C /opt/arrt-player checkout --detach <new>

    **Expected:** `HEAD is now at <new>`.

F3. Copy the settings across, keeping their owner and mode:

        sudo install -m 0640 -o tvpi -g tvpi /opt/samsung-frame-art-loader/.env /opt/arrt-player/.env

    *Why copy and not move: the old checkout's unit reads the old file, and F9
    starts it again.* `CACHE_DIR` and the token file are absolute paths, so the
    cache and the pairing carry over without moving anything.

F4. Compare the copied settings with the new checkout's example. Step 2's
    `git diff` cannot do this, because the old commit is in Arrt's history and
    not this repo's:

        diff <(sudo grep -oE '^[A-Z_]+' /opt/arrt-player/.env | sort -u) \
                  <(grep -oE '^#? ?[A-Z_]+=' /opt/arrt-player/.env.example | tr -d '#= ' | sort -u)

    **Expected:** `<` lines are keys the server reads and the Player does not.
    They can stay. `>` lines are keys the Player may read and `.env` lacks. Read
    each one in `.env.example`, and add it with
    `sudoedit /opt/arrt-player/.env` if it says it is required.

F5. Sync the environment, as in step 8.

F6. Check `.env` as in step 9a (if it prints a line, go to F9), then install
    the unit and reload systemd:

        sudo cp /opt/arrt-player/deploy/display.service /etc/systemd/system/
        sudo systemctl daemon-reload

F7. Steps 12–16 and steps 13a and 14a to 14c, except that where one of them sends you to step
    17, go to F9 instead. Then check the unit runs from the new checkout:

        systemctl show -p WorkingDirectory display.service

    **Expected:** `WorkingDirectory=/opt/arrt-player`.

F8. Leave `/opt/samsung-frame-art-loader` in place until the owner says the
    move held. Removing it is the owner's call, and it is the only way back.

F9. **Going back:** stop the Player, reinstall the old unit, reload, start:

        sudo systemctl stop display.service
        sudo cp /opt/samsung-frame-art-loader/deploy/display.service /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl start display.service

    **Expected:** step 14's two lines, and
    `WorkingDirectory=/opt/samsung-frame-art-loader/arrt-player`.

## Steps

**On the Pi** unless a step says otherwise.

1. Write down the commit from *When to use this*. It's where you come back
   to (step 20). For example: `81e2d15e`.

2. **On your Mac**, in this repo, read what the release asks of a deployment:

       git diff <old>..<new> -- deploy/README.md .env.example

   `<old>` is step 1's commit and `<new>` is the one you're deploying, for example
   `git diff 81e2d15e..58cd1785 -- …`.
   **Expected:** new dated sections, or nothing.
   **IF a new section names a step for the Pi or an order:** follow it where it
   says. Its instructions win over this runbook.
   **IF `.env.example` gained a key the Player reads:** you'll add it at step 7.

3. Stop the Player:

       sudo systemctl stop display.service

   *Why: it sends SIGTERM and waits up to 90 s. Never kill it. A hard stop
   leaves the TV holding the art channel for minutes.* The screen shows the
   text console while the Player is stopped, and that's expected.

4. Run `systemctl is-active display.service`.
   **Expected:** `inactive`.

5. Fetch:

       sudo -u tvpi git -C /opt/arrt-player fetch origin

6. Check out the new commit:

       sudo -u tvpi git -C /opt/arrt-player checkout --detach <new>

   **Expected:** `HEAD is now at <new>`.
   **If not:** `error: Your local changes … would be overwritten`. Someone
   edited the checkout on the Pi. Nothing has changed yet → step 12 to start
   the old revision again, then find out what the edit was before going on.

6a. **IF step 2 showed `deploy/README.md` § The television's identity, a
    watchdog and the page's Frame address, and a Frame's address was ever moved
    to a different television:** delete that wall's store before starting the new
    revision. Nothing on the Pi records which set the old rows were made on, and a
    store from before this release takes the first set it meets as theirs.

        sudo grep '^CACHE_DIR=' /opt/arrt-player/.env
        sudo -u tvpi find <that directory> -maxdepth 2 -name 'display-state.sqlite*'
        sudo -u tvpi rm -f <that directory>/<wall id>/display-state.sqlite{,-wal,-shm}

    **Expected:** `find` names one store per Frame wall, with its `-wal` and
    `-shm` files if the Player stopped with them; the `rm` removes the three of
    the wall whose set changed. The cost is a re-upload of that wall's works, and
    **the pictures the old store put on that set stay on it for good**: the new
    Player has no record of them, so it leaves them alone and uploads every work
    again beside them. The household can delete the extra copies on the set. **If
    the Frame has always been the same television, skip this step.** *Why: a store
    from before this release takes the first set it meets as the one its rows
    were made on and records their ids as pictures the Player put there, matched
    by id alone, so a household picture that happens to carry one would be
    removed; and a store that names the wrong set's ids would also select by
    them.*

7. **IF step 2 showed a new Player setting:** add it.

       sudoedit /opt/arrt-player/.env

   *Why `sudo`: `.env` is readable by `tvpi` and root only.*

7a. Check the system packages the Player runs beside it:

        mpv --version | head -1

    **Expected:** `mpv 0.40…` or later.
    **If `command not found`:** install it (`deploy/README.md` § The slideshow):

        sudo apt-get update && sudo apt-get install --no-install-recommends mpv

    *Why: slideshow mode draws through mpv, and nothing else on the Pi brings
    it. Arrt mode does not need it yet, but a Pi switched to a slideshow from
    the page should not have to wait for an install.*

7b. **IF step 2 showed `deploy/README.md` § Two things that can stop an
    upgrade**, check that `.env` holds none of the zero intervals it names:

        sudo grep -nEi '^[A-Z_]+_SECONDS=["'"'"']?(0+(\.0*)?|-.*|nan|inf.*)["'"'"']?[[:space:]]*$' /opt/arrt-player/.env

    **Expected:** nothing printed.
    **If it prints a line:** set that key to the number of seconds meant, or
    delete it for the default, with `sudoedit` as in step 7. *Why: the Player
    refuses to start (exit 2, `daemon.misconfigured` naming the key) with a
    `*_SECONDS` of 0, below 0, `nan` or `inf`, and `Restart=always` then tries
    again every 5 s with every wall dark.*

7c. **IF step 2 showed that section and you open the settings page by a name** that is not an IP address, not
    `localhost`, not the Pi's own name and not `<name>.local` (a router's
    `<name>.lan`, or a reverse proxy's name): list it in `.env`.

        sudoedit /opt/arrt-player/.env

    Add `UI_ALLOWED_HOSTS=<name>`, several comma-separated, names only. *Why:
    the page refuses any other name with a 421 before any page, and the journal
    says `settings_page.host_refused`.*

8. Sync the Player's environment:

       cd /opt/arrt-player && sudo -u tvpi /usr/local/bin/uv sync --frozen --no-dev --group raster --group epaper

   **Expected:** ends with an `Installed …`, `Uninstalled …`, `Audited …`
   or `Checked …` line, and no `error:`.
   *Why the groups: without them the sync removes the label's typesetter and
   the panel driver. Why `--frozen`: it installs what `uv.lock` says without
   re-resolving it, so the checkout's lock is never rewritten. Why `--no-dev`:
   the Pi runs no tests, and the unit starts without the dev group too.*

9. Compare the unit with the checkout's:

       diff -q /opt/arrt-player/deploy/display.service /etc/systemd/system/display.service

   **Expected:** nothing printed → step 12.
   **IF it prints `… differ`:** → step 9a.

9a. Check that `.env` names no path the unit cannot reach. The unit makes
    `/home`, `/root` and `/run/user` unreachable for the Player:

        sudo grep -nE '^[A-Z_]+=["'"'"']?(/home|/root|/run/user)(/|$)' /opt/arrt-player/.env

    **Expected:** nothing printed → step 10.
    **If it prints a line:** → step 17, with the new unit not installed. That
    setting has to name a path outside those three before this unit can run,
    and choosing where is the owner's call.

10. Install the new unit:

        sudo cp /opt/arrt-player/deploy/display.service /etc/systemd/system/

11. Reload systemd:

        sudo systemctl daemon-reload

12. Start the Player:

        sudo systemctl start display.service

12a. **IF steps 10–11 installed a new unit:** check that the unit's main process
     is `uv` and the Player its child. The unit's `NotifyAccess=all` rests on it:
     with `main` systemd would drop the Player's `READY=1` and fail every start.

         pid=$(systemctl show -p MainPID --value display.service); ps -o pid=,args= -p "$pid"; pgrep -P "$pid" -a

     **Expected:** the first line is `uv run …`, and `pgrep` prints the Python
     process that runs `python -m arrt_player`.
     **If the main process is Python itself:** `NotifyAccess=all` is wider than
     needed and harmless; tell the owner. **If it is neither:** → step 17.

13. Run `systemctl is-active display.service`.
    **Expected:** `active`. *Why it may say `activating` for a moment: the unit is
    `Type=notify` and counts the Player started only when it says `READY=1`,
    which it does once its mode is running, within seconds.*
    **If it still says `activating` after a minute, or `failed`:** run
    `systemctl status display.service`. A missing setting is refused by name: fix
    it with step 7 and repeat step 12. A start that never says `READY=1` ends with
    `start operation timed out` after ten minutes → step 17.

13a. **IF steps 10–11 installed a new unit:** check that systemd read the new
     directives, and that the Player is `active` in the mode it runs.

         systemctl show -p Type,NotifyAccess,WatchdogUSec,WatchdogSignal,TimeoutStartUSec,TimeoutAbortUSec display.service

     **Expected, all of:** `Type=notify`, `NotifyAccess=all`, `WatchdogUSec=5min`,
     `WatchdogSignal=15` (SIGTERM), `TimeoutStartUSec=10min`, `TimeoutAbortUSec=1min 30s`.
     **Once per Pi, with the owner:** every mode owes `READY=1`, and a mode that
     never sends it fails its start. Step 13 checked the mode the Player runs in.
     To check the others, switch on the settings page to *A Reddit slideshow* and
     back (step 13 after each: `active` within a minute), and, for setup mode,
     the Player started with no server address set anywhere (the page serves, and
     the unit is `active`). Put it back as it was.

13b. **Once per Pi, with the owner (it takes the wall dark for a minute):** check
     that a refused configuration still restarts every 5 s. Add a line
     `MANIFEST_POLL_SECONDS=0` with `sudoedit /opt/arrt-player/.env`, run
     `sudo systemctl restart display.service`, and look:

         systemctl status display.service --no-pager | head -5; journalctl -u display.service -n 5 --no-pager

     **Expected:** `activating (auto-restart)` and the journal saying
     `Arrt Player cannot start` and `daemon.misconfigured`, again every 5 s.
     Remove the line, restart, and step 13 says `active`.

14. Read the Player's start in the journal:

        journalctl -u display.service --since '5 min ago' --no-pager | grep -E 'client.started|client.wall_started'

    **Expected, all of:**
    - a `"event": "client.started"` line
    - a `"event": "client.wall_started"` line naming your wall and output (for example `hdmi-a-1`)

    **If not:** no `client.wall_started` after a minute → step 17.

14a. **IF steps 10–11 installed a new unit:** check that its sandbox let the
     Player do its work. Wait until the wall has had a few minutes, then:

         journalctl -u display.service --since '10 min ago' --no-pager | grep -E 'Read-only file system|Permission denied|Address family not supported'

     **Expected:** nothing printed. *Why: those are the errors a write the
     unit forbids, a path it hides and a socket it refuses would raise.*

         journalctl -u display.service --since '10 min ago' --no-pager | grep -oE '"event": "(rotation.selected|slideshow.shown|slideshow.not_shown|label.drawn|screen.draw_failed|screen.brightness_set|screen.brightness_failed)"' | sort | uniq -c

     **Expected, all of:**
     - `rotation.selected` (a wall from the server), or `slideshow.shown` (a
       slideshow), and the screen shows the picture, not the text console
     - `label.drawn`, if this Pi has a label panel
     - `screen.brightness_set`, if `.env` sets a location
     - no `screen.draw_failed`, `slideshow.not_shown` or
       `screen.brightness_failed`

     Then check that the kernel enforces the unit's memory bound:

         cat /sys/fs/cgroup/system.slice/display.service/memory.high /sys/fs/cgroup/system.slice/display.service/memory.max

     **Expected:** two numbers of bytes, the first smaller (about 5 and 6
     billion on an 8 GB Pi).
     **If `No such file or directory`:** the kernel's memory controller is off,
     so the bound is not enforced and the Player runs as it did before. The wall
     is not harmed. Tell the owner.
     **If anything else is not as expected:** → step 17, and keep the line the
     first command printed, if any. It names what the sandbox refused.

14b. **IF steps 10–11 installed a new unit, once per Pi, with the owner:** check
     that systemd restarts a Player that stops making progress, and that it stops
     it the orderly way. Stop the Player's Python without ending it:

         sudo kill -STOP $(pgrep -P "$(systemctl show -p MainPID --value display.service)" -f arrt_player)

     Wait six minutes, then:

         journalctl -u display.service --since '8 min ago' --no-pager | grep -iE 'watchdog|Stopping|Started|frame.stopped|client.stopped|client.started'

     **Expected:** `Watchdog timeout (limit 5min)!`; then, before the next
     `Started`, a `frame.stopped` line for each Frame wall and one
     `client.stopped`, which the Player logs only after it has closed each
     wall's connection to the television (`frame.stopped` follows the close),
     so they are the orderly stop itself; then a start (a `client.started`
     line), within `WatchdogSec` plus `RestartSec`, about 305 s; and step 13
     prints `active`. A `tv.unavailable` after the restart, or its absence,
     says nothing about how the stop went: a set asleep logs one either way, and
     over five minutes a set may drop a half-open channel on its own side.
     The wall keeps its picture while this runs.
     **If `client.stopped` is missing before `Started`:** the watchdog's stop did
     not take the orderly path → tell the owner, and keep the lines.
     **If nothing happens:** the Player is being petted when it should not be, or
     the unit is not `Type=notify` → step 13a.
     **If `Stopping` is followed by `SIGKILL` after about 90 s:** the Player's
     event loop is stuck too, which is the one case the unit accepts → tell the
     owner.

14c. **IF steps 10–11 installed a new unit:** check that the added directives
     are what systemd reports, and that none stopped a device.

         systemd-analyze security display.service --no-pager | grep -E 'LockPersonality|RestrictNamespaces|RestrictSUIDSGID|RestrictRealtime|ProtectHostname|ProtectKernelLogs|ProtectControlGroups|SystemCallArchitectures|CapabilityBoundingSet'

     **Expected:** a line for each, none marked as exposed. *The tool's wording is
     systemd's and has not been read on this Pi.* Then repeat step 14a's second
     command once the wall has run ten minutes: the screen, the label (SPI and
     GPIO), the DDC bus (`screen.brightness_set`) and mpv each draw. **A directive
     that stops one is reverted by name in `deploy/display.service`**, and the
     owner is told which.

15. **On your Mac**, read how the server hears the Pi. `SERVER_URL` is the
    server's address, the same value as in the Pi's `.env`:

        curl -s "$SERVER_URL"/api/clients | jq -c '.clients[] | {name, heartbeat: {age_seconds: .heartbeat.age_seconds, problem: .heartbeat.problem}}'

    **Expected:** for your Pi, `problem` is `null` and `age_seconds` is under 90.
    *Why 90: an unchanged heartbeat goes once a minute, and the server is
    polled about every 30 s.*

16. **On your Mac**, check the wall reads its feed:

        curl -s "$SERVER_URL"/api/walls | jq -c '.walls[] | {name, reads_feed, feed_notice}'

    **Expected:** each wall the Pi shows has `"reads_feed":true` and
    `"feed_notice":null`.
    **If not:** → step 17.

## Done when
- `systemctl is-active display.service` prints `active`
- `git -C /opt/arrt-player rev-parse --short HEAD` prints `<new>`
- step 16's command shows `"reads_feed":true` for the Pi's walls

## Going back

Every step above can be undone, but the Player moves two of its files
forward, and an older Player may refuse them. (To go back from § The first
time, use F9 there.)

- **The settings page's file, `$CACHE_DIR/.settings.json`, is layout version
  2** once a Player from v0.8.0 on has saved anything to it, **and layout 3 while
  it holds `TV_ADDRESS`**, which the page can now set. A Player that writes an
  older layout refuses a newer one by name (`.settings.json is layout version 3`)
  and exits with status 2, and the unit starts it again every 5 s with every
  wall dark. Step 19a puts the file back to what the old revision writes.
- **Each Frame wall's store, `$CACHE_DIR/<wall id>/display-state.sqlite`, is
  widened on open** to the schema the running Player knows, and **an older
  Player refuses a store newer than itself**: it shows no art on that wall,
  saying `daemon.state_too_new`. A Player from before 2026-11-19 writes schema 2
  and the release that follows it writes 5, so going back across it parks every
  Frame wall until step 19b sets each store's version back. The older Player then
  ignores the new columns and tables, and sends every work again (one upload per
  work); the rows stay, so going forward again loses nothing but the recorded
  television, which is forgotten and taken from the first read.
- **Going back across schema 5 needs the owner's say-so each time** (the owner,
  2026-11-25), because what the release before it does to the set is not
  harmless. Measured at integration: v0.8.0, which writes schema 2, refuses the
  newer store, journals `daemon.state_too_new`, parks only the Frame wall and
  **touches nothing on the set**. **Only after step 19b sets the store's version
  to 2, or the store is deleted, does its sweep remove every uploaded picture its
  own records do not name**: the household's pictures, an older Player's
  leftovers, even the one on screen. So the steps below stop at 19b without the
  owner's word. Leaving the stores as they are, the walls on the old revision
  stay parked and the set is untouched.

17. **IF step 2's sections say the release goes back as a pair** (the server
    and the Player together): roll the server back first, using the homelab
    runbook's *Going back*.

18. Stop the Player: `sudo systemctl stop display.service`

19. Check out the old commit:

        sudo -u tvpi git -C /opt/arrt-player checkout --detach <old>

19a. Ask the old revision which settings layout it writes:

         sudo -u tvpi git -C /opt/arrt-player show <old>:src/arrt_player/settings_ui/store.py | grep '^VERSION: Final'

     **Expected:** `VERSION: Final[int] = 2`, and the file's own layout is not
     3 (`sudo jq .version <CACHE_DIR>/.settings.json`, with `CACHE_DIR` from
     `sudo grep '^CACHE_DIR=' /opt/arrt-player/.env`) → step 19b.
     **If the file prints `3`:** the page holds the Frame's address, which the
     old revision cannot read. Move it into `.env` first: read it with
     `sudo jq -r .values.TV_ADDRESS <that directory>/.settings.json`, add
     `TV_ADDRESS=<it>` to `.env` with `sudoedit /opt/arrt-player/.env`, then as
     root (`sudoedit <that directory>/.settings.json`) delete `TV_ADDRESS` from
     `values` and set `version` to `2`. → step 19b.
     **If it prints `fatal: path …`:** the old revision has no settings
     page and ignores the file → step 19b.
     **If `VERSION: Final[int] = 1`:** read the file's own layout:

         sudo grep '^CACHE_DIR=' /opt/arrt-player/.env
         sudo jq .version <that directory>/.settings.json

     If it prints `2` or `3`, edit it as root (a `TV_ADDRESS` in `values` moves
     into `.env` as above):

         sudoedit <that directory>/.settings.json

     In `values`, delete the keys `PLAYER_MODE`, `SLIDESHOW_URL`,
     `REDDIT_SESSION`, `SLIDESHOW_MIN_SECONDS`, `SLIDESHOW_MAX_SECONDS` and
     `SLIDESHOW_OUTPUT`, and set `version` to `1`. Leave `auth` as it is.
     Then check:

         sudo jq -c '{version, values: (.values | keys)}' <that directory>/.settings.json

     **Expected:** `"version":1`, and none of the six keys.
     *Why the edit and not a delete: the file holds the page's password and
     whatever it set, which `.env` does not.* If it printed `1`, or
     `No such file`, there is nothing to do → step 19b.

19b. **Only with the owner's say-so (see § Going back).** Ask the old revision
     which Frame store schema it writes, and set each Frame wall's store to it.
     **The Player must be stopped** (step 18). **Past this step the old revision
     sweeps the set** by the rule it was written with, and removes every uploaded
     picture its records do not name, the household's included; **without it, the
     old revision parks that wall and touches nothing on the set.** Stop here, and
     tell the owner, if you do not have their word.

         sudo -u tvpi git -C /opt/arrt-player show <old>:src/arrt_player/state/store.py | grep '^SCHEMA_VERSION'

     **Expected:** `SCHEMA_VERSION: Final[int] = 2` (call the number `<n>`).
     **If it prints `fatal: path …`:** the old revision has no store → step 20.
     Find the stores (step 6a's commands, given here because 6a is skipped
     when the Frame has always been the same set):

         sudo grep '^CACHE_DIR=' /opt/arrt-player/.env
         sudo -u tvpi find <that directory> -maxdepth 2 -name 'display-state.sqlite'

     For each store it names:

         sudo -u tvpi python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA user_version = <n>'); c.commit()" <that directory>/<wall id>/display-state.sqlite

     Then read it back:

         sudo -u tvpi python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA user_version').fetchone())" <that directory>/<wall id>/display-state.sqlite

     **Expected:** `(2,)`, or `(<n>,)` as this step's first command printed it.
     *Why not `3`: a revision that writes schema 2 refuses 3 as it refuses 4, and
     only the number the old revision writes opens the store (measured against
     the end of wave 1's code, 2026-11-19, and again with v0.8.0 against a
     schema-5 store at integration), so the number is the one the old
     revision prints, never the nearest below the store's.* Or delete the store,
     which costs a re-upload of the whole wall instead of one per work, and which
     has the same effect on the set. If the old revision prints 5 or more, the
     store opens as it is.

20. Repeat steps 8–14, which put the old revision's environment and unit back
    and start it.
    **Expected:** step 14's two lines, from the old revision.
    *A unit from before 2026-11-19 starts `uv run` without `--no-dev`, so its
    first start installs the dev group again, from uv's cache or from PyPI.*

## When the revision is a release: move the server's pinned Player

**On your Mac, once, when a release is cut** (a tag on `main` of this repo), not
on the Pi. Arrt's test of a real Player against a real server runs the Player at
the one commit that Arrt's `arrt/tests/system/player-ref.txt` names, fetched from
this repository, so a change to the Player is tested against the server only when
that line moves. Open a pull request on Arrt that replaces the 40-hex commit on
its last line with the release's commit (`git rev-parse <tag>^{commit}`), and say
in it which release. *Why a commit and not the tag: a tag can be moved.* Until it
merges, the server's test still runs the older Player. **The file is in the
server's wave-2 work and had not merged when this was written; if it is not at
that path, find it by the name `player-ref.txt` in Arrt's `arrt/tests/system/`.**

## If this doesn't work
- The old revision won't start either → stop. The wall shows the text console
  until the Player runs, and nothing else is harmed. Read
  `journalctl -u display.service -n 100` and look at it tomorrow.
- A step doesn't match what you see → stop and ask. That's a defect in this
  document.
- More on what the Player logs and why: `deploy/README.md`
  § The Player as a client of the NAS.
