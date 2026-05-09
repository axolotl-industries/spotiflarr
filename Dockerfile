# Stage 1 — build SpotiFLAC headless from upstream source.
# Pinned to a specific commit at build time via the SPOTIFLAC_REF arg if you
# want reproducibility; defaults to main. Re-run `docker compose build` to
# refresh against upstream.
FROM golang:1.22-bookworm AS spotiflac-builder

ARG SPOTIFLAC_REPO=https://github.com/afkarxyz/SpotiFLAC.git
ARG SPOTIFLAC_REF=main

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone "$SPOTIFLAC_REPO" /src \
 && cd /src \
 && git checkout "$SPOTIFLAC_REF" \
 && go build -tags headless -trimpath -ldflags="-s -w" -o /spotiflac . \
 && /spotiflac --help 2>&1 | head -1 || true

# Stage 2 — slim Python runtime for the orchestrator + Flask UI.
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=spotiflac-builder /spotiflac /usr/local/bin/spotiflac

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orchestrator.py .
COPY templates/ ./templates/

# /data: state.json, config.json overrides
# /output: where SpotiFLAC writes; mount this to the path Lidarr can read
VOLUME ["/data", "/output"]

EXPOSE 8181

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-u", "orchestrator.py"]
