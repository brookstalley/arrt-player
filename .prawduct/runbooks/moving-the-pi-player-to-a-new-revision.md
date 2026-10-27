# Moving the Pi's Arrt Player to a new revision

Owner: the operator · Last executed: 2026-10-27, § The first time, F1–F7, from
the monorepo checkout to f7937da in the standalone one (F8 is the owner's call
and the way back, F9, was not exercised). § Steps last ran 2026-10-11,
81e2d15e → 75770fe8, in the monorepo checkout (no new setting or unit, so steps
7, 10–11 and the way back were not exercised), and has not yet run against the
standalone checkout.

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

F6. Install the unit and reload systemd:

        sudo cp /opt/arrt-player/deploy/display.service /etc/systemd/system/
        sudo systemctl daemon-reload

F7. Steps 12–16, except that where one of them sends you to step 17, go to F9
    instead. Then check the unit runs from the new checkout:

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

7. **IF step 2 showed a new Player setting:** add it.

       sudoedit /opt/arrt-player/.env

   *Why `sudo`: `.env` is readable by `tvpi` and root only.*

8. Sync the Player's environment:

       cd /opt/arrt-player && sudo -u tvpi /usr/local/bin/uv sync --group raster --group epaper

   **Expected:** ends with an `Installed …`, `Uninstalled …`, `Audited …`
   or `Checked …` line, and no `error:`.
   *Why the groups: without them the sync removes the label's typesetter and
   the panel driver.*

9. Compare the unit with the checkout's:

       diff -q /opt/arrt-player/deploy/display.service /etc/systemd/system/display.service

   **Expected:** nothing printed → step 12.
   **IF it prints `… differ`:** → step 10.

10. Install the new unit:

        sudo cp /opt/arrt-player/deploy/display.service /etc/systemd/system/

11. Reload systemd:

        sudo systemctl daemon-reload

12. Start the Player:

        sudo systemctl start display.service

13. Run `systemctl is-active display.service`.
    **Expected:** `active`.
    **If not:** run `systemctl status display.service`. A missing setting is
    refused by name: fix it with step 7 and repeat step 12. Anything else → step 17.

14. Read the Player's start in the journal:

        journalctl -u display.service --since '5 min ago' --no-pager | grep -E 'client.started|client.wall_started'

    **Expected, all of:**
    - a `"event": "client.started"` line
    - a `"event": "client.wall_started"` line naming your wall and output (for example `hdmi-a-1`)

    **If not:** no `client.wall_started` after a minute → step 17.

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

Every step above can be undone. Nothing on the Pi migrates. (To go back from
§ The first time, use F9 there.)

17. **IF step 2's sections say the release goes back as a pair** (the server
    and the Player together): roll the server back first, using the homelab
    runbook's *Going back*.

18. Stop the Player: `sudo systemctl stop display.service`

19. Check out the old commit:

        sudo -u tvpi git -C /opt/arrt-player checkout --detach <old>

20. Repeat steps 8–14, which put the old revision's environment and unit back
    and start it.
    **Expected:** step 14's two lines, from the old revision.

## If this doesn't work
- The old revision won't start either → stop. The wall shows the text console
  until the Player runs, and nothing else is harmed. Read
  `journalctl -u display.service -n 100` and look at it tomorrow.
- A step doesn't match what you see → stop and ask. That's a defect in this
  document.
- More on what the Player logs and why: `deploy/README.md`
  § The Player as a client of the NAS.
