# The image the Player's suite runs in on testament's runners (`.testament.toml`).
#
# Not a deploy image: the Player deploys onto a Pi from a checkout
# (`deploy/README.md`). This holds what `uv run --frozen --group raster pytest`
# needs and nothing that ships.
#
# **Debian trixie, not bookworm, because of the typesetter.** The `raster` group
# locks PyGObject 3.56, which ships no Linux wheel and builds against
# girepository-2.0 (GLib 2.80 or later). Bookworm's GLib is 2.74 and has no such
# package; trixie has it, and trixie is what the Pi runs. pycairo builds from
# source too, so the C toolchain, pkg-config and the cairo, pango and
# girepository headers are all here for the build, not only for the tests.
#
# **The fonts are not optional.** Pango with no fonts renders blank rather than
# failing, which passes the label's shape assertions and fails its ink ones; CI
# installs the same family for the same reason (`.github/workflows/suites.yml`).
#
# Built on each runner for its own architecture, so nothing here may be
# architecture-specific: the pool has arm64 and x86_64 machines.

FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

# git: the repository guards walk `git ls-files`, and `arrt-contract` is a git
# dependency. tar: testament unpacks its plugin with it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git tar \
      gcc libc6-dev pkg-config \
      libgirepository-2.0-dev libcairo2-dev libpango1.0-dev \
      fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

# Python lives outside any home, readable by the user below. 3.13 is the
# project's version (`project-preferences.md` § Language & Runtime): what the Pi
# runs.
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.13
RUN uv python install 3.13 \
 && chmod -R a+rX /opt/python

RUN useradd --create-home --uid 1000 tester
USER tester
