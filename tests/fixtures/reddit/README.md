# Reddit listing fixtures

`shapes.json` is one listing built from real `/…/top.json?raw_json=1` answers,
fetched on 2026-11-10 with the owner's session cookie, one post for each shape the
slideshow's parser reads. The fetch is described in `.prawduct/artifacts/build-plan-slideshow.md`
(chunk 02's verify-api).

Each post is trimmed to the keys the parser reads, plus a few for context. Because
this repository is public:

- every title is replaced with the shape's name and every author with `someone`;
- the redgifs post comes from a host whose posts are almost all NSFW, so its URLs
  are redacted down to their host and form (`/watch/…`, `DASH_720.mp4?source=fallback`).

Everything else (domains, media URLs, `gallery_data` order, `media_metadata`, `preview`,
`secure_media`, crosspost parents) is as Reddit sent it.
