# Single-stage build. The SpotiFLAC Python CLI lives vendored under
# spotiflac-cli/ in this repo (forked from
# jelte1/SpotiFLAC-Command-Line-Interface and retrofitted with Spotbye
# proxy endpoints extracted from the SpotiFLAC-Next AppImage).
#
# Build with the usual:
#   docker compose build
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates tini ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Vendored SpotiFLAC CLI — orchestrator.py invokes /opt/spotiflac-cli/launcher.py
COPY spotiflac-cli/ /opt/spotiflac-cli/

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
