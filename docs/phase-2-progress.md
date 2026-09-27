# Phase 2 implementation checkpoint

## Completed foundations

- Mobile text outbox uses a transport interface with the existing Axion adapter.
- Chat still selects Axion only. Existing persistence, media, receipts and retries remain active.
- `p2p_text_signal` is a separate authenticated Axion signaling namespace.
  The server checks exact two-person direct-room membership and blocking in
  either direction. It validates version, identifiers, SDP/ICE size and types,
  limits each socket to 120 signaling frames/minute, and supplies sender identity.
- Each Axion connection has its own server-generated endpoint ID. Offers can
  reach multiple installations; answers and later candidates target one endpoint.
- Mobile `p2pTextSession.ts` implements an isolated data-channel prototype with
  endpoint/session isolation, early ICE buffering, bounded queues, 10-second
  connection timeout, 2-second storage-receipt timeout and 60-second lifetime.
- `nativeTextPeer.ts` uses the installed WebRTC module with STUN only. No new
  native dependency, media track or call session is required.

## Not enabled or claimed complete

The prototype is not registered in the normal chat transport manager. It has
not exchanged messages on real devices. A channel write is not delivery:
`send()` succeeds only upon a matching peer `stored` receipt. The prototype
callback must persist and deduplicate before returning true.

WebRTC transport encryption is not the planned transport-independent E2EE
layer. No peer relay, distributed storage, credits or blockchain is implemented.

## Next integration checkpoint

1. Add an opt-in development runtime that dispatches only `p2p_text_signal` to
   bounded sessions and leaves call signaling untouched. Bind sessions to the
   signed-in account and close them on logout, background, membership/block
   changes and Axion reconnection (endpoint IDs are connection-scoped).
2. Establish authenticated peer capability/eligibility before trying P2P. Keep
   groups, notification replies and unsupported peers on the current path.
3. Integrate local ingress and durable recipient receipts. Do not send a P2P
   receipt through the current HTTP acknowledgement endpoint as though Django
   had relayed that message. Do not reuse the server-acceptance timer for P2P.
4. Select P2P with a bounded attempt, then fall back through the existing Axion
   adapter using the same message ID. Reconcile a late receipt and duplicate
   delivery without duplicate rows, notifications or status regression.
5. Revisit Axion readiness gates in sending/recovery before claiming that an
   established P2P channel works during an Axion outage.
6. User deploys the backend changes before device signaling tests. No migration
   is required. Verify two real phones on same Wi-Fi, separate Wi-Fi and mobile
   networks; test timeouts, account changes, duplicates and calls alongside chat.
7. Add established application E2EE before completing the first major milestone
   or allowing any peer relay to carry user messages.

Unit tests use fake peer connections and a local in-memory Django database.
They validate protocol behavior, not native networking or delivery on phones.
