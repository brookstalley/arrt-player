"""The Player's own settings page, and the file the page writes.

`store` is the file and nothing else, so `config` can read it without importing a
web server. `auth` is the password and the session cookie. `server` is the page,
and the one module here that listens on a socket.
"""
