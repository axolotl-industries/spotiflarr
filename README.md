# Spotiflarr

Bridges [SpotiFLAC](https://github.com/afkarxyz/SpotiFLAC) into Lidarr.

Polls Lidarr's wanted-missing list every 3 hours, picks 5 albums, resolves
each to a Spotify album URL via MusicBrainz url-rels, runs SpotiFLAC to
fetch the FLAC, then triggers `DownloadedAlbumsScan` so Lidarr imports
into the artist tree. Runs alongside Soularr — Soularr handles the
albums Spotiflarr can't (no Spotify URL on MB, deny-listed, etc.).

## What's in the container

- `spotiflac` — built from upstream `afkarxyz/SpotiFLAC` headless mode at
  image build time. Re-run `docker compose build` to refresh.
- Python orchestrator + tiny Flask UI on port 8181.
- State in `/data/state.json`, config overrides in `/data/config.json`.

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

docker compose build
docker compose up -d
docker compose logs -f
```

First-run logs should show:
- `starting spotiflarr`
- `UI on :8181`
- `cycle: N wanted, 5 this run`

Open `http://<LXC-100-IP>:8181/` for the status page. Settings page
at `/settings` lets you edit any of the runtime values without rebuilding.

## What it does, per cycle

For each of up to N albums Lidarr is missing:

1. **Resolve.** Hit MusicBrainz at `/release-group/<mbid>?inc=url-rels`,
   look for a Spotify album URL. If absent, walk the first 5 releases
   under the group and check their url-rels too. If still nothing, mark
   `no_spotify_url` and move on — Soularr's path is unaffected.

2. **Fetch.** Shell out to `spotiflac -o /output/<Artist - Album>/ <url>`.
   SpotiFLAC tries Tidal → Qobuz → Amazon Music in order until something
   delivers FLACs.

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
- **SpotiFLAC's audio sources are reverse-engineered third-party APIs**
  (hifi-api, dabmusic.xyz, squid.wtf, doubledouble.top, lucida.to). They
  WILL break occasionally. Rebuild the image to pick up upstream fixes.
- **Quality profile gating.** Whatever SpotiFLAC delivers (mostly 16/44
  from Tidal/Qobuz, sometimes 24-bit on Qobuz HiRes) needs to be allowed
  by your Lidarr quality profile, or imports will fail with "Not an
  Upgrade." Same caveat as the DSF migration tool.
- **Race with Soularr.** Both will see the same wanted list. If both
  grab the same album in the same window, Soularr's import will fail
  silently because Lidarr already has the file. Wasted bandwidth, no
  damage.
- **Public exposure.** Don't put :8181 behind Cloudflare/Authelia unless
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

## Pinning SpotiFLAC

Default Dockerfile builds upstream `main`. To pin to a specific commit
(reproducibility, or to dodge a regression), uncomment the build args
in `docker-compose.yml`:

```yaml
build:
  context: .
  args:
    SPOTIFLAC_REF: <commit-sha>
```

Then `docker compose build`.
