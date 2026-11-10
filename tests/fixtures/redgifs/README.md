# redgifs API fixtures

Real answers from `api.redgifs.com/v2`, fetched on 2026-11-10 with a temporary
token: `auth.json` (`/auth/temporary`), `gif.json` (`/gifs/{id}`, 200),
`gif_missing.json` (404) and `unauthorized.json` (a bad token, 401).

redgifs' posts are almost all NSFW and this repository is public, so every key and
type is kept as sent, and every value that names a person or the content is
redacted: strings become `redacted`, URLs keep their host and extension, the
`user` object is dropped, and the counts, dates and dimensions are replaced. The
token itself is never saved; `auth.json` keeps its claim names and its measured
lifetime instead.
