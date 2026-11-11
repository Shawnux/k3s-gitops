# tidal-discography 2.1

Successor to `apps/tidal-sync`. Keeps a series of TIDAL playlists
("Master Discography 01", "02", ...) of up to 2,000 tracks each, holding the
official releases of every artist you follow: their own albums, EPs and
singles, in artist order, then release date, then track order.

Laid out like `apps/tidal-sync`: code and Dockerfile in `app/`, the CronJob in
`manifests/`, and a GitHub Actions workflow that tests the code and pushes
`ghcr.io/shawnux/tidal-discography`.

## What was wrong with tidal-sync, and what replaces it

| tidal-sync | Why it went wrong | Now |
| --- | --- | --- |
| Kept a release only if its title matched a MusicBrainz release group | The artist was matched by name search, only the first 100 MusicBrainz groups were read, and titles had to match exactly, so real releases were dropped | No MusicBrainz, no name search. Releases come from the artist's own TIDAL release list, by artist ID, every page |
| Fell back to a blocklist when MusicBrainz found nothing | Anything not on the list got in | Collections are recognised by who the tracks are by, then by name, then by repetition (below) |
| Blocklist matched substrings, on release and track titles | "edc", "purified", "satisfaction", "live at" also hit real titles | Whole-word matches on multi-track releases only, never when the tracks are the artist's own |
| Dedupe key removed all digits and brackets | "Vol. 1" and "Vol. 2", or a single and its remix pack, collapsed into one | Only true copies collapse (same title, version, type and tracklist) |
| Only ever appended, to the last volume | Junk already added stayed; order was whatever order things arrived in | Each part is reconciled: wrong tracks the tool added are removed, new releases are inserted beside the artist's older ones |
| First 1,000 favourites; unofficial `tidalapi` session file | Silent truncation; internal API | Official TIDAL API with your own app, all pages followed |

The old "Master Discography - Vol N" playlists are not touched. Delete them in
TIDAL once the new ones look right.

## What counts as an official release

Kept: a release where the followed artist is a main artist, of type album, EP
or single. Left out, each with its reason recorded:

| Left out | How it is recognised |
| --- | --- |
| Appears-on | the artist is not among the release's main artists |
| Various-artists compilation | a main artist is "Various Artists" |
| Compilation, sampler, radio show episode | 4+ tracks and fewer than half are the artist's |
| Named collection | 4+ tracks, title contains a known show, mix series or festival name ("A State of Trance", "ASOT", "Group Therapy", "ABGT", ...) or "selected by", "year mix", "episode 123", and under 80% of the tracks are the artist's (or TIDAL gave no credits) |
| Series | 15 or more of the artist's multi-track releases share one title once numbers are removed |
| DJ mix | "DJ Mix" / "Mixed by" in the title, or half the tracks are "(Mixed)" |
| Karaoke, tribute and similar | title patterns |
| Duplicate edition | a copy of a kept release (clean/explicit pairs) |

A track is the artist's when they are credited on it or named in its title, as
a remixer is. The built-in name list contains the whole tidal-sync blocklist;
add more under `extra_collection_names`. Deluxe editions, remasters, remix
packs and the artist's own live albums are official releases and are kept.

If TIDAL returns no track credits for a release, the tool asks for them
separately. If that fails too, the release is judged by name only and the run
summary says how many releases were kept without a credit check.

`tidal-disco report` lists everything left out and why, with album ids for
`force_include_album_ids` and `force_exclude_album_ids`.

## Playlists

- Parts are filled to 1,850 tracks and never grow past 2,000. An artist stays
  in its part, so a new release is inserted next to the older ones. A part
  that would pass 2,000 hands its last artist to a neighbouring part. An
  artist with more than 1,850 tracks spans several parts.
- A newly followed artist goes to the nearest part with room to its
  alphabetical neighbours.
