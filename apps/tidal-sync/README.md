Tidal-Sync: GitOps Automated Discography Manager

Tidal-Sync is a highly resilient, automated state-management engine that synchronizes a user's Tidal favorite artists into a comprehensive, deduplicated "Master Discography" playlist series.

Designed to run as a Kubernetes CronJob within a GitOps architecture, this tool bypasses undocumented API limits, dynamically handles HTTP failures, and autonomously cleanses metadata to ensure a pristine listening experience.
✨ Key Engineering Features

    Multi-Volume Pagination Bypass: Natively circumvents Tidal's 10,000-track playlist limit and 1,000-track API pagination blindspots by dynamically building cross-volume caches and executing stateful rollovers at 9,500 tracks.

    Fault-Tolerant API Interactions (Graceful Degradation): * 400 Bad Request Fallback: If a batch-add fails due to a single region-locked or unplayable track, the engine dynamically degrades from chunked-processing to 1-by-1 isolation, silently dropping the corrupted track while saving the rest of the batch.

        412 Precondition Failed Defense: Implements ETag lock refreshing and retry loops to prevent race conditions when modifying playlist state.

    Autonomous Spam Annihilation: Utilizes a hybrid of static blocklists and dynamic frequency analysis (detecting recurring base-patterns like Group Therapy or A State of Trance) to execute fuzzy-substring matching. It proactively ignores new spam and automatically purges old spam from existing playlists.

    Intelligent Deduplication: Groups identical releases and scores them based on metadata keywords (e.g., prioritizing "Super Deluxe" or "Expanded" editions), ensuring only the highest-quality version of an album is synced.

    Headless GitOps Deployment: Designed for zero-touch execution. Authentication state is mounted as a read-only Kubernetes Secret, allowing the script to run seamlessly inside a Docker container managed by ArgoCD.

🏗️ Architecture & Deployment

This tool is built to be deployed via a CI/CD pipeline (GitHub Actions -> Docker Registry) and orchestrated via Kubernetes.
1. Authentication Secret

The script requires a valid Tidal OAuth session file (session.json) to run headlessly. You must extract this from a local login and mount it into your cluster as a Secret:
YAML

apiVersion: v1
kind: Secret
metadata:
  name: tidal-session-secret
  namespace: default
type: Opaque
data:
  session.json: <base64-encoded-session-data>

2. Kubernetes CronJob

The workload is managed via a CronJob that mounts the secret into /app/secrets. See the manifests/cronjob.yaml for the complete definition.
YAML

# Snippet from cronjob.yaml
          containers:
          - name: tidal-sync
            image: ghcr.io/your-username/tidal-sync:latest
            env:
            - name: SESSION_PATH
              value: "/app/secrets/session.json"
            volumeMounts:
            - name: auth-vol
              mountPath: "/app/secrets"
              readOnly: true

⚙️ Configuration

The engine's behavior can be tweaked by adjusting the variables at the top of sync.py:
Variable	Description	Default
PLAYLIST_PREFIX	The naming convention for the generated playlists.	"Master Discography"
MAX_TRACKS_PER_VOL	The threshold to trigger a Volume rollover.	9500
CHUNK_SIZE	Tracks pushed per API request.	50
SERIES_THRESHOLD	Occurrences required for an album pattern to be flagged as an automated radio show/podcast.	15
STATIC_BLOCKLIST	Hardcoded fuzzy-match strings to immediately drop known spam.	["group therapy", "asot", ...]
🚀 Running Manually (Testing)

While designed for ArgoCD, you can trigger a manual sync in your cluster for testing or immediate updates:
Bash

# Clear previous job runs
kubectl delete jobs --all 

# Spawn a new run from the CronJob template
kubectl create job --from=cronjob/tidal-sync tidal-sync-manual-run

# Follow the execution logs
kubectl logs job/tidal-sync-manual-run -f

⚠️ Disclaimer

This project uses the unofficial tidalapi library. It is built for personal homelab use to manage one's own legal library. API behaviors (such as ETag requirements and pagination limits) are subject to change by Tidal.
