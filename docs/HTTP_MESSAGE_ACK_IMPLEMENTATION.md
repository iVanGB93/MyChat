# HTTP Message Delivery Acknowledgment Implementation

## Overview
Implemented a robust HTTP-based delivery acknowledgment system that works even when the app is closed, with automatic retry queue and exponential backoff.

## Flow

```
User A sends message → Backend creates MessageDelivery(status=pending)
  ↓
Backend routes via WS/push
  ↓
User B receives push → savePushMessage() executes (even when app closed)
  ↓
Message persisted to local SQLite
  ↓
_tryAckMessage() attempts 3-part ack:
  1. Try HTTP POST /api/chat/messages/ack/ immediately
  2. If HTTP fails → enqueue in AsyncStorage (retry queue)
  3. Try WS ack for compatibility (notification socket fallback)
  ↓
Backend marks MessageDelivery.status = 'delivered'
  ↓
Backend emits message_delivery_ack to sender (WS/notif)
  ↓
Sender UI updates tick to "delivered"
```

## Backend Endpoint

### `POST /api/chat/messages/ack/`

**Request:**
```json
{
  "message_id": "uuid",
  "sender_id": 123,
  "room_id": "uuid",
  "delivered_at": "2026-06-17T12:34:56Z" (optional),
  "device_id": "uuid" (optional)
}
```

**Response (200 OK):**
```json
{
  "status": "delivered" | "already_delivered",
  "message_id": "uuid"
}
```

**Error Response (400):**
```json
{
  "error": "message_id, sender_id, and room_id are required",
  "status": "invalid_request"
}
```

**Features:**
- ✅ Idempotent (safe to call multiple times)
- ✅ Validates recipient is request.user
- ✅ Cleans up PendingDelivery when all messages delivered
- ✅ Notifies sender via WS immediately
- ✅ Returns 200 even if delivery record is gone (already processed)

## Client-Side Retry Queue

### `messageAckRetryQueue.ts`

**Queue Structure:**
```typescript
interface QueuedMessageAck {
  id: string;                    // unique retry ID
  message_id: string;
  sender_id: number;
  room_id: string;
  delivered_at?: string;
  device_id?: string;
  created_at: number;            // initial attempt timestamp
  last_retry_at?: number;
  retry_count: number;
  next_retry_at: number;         // when to retry next
}
```

**Retry Policy:**
- Max retry window: 24 hours
- Initial backoff: 1 second
- Max backoff: 60 seconds
- Exponential: backoff = min(1s × 2^retry_count, 60s)
- Drops after 24h with warning

**Public API:**
```typescript
enqueueMessageAck(ack)        // Queue one ACK
flushPendingAcks()            // Flush all ready ACKs
getQueueStatus()              // Debug: get queue contents
clearQueue()                  // On logout
```

## Integration Points

### 1. **Push Reception → HTTP ACK**
   - File: `pushMessageStore.ts`
   - After saving message to SQLite
   - Calls `_tryAckMessage()` which attempts HTTP first
   - Falls back to queue on HTTP failure
   - Also tries WS ack for compatibility

### 2. **Network Restoration**
   - File: `notificationWsManager.ts`, network listener
   - On internet restored, calls `flushHttpAckRetryQueue()`
   - Retries all queued ACKs

### 3. **WS Authentication**
   - File: `notificationWsManager.ts`, auth_ok handler
   - On auth success, calls `flushHttpAckRetryQueue()`
   - Ensures ACKs flushed when user reconnects

### 4. **Background Keepalive**
   - File: `notificationWsManager.ts`, `ensureWsAlive()`
   - Periodic flush every 20s in background
   - Catches stale queued ACKs before they expire

### 5. **Push Reception Task**
   - File: `backgroundNotificationService.ts`
   - After push save, calls `flushHttpAckRetryQueue()`
   - Attempts immediate HTTP ACK in background context

## Status Model (User-Facing)

```
sent       → Server accepted message
  ↓
routed     → Server attempted WS/push route (logged in MessageDelivery.routed_via)
  ↓
delivered  → Recipient persisted message locally (HTTP or WS ack received)
  ↓
read       → Recipient read the message (future phase)
```

## Error Handling

**4xx Client Errors (400, 404):** Dropped from retry queue (not retried)
**5xx Server Errors:** Kept in queue, retried with backoff
**Network Errors:** Kept in queue, retried on network restore
**No Token / Auth Error:** Queued, flushed after WS auth_ok

## Idempotency Guarantee

The HTTP endpoint:
- Checks if delivery already marked delivered (no double-update)
- Cleans up PendingDelivery only once all messages from sender delivered
- Returns 200 even if record not found (already processed)
- Client retries are safe — backend handles duplicates

## Testing Scenarios

1. **App Closed (No WS)**
   - Push arrives → savePushMessage() → HTTP ack
   - If HTTP fails → queued
   - On app open → WS reconnect → flushHttpAckRetryQueue()
   - Result: Delivered status eventually reaches sender ✓

2. **App Backgrounded**
   - Push arrives → savePushMessage() → HTTP ack + WS ack
   - Both paths redundant → double safety
   - Result: Fastest delivery status update ✓

3. **App Offline During Push**
   - Push arrives (FCM keeps device awake briefly)
   - savePushMessage() → HTTP fails (no internet)
   - Enqueued
   - On network restored → retry
   - Result: Delivery status sent as soon as possible ✓

4. **Large Queue Backlog**
   - Multiple messages received offline
   - Queue persisted across app restarts
   - Flushed periodically or on network restore
   - Max 24h window (old entries dropped)
   - Result: Delivery updates sent in batches, not dropped ✓

## Logging

All logs prefixed with `[AckRetryQueue]` or `[PushStore]`:
- Enqueue events
- Flush attempts (success/failure/dropped)
- Queue size on flush
- Age warnings on expiry

## Files Modified/Created

**Backend:**
- ✏️ `d:/Proyects/Axonic/chat/views.py` — Added `ack_message_delivery()` endpoint
- ✏️ `d:/Proyects/Axonic/chat/urls.py` — Added route `messages/ack/`

**App:**
- ✨ `d:/Proyects/Axonic-app/src/services/messageAckRetryQueue.ts` — New retry queue service
- ✏️ `d:/Proyects/Axonic-app/src/services/pushMessageStore.ts` — Added HTTP ack attempt
- ✏️ `d:/Proyects/Axonic-app/src/services/notificationWsManager.ts` — Flush on auth/network
- ✏️ `d:/Proyects/Axonic-app/src/services/backgroundNotificationService.ts` — Flush in push task

## Next Steps (Optional)

1. **Monitor & Telemetry**: Track HTTP ack success rate, retry latency, queue size
2. **Dashboard**: Show message delivery timeline (sent → routed → delivered → read)
3. **Exponential Read Receipts**: Extend to `read` status via similar mechanism
4. **Device Sync**: Per-device delivery status for multi-device users
