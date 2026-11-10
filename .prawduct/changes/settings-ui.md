---
schema: 1
type: feature
scope: settings-ui
date: 2026-11-10
title: A settings page on the Player, and a first run with no server configured
---
The Player serves one page of its own: a settings page on port 8771. It sets the
server, the client token and the Player's name, and wears Arrt's design. A Player
with no server configured now starts that page, rather than refusing to start.

### What changed
- **Setup mode.** With `SERVER_URL` or `CLIENT_TOKEN` set neither in the
  environment nor on the page, the Player serves only the page and prints
  `go to http://<address>:<port>/setup?code=<code> to configure`. A missing
  `CACHE_DIR` still exits 2.
- **The page.** The first password is set with a one-time code printed at
  start, and has at least 8 characters. Sessions are an HMAC-signed cookie,
  form posts must come from the page's own origin, and wrong passwords are
  throttled. The token can be written and never read back. The server's
  address and its token move together. A key `.env` sets is read-only on the
  page. Saving restarts the Player onto what was saved.
- **The name** a Player goes by is its server's, once there is one, and
  `PLAYER_NAME` otherwise. `TV_CLIENT_NAME` is untouched.
- **Its look** is Arrt's, copied with its source recorded: tokens, typefaces
  and component rules. A test holds the copy to WCAG AA in both colour schemes.
- **The plane-isolation guard** lets the page's one module import
  `aiohttp.web`, and nothing else from aiohttp.
- Settings are kept in `$CACHE_DIR/.settings.json` (versioned, 0600). New
  `.env` keys: `UI_BIND`, `UI_PORT`, `PLAYER_NAME`.

### Confirmed by the owner (2026-11-10)
- Setup mode and the printed line.
- A password set on first run, with an 8-character minimum.
- The setup code printed and logged.
- The narrowed guard.
- Never re-pairing the television over a name.
- Arrt's look.
- How the page looks in a real browser.

## Consumer note
**Check `.env` before deploying.** At start the Player now refuses, with exit 2
naming the key:
- a `SERVER_URL` that is not an http(s) address with a host, or that carries
  a user name, password, query or fragment;
- a `CLIENT_TOKEN` containing whitespace;
- a `PLAYER_NAME` over 64 characters.

Port 8771 is now listened on, on every interface unless `UI_BIND` narrows it.
On the first start, the journal's `go to http…` line is how to set the page's
password (`deploy/README.md` § The settings page).

