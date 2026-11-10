# Typefaces

The settings page's two typefaces, served by the page itself (`/static/fonts/`)
and declared at the top of `../settings.css`. Each family's licence is the
`OFL.txt` beside its files.

They are copied from Arrt's curation surface
(`arrt/src/arrt/http/static/fonts/`, Arrt at `3d2b599e`), so the Player's one
page reads as part of the same product (owner, 2026-11-10). The two repositories
share no files, so this copy does not follow Arrt's; to update it, copy the
files again from Arrt and change the commit above.

| Family | Files | Source | Licence |
|---|---|---|---|
| Newsreader (variable: `opsz`, `wght` 200–800) | `newsreader/{latin,latin-ext}-normal.woff2` | npm `@fontsource-variable/newsreader@5.3.0`, by way of Arrt | SIL OFL 1.1 |
| Instrument Sans (variable: `wdth` 75–100%, `wght` 400–700) | `instrument-sans/{latin,latin-ext}-normal.woff2` | npm `@fontsource-variable/instrument-sans@5.3.0`, by way of Arrt | SIL OFL 1.1 |

Upright only. The page sets nothing in italic, so the italic files Arrt carries
are left out. A browser that needs italic will synthesise it.
