# Single-stage build now — the original Go-binary stage is gone.
#
# We've forked jelte1/SpotiFLAC-Command-Line-Interface (Python) and
# retrofitted it with Spotbye proxy endpoints extracted from the
# SpotiFLAC-Next AppImage. That fork lives at SPOTIFLAC_REPO and is
# expected to be private — the build needs a GitHub token to clone it.
#
# Build with:
#   GH_TOKEN=$(gh auth token) docker compose build
#
# (compose passes GH_TOKEN through as a build arg.)
FROM python:3.11-slim

ARG SPOTIFLAC_REPO=geoffevans/SpotiFLAC-Command-Line-Interface
ARG SPOTIFLAC_REF=main
ARG GH_TOKEN

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates tini ffmpeg git \
 && rm -rf /var/lib/apt/lists/*

# Clone the private fork. Token is required at build time. Using
# x-access-token:<TOKEN> is GitHub's preferred PAT format. The token DOES
# end up in image layers/history — fine for a homelab image that never
# leaves your network. Use a read-only fine-grained PAT scoped to this
# one repo if you're paranoid (gh auth token's default scopes are wider).
RUN test -n "$GH_TOKEN" || (echo "GH_TOKEN build arg is required" && exit 1) \
 && git clone "https://x-access-token:${GH_TOKEN}@github.com/${SPOTIFLAC_REPO}.git" /opt/spotiflac-cli \
 && cd /opt/spotiflac-cli \
 && git checkout "$SPOTIFLAC_REF" \
 && rm -rf /opt/spotiflac-cli/.git \
 && echo "spotiflac-cli cloned ($(ls /opt/spotiflac-cli | wc -l) entries)"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orchestrator.py .
COPY templates/ ./templates/

# /data: state.json, config.json overrides
# /output: where the CLI writes; mount this to the path Lidarr can read
VOLUME ["/data", "/output"]

EXPOSE 8182

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-u", "orchestrator.py"]
