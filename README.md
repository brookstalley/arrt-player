# Arrt Player

The player at each wall of an [Arrt](https://github.com/brookstalley/arrt)
installation. A Player is a client of the Arrt server: it is told the server's
address and its own token, learns which walls it shows and on which of its
outputs, pulls each wall's programming and media into a local cache, and draws
only from that cache. A server that is down or restarting never blanks a wall.

Its outputs are a Samsung Frame television over the set's network API, the HDMI
connectors of the machine it runs on, and an e-paper panel beside the picture
that carries the work's museum label.

- **Configure** by copying `.env.example` to `.env`; every key is described there.
- **Run** with `uv run python -m arrt_player`. It drives the real television, and
  it leaves alone a set that somebody is watching.
- **Deploy** on a Raspberry Pi under systemd: `deploy/display.service`, with the
  record of how the reference wall was installed in `deploy/README.md`.
- **Develop**: `CLAUDE.md` holds the commands, including the one optional group the
  suite needs.

The contract between the Player and the server is Arrt's `arrt-contract` package,
which this project takes at a released tag.
