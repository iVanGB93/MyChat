"""Per-row metadata deltas. No message content or permanent change log."""
import hashlib
import json
from rest_framework.response import Response


VOLATILE = {"is_online", "last_seen", "presence_updated_at", "presence_expires_at",
            "notification_socket_connected", "app_state", "presence"}


def stable_metadata(value):
    if isinstance(value, dict):
        return {key: stable_metadata(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, list):
        return [stable_metadata(item) for item in value]
    return value


def metadata_delta(request, rows, id_field="id"):
    known = request.data.get("versions", {})
    if not isinstance(known, dict) or len(known) > 10000:
        return Response({"error": "versions must be an object with at most 10000 entries"}, status=400)
    versions, changed = {}, []
    for row in rows:
        key = str(row[id_field])
        digest = hashlib.sha256(json.dumps(
            stable_metadata(row), sort_keys=True, separators=(",", ":"), default=str,
        ).encode()).hexdigest()[:24]
        versions[key] = digest
        if known.get(key) != digest:
            changed.append(row)
    return Response({
        "upserts": changed,
        "removed_ids": [key for key in known if key not in versions],
        "versions": versions,
    })
