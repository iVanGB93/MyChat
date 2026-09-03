# Local-first receipts and metadata rollout

## What the phone owns

- Message history and a frozen snapshot of intended recipient IDs from the first server acceptance.
- Each recipient's delivered/read flags and first-known timestamps in SQLite schema version 5.
- A durable queue confirming which delivery receipts the sender has saved locally.
- Rooms and contacts rendered from SQLite, with shared refreshes and a two-minute freshness window. Manual refresh and known mutations bypass that window.
- Call history merged locally instead of replaced or automatically trimmed to 1,000 entries.
- Profile/group image disk caching (the existing Expo Image memory/disk cache).

Group delivery becomes `delivered` when all expected recipients have acknowledged storage. The existing aggregate `read` behavior remains: a read receipt can advance the bubble to read, while individual recipients continue to be tracked and reconciled. Historical messages cannot recover a recipient list or exact timestamp that was never recorded.

## What remains on the server

Authentication, membership/permission checks, notification routing, offline recovery hints, pending delivery receipts, and temporary media availability must remain server-coordinated. A device cannot authorize its own group membership or reliably wake another device by itself.

The server keeps a delivery receipt until the sender confirms local persistence. Existing Beat cleanup then removes confirmed receipts after `MESSAGE_RECEIPT_CONFIRMED_RETENTION_DAYS` (default 7). Pending and unconfirmed legacy receipts are retained, even when older than the previous age-only limit. This favors recovery over immediate database reduction; monitor the unconfirmed backlog during rollout. `MESSAGE_DELIVERY_RETENTION_DAYS` no longer controls this task.

No message content was added to server persistence. Read receipts continue through the existing peer mutation/recovery system, not a new server message archive.

## Protocol and efficiency

- Axion `message_server_ack` includes `recipient_ids`; HTTP notification replies receive the same field.
- Retries include the saved `expected_recipient_ids`. This may narrow, but never expand, current server-authorized recipients. Newly joined members are not automatically sent an older accepted message.
- `receipts_stored` / `receipts_stored_ack` acknowledge locally saved receipt metadata, in bounded batches. Duplicate confirmation is safe.
- Incoming receipt bursts share `/api/chat/messages/ack-batch/`; older backends fall back to the single-receipt endpoint. The batch reduces HTTP overhead, not the number of per-receipt authorization/database checks.
- `/api/chat/rooms/sync/`, `/api/users/contacts/sync/`, and POST `/api/calls/history/` return changed rows and removed IDs, based on client-known hashes. Presence is excluded from hashes and remains driven by live, expiring Axion presence.
- These endpoints still read/serialize the authorized collection when a refresh is due; they are not database change-log cursors. Savings come from fewer checks and smaller response payloads. Legacy GET fallback follows all pages.
- Call history is retained on the phone when absent from a later server response. Server call retention is unchanged until durable backup/device-transfer and participant confirmations are designed.

## Deployment order

1. Deploy the backend normally and apply migrations, including `chat.0017_messagedelivery_sender_confirmed_at`. Keep Worker and the single Beat service running.
2. Update/reload the mobile client. This changes JavaScript and SQLite migrations only; it adds no new native dependency. Existing installed production clients need the normal app release to gain the behavior. Older clients keep working but do not enable confirmed-receipt cleanup.
3. Verify two-device private/group sends, partial group delivery, sender offline receipt recovery, notification replies, app restart, and foreground/background media receipt behavior. Automated tests do not replace this live rollout check.
4. New avatars use `PROFILE_MEDIA_STORAGE_BACKEND`, which defaults to `MEDIA_STORAGE_BACKEND`. With `spaces`, they use existing Spaces credentials/bucket and immutable `profile-objects/` keys. Stable public avatar URLs redirect to short-lived signed URLs; chat-media keys are rejected by that route.

### Existing avatar migration (optional, explicit operation)

After deployment, run on the server:

```powershell
python manage.py migrate_profile_media
```

This is a dry-run inventory. To copy existing referenced user/group avatars and switch references:

```powershell
python manage.py migrate_profile_media --apply
```

The apply operation uploads objects and updates database references. It intentionally retains original FileBlob rows for recovery; it does not immediately reclaim their database space. Do not remove originals until the new URLs have been verified. The command is restartable and skips already-migrated references.

Neither deployment nor this migration command was run as part of the local implementation.

## Limitations to preserve explicitly

- This is not a cloud backup or phone-transfer feature. Losing/clearing a device can still lose its local history.
- Server retention must not be shortened for pending delivery or calls merely because local storage exists.
- Avatar migration does not implement old-avatar object garbage collection; introduce a reference-aware, delayed cleanup separately.
- Collection freshness may lag changes from another device until the next due refresh when no realtime invalidation is available. Pull-to-refresh forces a check.
