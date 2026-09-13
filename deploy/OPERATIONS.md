# Production maintenance

## Deploy this change

Deploy the backend, Worker, and the single-replica Beat from the same commit.
All builds install `requirements.txt`, which uses the checked-in constraints.
Use Python 3.13. Do not install `requirements.lock` by itself: it constrains,
rather than declares, platform-specific dependencies.

Set `CELERY_BROKER_URL` consistently across all three services, including the
same Redis database number. The web service previously pointed to localhost.
The new production resolver falls back to the configured Channels Redis URL
for a missing/loopback broker; verify that this matches Worker and Beat before
deployment. Do not copy secrets into files or logs.

`/health/` is process liveness; `/ready/` checks PostgreSQL and both Redis uses,
returns 503 on failure, and is the Railway deployment readiness probe.
Neither endpoint establishes worker or scheduler liveness: verify recent task
successes in Railway logs as well. Secure cookies and a short HSTS policy are
enabled outside DEBUG; only health probes are exempt from HTTPS redirection.

## Store updates

Android already queries Google's native Play in-app update API. The legacy
version-policy endpoint stays available, with an empty `latest` field; old
clients fall back to their installed version. `APP_LATEST_VERSION` is ignored
and can be removed from Railway. No release-number updates are needed.
Keep `APP_MIN_SUPPORTED_VERSION` only for exceptional compatibility cutoffs.
Play eligibility depends on account, installation source, device, and rollout.
Sideloaded builds without Play support no longer get optional server prompts.
iOS store discovery remains future work when the iOS app is published.

## Backups — setup and restore validation required

Create a dedicated **private** Spaces bucket, without CDN or public bucket
policy. Never use the media bucket. Set `BACKUP_SPACES_BUCKET` in the backup
runtime, and use bucket-limited `BACKUP_SPACES_ACCESS_KEY` and
`BACKUP_SPACES_SECRET_KEY`. Do not replace the media credentials. Database
archives contain sensitive account data. The command probes anonymous access
using a harmless private sentinel before uploading any archive. Sentinel files
are deliberately retained; no automated deletion is enabled.

Set Worker `NIXPACKS_CONFIG_FILE=deploy/nixpacks-worker.toml` to include PostgreSQL
17 tools in the worker build. Deploy the updated code before the first backup.
The Worker needs the production database connection and Spaces endpoint/region.
Run in the Worker console (use `/opt/venv/bin/python` if necessary):

    python manage.py backup_database

The command takes a consistent logical snapshot, verifies its archive listing,
uploads privately, checks object size/checksum metadata, and removes only its
own temporary local files. It never prunes remote archives or alters the DB.
Successful upload is **not** proof of successful restore.

Before scheduling daily execution, download one archive securely, verify its
SHA-256 against metadata, restore with `pg_restore --exit-on-error --no-owner
--no-acl` into a NEW isolated PostgreSQL database, and compare migration and
table counts. Never run a restore command against the production database.
Record restore evidence and configure alerts for missed/failed backups.
After the drill succeeds, set `DATABASE_BACKUPS_ENABLED=true` on both Worker
and the single Beat service and deploy them. This schedules 08:00 UTC daily;
the task expires if not picked up within an hour, logs sanitized failures,
and never automatically deletes archives. Keep this flag off until validated.
Choose retention/cost limits before enabling automatic remote deletion.

The backup command is prepared, not a claim that scheduled backups or a
restore drill are already running. No Railway plan upgrade is required for
an independently scheduled logical backup.
