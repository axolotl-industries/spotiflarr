# Stage 1 — build SpotiFLAC headless from the Nizarberyan fork.
#
# We use Nizarberyan/SpotiFLAC instead of upstream afkarxyz/SpotiFLAC
# because upstream has stripped CLI/headless support (no more headless.go
# or build-tag gating). The fork still ships it and the README's
# `go build -tags headless` invocation actually produces a working binary.
#
# go.mod in the fork requires Go 1.25, so use a matching base image.
# Override SPOTIFLAC_REPO/SPOTIFLAC_REF in compose if you want a different
# fork or a pinned commit.
FROM golang:1.25-bookworm AS spotiflac-builder

ARG SPOTIFLAC_REPO=https://github.com/Nizarberyan/SpotiFLAC.git
ARG SPOTIFLAC_REF=main

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone "$SPOTIFLAC_REPO" /src \
 && cd /src \
 && git checkout "$SPOTIFLAC_REF" \
 && go build -tags headless -trimpath -ldflags="-s -w" -o /spotiflac . \
 && test -x /spotiflac \
 && /spotiflac --help > /dev/null 2>&1 \
 && echo "spotiflac built OK ($(stat -c %s /spotiflac) bytes)"

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
