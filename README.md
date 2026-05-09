# Spotiflarr

Bridges a private fork of [jelte1/SpotiFLAC-Command-Line-Interface](https://github.com/jelte1/SpotiFLAC-Command-Line-Interface)
(retrofitted with Spotbye proxy endpoints from the SpotiFLAC-Next AppImage)
into Lidarr.

Polls Lidarr's wanted-missing list every 3 hours, picks 5 albums, resolves
each to a Spotify album URL via MusicBrainz url-rels, runs the SpotiFLAC
CLI to fetch the FLACs, then triggers `DownloadedAlbumsScan` so Lidarr
imports into the artist tree. Runs alongside Soularr — Soularr handles
the albums Spotiflarr can't (no Spotify URL on MB, deny-listed, etc.).

## What's in the container

- The Python SpotiFLAC CLI cloned from `SPOTIFLAC_REPO` at image build
  time (private fork; needs a GitHub token — see below). Re-run
  `docker compose build` to refresh.
- Python orchestrator + tiny Flask UI on port 8182.
- State in `/data/state.json`, config overrides in `/data/config.json`.
- `ffmpeg` (the CLI uses it to remux Tidal manifests and decrypt Amazon Music).

## Install on LXC 100

```bash
mkdir -p /home/docker/spotiflarr
cd /home/docker/spotiflarr

# scp the contents of this directory in:
#   Dockerfile, docker-compose.yml, .env.example, requirements.txt,
#   orchestrator.py, templates/

cp .env.example .env
nano .env       # set LIDARR_API_KEY (required); rest can stay defaults

# Confirm the bind mount path on the LEFT side of the volumes section in
# docker-compose.yml matches your host. Default is:
#   /pool/media/downloads/music/spotiflac:/output
# That host path doesn't need to exist yet — Docker will create it.

# Build needs a GitHub token because the SpotiFLAC fork is private. If
# you've already run `gh auth login` on this host, just:
GH_TOKEN=$(gh auth token) docker compose build
docker compose up -d
docker compose logs -f
```

First-run logs should show:
- `starting spotiflarr`
- `UI on :8182`
- `cycle: N wanted, 5 this run`

Open `http://<LXC-100-IP>:8182/` for the status page. Settings page
at `/settings` lets you edit any of the runtime values without rebuilding.

## What it does, per cycle

For each of up to N albums Lidarr is missing:

1. **Resolve.** Hit MusicBrainz at `/release-group/<mbid>?inc=url-rels`,
   look for a Spotify album URL. If absent, walk the first 5 releases
   under the group and check their url-rels too. If still nothing, mark
   `no_spotify_url` and move on — Soularr's path is unaffected.

2. **Fetch.** Shell out to `python /opt/spotiflac-cli/launcher.py <url>
   /output/<Artist - Album>/ --service qobuz tidal`. The CLI walks the
   service list in order until one delivers FLACs.

3. **Import.** POST `DownloadedAlbumsScan` to Lidarr with the album folder
   path (Lidarr's view, set via `LIDARR_OUTPUT_PATH`). Lidarr matches by
   tags and moves files into the artist tree on its own. Source folder
   gets cleaned up by Lidarr's Move logic.

4. **Track.** Successes clear retry counters. Failures increment; at
   `max_retries` the album gets deny-listed (won't be retried until you
   click "clear deny-list" on the UI).

## Caveats worth knowing

- **MB url-rel coverage is patchy.** Big-name releases usually have
  Spotify links populated; obscure stuff often doesn't. Expect a healthy
  `no_spotify_url` count — that's not a bug, it's MB completeness.
- **SpotiFLAC's audio sources are reverse-engineered third-party APIs.**
  The fork prefers Spotbye proxies (`*.spotbye.qzz.io`) extracted from
  the SpotiFLAC-Next AppImage; older mirrors (dab.yeet.su, dabmusic.xyz,
  squid.wtf, qqdl.site) are kept as fallbacks but mostly dead. Rebuild
  the image to pull the fork's `main` and pick up endpoint patches.
- **Quality profile gating.** Whatever SpotiFLAC delivers (mostly 16/44
  from Tidal/Qobuz, sometimes 24-bit on Qobuz HiRes) needs to be allowed
  by your Lidarr quality profile, or imports will fail with "Not an
  Upgrade." Same caveat as the DSF migration tool.
- **Race with Soularr.** Both will see the same wanted list. If both
  grab the same album in the same window, Soularr's import will fail
  silently because Lidarr already has the file. Wasted bandwidth, no
  damage.
- **Public exposure.** Don't put :8182 behind Cloudflare/Authelia unless
  you genuinely need it remote — there's no auth on the UI itself. LAN
  access only is the right default.

## UI quick reference

- `/` — status page; shows last cycle, totals, deny-list size, recent
  activity, last 50 log lines.
- `/settings` — editable config form. Saves to `/data/config.json`,
  applied on next cycle (no restart needed).
- `/run-now` (POST button) — wakes up the loop early instead of waiting
  for the timer.
- `/state/clear-denied` (POST button) — wipes deny-list and
  no_spotify_url so failed albums get reconsidered.
- `/healthz` — JSON liveness probe.

## Pinning the SpotiFLAC fork

Default build args clone `geoffevans/SpotiFLAC-Command-Line-Interface`
at `main`. To pin a commit or point at a different fork, override at
build time:

```bash
GH_TOKEN=$(gh auth token) \
  SPOTIFLAC_REF=<commit-sha> \
  SPOTIFLAC_REPO=<owner>/<repo> \
  docker compose build
```

The `GH_TOKEN` ends up in image layers. For a homelab that's fine; if
you'd rather it didn't, mint a fine-grained PAT scoped read-only to
the one repo and use that instead of `gh auth token`.
