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

### Restore evidence — 2026-09-13

The production Worker created a private archive in `axonic-private-backups`:
`postgres/2026/09/13/062137-01acc5f0ed2a4ac89ea82f887877b3ce.dump`.
Downloaded size: 15,059,731 bytes. The downloaded SHA-256 matched the stored
metadata. Anonymous access to the uploaded archive was denied.

The downloaded archive restored successfully with `--exit-on-error` into a
temporary PostgreSQL cluster inside the Worker, using a private Unix socket
and no TCP listener. All 30 public tables were readable (2,035 rows total),
including 17 users, 50 chat rooms, and 41 media records. All 56 migration
records matched production. These are snapshot counts, not ongoing totals.
The test PostgreSQL process was stopped; production was not restored over.

Worker and Beat were deployed with `DATABASE_BACKUPS_ENABLED=true`. The live
Beat settings showed the 08:00 UTC schedule and the live Worker registered
the task. A queue-dispatched test (`3d76f150-436c-42bf-b599-eb2d92074bed`)
succeeded in 2.26 seconds, creating a second verified archive at
`postgres/2026/09/13/063151-2840e6d6b9be4da9b1e07c1e84b026f2.dump`.
This verifies queue execution; the first clock-triggered daily run has not
yet occurred as of this setup check.

Daily scheduling requires the enabled flag on both deployed Worker and Beat;
verify live settings and task execution after changing it. Failure logging is
implemented, but an independent missed-backup alert and automatic retention
are not configured. No Railway plan upgrade is required for these logical
backups. Media object contents are not included in the database archive.