- Only tracks this tool added are ever removed. Tracks you add by hand stay.
  A track you delete by hand comes back; to keep a release out, list its
  album id in `force_exclude_album_ids`.
- Removals with a known reason (an unfollow, a move between parts) always go
  through. Any other removal of more than a quarter of a part is skipped and
  reported; after changing the rules, run once with `--force`.
- A run where TIDAL returns no followed artists, or under half as many as
  last time, changes nothing.
- Each write is followed by a size check, because TIDAL accepts additions
  past its 10,000 cap and silently drops them.

## Set up

1. **TIDAL app.** At developer.tidal.com create an app. Add the redirect URI
   `http://localhost:8765/tidal` and enable the scopes `collection.read`,
   `playlists.read` and `playlists.write`. Copy the client ID. (The old
   `session.json` cannot be reused: it belongs to the unofficial API.)

2. **Log in once**, on a machine with a browser (Python 3, nothing to install):

       python3 apps/tidal-discography/app/tidal_disco/login.py --client-id YOUR_CLIENT_ID

   It prints a refresh token. TIDAL may replace that token on each use; the
   tool keeps the newest one in `/data/state.db`.

3. **Secret.** This repo is public, so the secret goes straight into the
   cluster, as `tidal-session-secret` did:

       kubectl create namespace tidal-discography
       kubectl -n tidal-discography create secret generic tidal-discography \
         --from-literal=TIDAL_CLIENT_ID=... \
         --from-literal=TIDAL_REFRESH_TOKEN=...

   Optional keys: `TIDAL_CLIENT_SECRET`, `DISCORD_WEBHOOK_URL` (run summaries).

4. **Push.** Copy `apps/tidal-discography/` and
   `.github/workflows/tidal-discography-build.yml` into `k3s-gitops` and push
   to `main`. The workflow runs the tests and pushes the image. If the first
   pull fails, set the new GHCR package to public, as `tidal-sync`'s is.

5. **ArgoCD.** `kubectl apply -f apps/tidal-discography/extras/argocd-application.yaml`

6. **Check one artist before the first real run.** In
   `extras/preview-job.yaml` set `args` to `["sync", "--only", "armin"]`, then

       kubectl create -f apps/tidal-discography/extras/preview-job.yaml
       kubectl -n tidal-discography logs -f -l app=tidal-discography-preview

   It prints that artist's kept releases and every left-out release with its
   reason, and changes nothing.

7. **First run.**

       kubectl -n tidal-discography create job first-run --from=cronjob/tidal-discography

   It reads every release of every artist and can take hours. If it reaches
   the time budget it stops without touching playlists and the next run
   carries on from the cache. Later runs fetch only what is new.

8. **Retire tidal-sync.** Set `suspend: true` in
   `apps/tidal-sync/manifests/cronjob.yaml`.

## Commands

    tidal-disco sync                 update the playlists
    tidal-disco sync --dry-run       show what would change
    tidal-disco sync --only NAME     one artist's kept and left-out releases (changes nothing)
    tidal-disco sync --rebuild       remove and re-add everything, restoring exact order
    tidal-disco sync --force         skip the safety limits
    tidal-disco report [--artist N]  what the last run left out, and warnings

Settings are in `manifests/config.toml`.

## What has and has not been verified

Written against TIDAL API reference v1.10.245 (endpoints, parameters,
payloads, 20 items a page, 50 items per playlist write).

Tested against a fake TIDAL server built from that reference (`app/tests`,
44 tests), not against the live API. Things only a live run can confirm, each
with a fallback in place:

- whether one request can fetch 20 albums with their tracks and credits
  (falls back to one album at a time, then to a separate credit lookup);
- whether TIDAL accepts `onDuplicates: SKIP` from third-party apps (dropped if refused);
- whether the artist release list includes appears-on releases (filtered either way);
- TIDAL's rate limits, which are not published (the tool backs off on 429).

Run the tests with `python3 -m pytest` in `app/` (needs `requests` and `pytest`).
