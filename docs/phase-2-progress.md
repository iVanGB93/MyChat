# Phase 2 implementation checkpoint

## Completed foundations

- Mobile text outbox uses a transport interface with the existing Axion adapter.
- Development builds can opt into P2P with `EXPO_PUBLIC_AXONIC_P2P_TEXT=1`.
  Production builds always ignore this switch. Existing persistence, media,
  receipts and retries remain active.
- `p2p_text_signal` is a separate authenticated Axion signaling namespace.
  The server checks exact two-person direct-room membership and blocking in
  either direction. It validates version, identifiers, SDP/ICE size and types,
  limits each socket to 120 signaling frames/minute, and supplies sender identity.
- Each Axion connection has its own server-generated endpoint ID. Offers can
  reach multiple installations; answers and later candidates target one endpoint.
- Mobile `p2pTextSession.ts` implements an isolated data-channel prototype with
  endpoint/session isolation, early ICE buffering, bounded queues, 10-second
  native connection timeout, 2-second storage-receipt timeout, 60-second idle
  timeout and 5-minute maximum authorization lease.
- `nativeTextPeer.ts` uses the installed WebRTC module with STUN only. No new
  native dependency, media track or call session is required.

## Not enabled or claimed complete

The prototype is now registered through an opt-in development composition.
Bidirectional delivery has been verified on two Android emulators and between
an emulator and one physical phone on cellular. Two-real-phone network coverage
remains unverified. A channel write is not delivery:
`send()` succeeds only upon a matching peer `stored` receipt. The prototype
callback must persist and deduplicate before returning true.

WebRTC transport encryption is not the planned transport-independent E2EE
layer. No peer relay, distributed storage, credits or blockchain is implemented.

## Current integration and next checkpoint

The backend deployment was reported complete and `/health/` returned `ok`.
The local mobile `.env.local` enables the experiment; set the flag to `0` and
restart Metro to disable it. No native rebuild or further backend change is
required for this integration.

Initial scope: both users must be foregrounded in the same direct room, without
an active call. Groups, quoted replies, hydration, background delivery and other
unsupported cases keep Axion. New P2P connections use server-authorized
signaling, waiting up to 3 seconds for a channel and 2 seconds for a storage receipt,
then falls back with the original ID. The runtime has a 5.5-second overall budget
for discovery/connection/receipt work. The existing local outbox remains durable.

The receiver reuses ingress with a `p2p` source, skips the HTTP delivery ACK, and
confirms the persisted row before returning a peer receipt. The sender stores
the per-recipient receipt locally without starting the server-acceptance timer.
Existing metadata/read-receipt synchronization can still use Axion.

Account, room visibility, lifecycle, block and call changes close sessions.
Room updates and successful Axion reauthentication also invalidate them.
Established sessions survive Axion loss within their idle/authorization bounds;
unfinished handshakes close. Axion remains necessary for new setup and fallback.

Development logs show `peer_stored`, `fallback_connection`, or
`fallback_no_receipt`; they do not include message contents.

Remaining work:

1. Extend device verification to simultaneous sends, missed peer
   receipts and late Axion duplicates. The versioned answer/data-channel
   handshake currently supplies capability confirmation; unsupported peers
   simply time out to Axion.
2. Established-channel delivery during an Axion outage now passes on emulators
   and between one physical phone on cellular and an emulator (checkpoints below).
3. Verify two real phones on same Wi-Fi, separate Wi-Fi and mobile
   networks; test timeouts, account changes, duplicates and calls alongside chat.
4. Add established application E2EE before completing the first major milestone
   or allowing any peer relay to carry user messages.

Unit tests use fake peer connections and a local in-memory Django database.
They validate protocol behavior, not native networking or delivery on phones.

## Emulator verification — 2026-09-27

Tested using existing emulator accounts (5554/user 18 and 5556/user 14),
development P2P enabled, and deployed backend 1.1.1. No reinstall or data reset.

- Reproduced a native-only failure: Android react-native-webrtc reports an empty
  `protocol` for remote data channels. The strict protocol check closed the peer
  channel and triggered Axion fallback. Accept missing native metadata while
  still rejecting an explicit mismatch and validating protocol/session/room on
  every application packet. Added two regression tests.
- Both foreground in the direct conversation: messages
  `decb90de-6f84-419d-9102-33357c27593a` and
  `9c279ceb-0552-4ed0-bc88-67080b70d644` passed in opposite directions.
  Each logged ingress `via p2p` and sender `peer_stored`, with no text fallback
  for those IDs. Both appeared in the receiving UI.
- Receiver backgrounded with Home: message
  `2e370d4c-eb06-4a40-9b35-1f59d753863b` used `fallback_connection` and
  arrived through `push_receive`. Android posted the matching notification,
  visually verified in its shade. Tapping it opened the correct conversation;
  the background message and earlier direct messages were present.
- Checks: 318 mobile tests, TypeScript and service dependency check (88 modules)
  passed. Earlier backend run passed 41 isolated tests. Railway deployment/logs
  were inspected; no backend deployment was performed by the agent.

This validates the narrow emulator scenario, not physical-phone reliability,
force-stop behavior, server-independent delivery, or the full Phase 2 milestone.

## Step 1 reliability checks — 2026-09-27

- Simultaneous sends: both emulator send buttons triggered concurrently.
  `024b38ea-26a3-47b0-bc04-059ab8ba54d9` delivered via P2P;
  `f32d6968-4b18-4220-ac7c-3ae8bdeaa1ba` reached the storage-receipt timeout
  and delivered via Axion. Both were visible once. Direct transport reliability
  under simultaneous traffic remains an open investigation; the reason for
  that missing receipt has not been established.
- Interrupted sending: briefly disabled sender Wi-Fi/data immediately after
  sending `1570fbc8-1445-4c1c-abe0-487d53c69036`, then restored both to their
  original enabled state. Reconnect flushed the retry queue. Logs showed an
  Axion submission and a P2P storage receipt for the same ID; receiver UI and
  SQLite contained a single copy. This tests recovery and duplicate handling,
  not continued operation without Axion.
- Read-only snapshots of both emulator message databases passed SQLite
  `quick_check` and contained exactly one row for each of the three IDs above.
- Voice call `34cd4351-b214-4921-89ce-6399436676d1` connected on both emulators
  and ended normally. Call logs reported a direct srflx/srflx connection.
  The existing call navigation guard prevents opening chat while a call is
  active, so in-call message sending could not be exercised through the UI.
  Audio quality was not assessed. Post-call message
  `08cf74e5-362f-4fc8-9adb-8a3dec370621` used P2P with `peer_stored`.
- Added five controlled tests: dropped/late storage receipt; concurrent
  P2P/Axion persistence; late Axion duplicate after simulated restart; call
  start/end P2P eligibility and cancellation; lifecycle/room/account cancellation.
  Native networking and storage are mocked in these tests. All 323 mobile tests
  passed. No application behavior was changed in this testing pass.

Before calling step 1 fully validated, investigate the simultaneous-send
receipt timeout and resolve the product/test limitation around in-call chat.
Both emulators remain running with their accounts and local data preserved.

## Readiness handshake follow-up — 2026-09-27

- Added session-stage diagnostics without message content, SDP or addresses.
  Two repeat simultaneous pairs passed before the handshake change, so the
  original receipt timeout was not reproduced with tracing enabled.
- Found a timing gap in the native bridge: the sender's channel can open and
  submit text before the receiver attaches its JavaScript channel handler.
  Successful traces demonstrated that ordering, but do not prove that it caused
  the original timeout. The answerer now sends a room/session-bound `ready`
  packet only after installing its message listener. The offerer waits for both
  local channel-open and this packet before submitting text. Missing readiness
  retains the bounded Axion fallback; storage receipts remain mandatory.
- Added four regression tests covering delayed readiness, wrong-room readiness,
  missing readiness, already-open remote channels and readiness before local
  open. All 327 mobile tests, TypeScript and the 88-module dependency check pass.
- Two simultaneous pairs with the new handshake delivered all four messages via
  P2P with matching `peer_stored` receipts and no fallback:
  `d5caf057-e6ca-4bf0-867d-eee11cb3cd45`,
  `ac3afd45-1546-4779-84a1-f16ef594bcb5`,
  `c3abd212-4817-4a40-b417-b049013886a9`,
  `a9c2ba02-9a03-48c5-a2d9-cba611a7bb07`.
  Read-only snapshots of both databases passed `quick_check` and contained
  exactly one row per test message.
- A development reload produced a SQLite "shared object already released"
  rejection before testing; both chats subsequently loaded and persisted the
  four messages successfully. Track this separately if it recurs outside reload.

The readiness gap is addressed; the original intermittent timeout's cause is
still unconfirmed. Physical-device/network coverage and the in-call chat test
limitation remain open. No backend deployment or native rebuild is required
for this JavaScript-only change.

## Physical phone on cellular — 2026-09-27

- Separate `com.axonic.dev` build installed with explicit user authorization,
  preserving the existing release app and data. Chaty (user 27) signed in;
  Expo and FCM push-token registration succeeded.
- Samsung phone used cellular with Wi-Fi off; USB forwarded only Metro port
  8081. Tested against emulator-5556 (user 14) in room
  `0d17565f-4f64-4027-84d2-23b616bcdd4a`.
- Foreground P2P passed both ways with readiness and matching storage receipts:
  emulator to phone `93992cd0-41f9-4466-8316-7082110d24fa`; phone to emulator
  `9baf5f6c-77d6-412b-9c9b-9b7571860f48`.
- With the phone app backgrounded, message
  `9288e412-e72e-4941-a315-de500fa86c59` used `fallback_connection`, produced
  a visible Android notification, and opened the correct chat when tapped.
  A background Axion socket abort/reconnect was logged; delivery still succeeded.
- Read-only database snapshots passed `quick_check` on both devices. Each of
  the two direct messages, background message and initial setup message appeared
  exactly once in each database.
- This covers one physical phone/carrier path, not general NAT compatibility.
  Signaling still uses Axion; background delivery still uses server/push.
  Force-stop delivery, physical-device simultaneous sends and the in-call chat
  limitation remain outside this checkpoint. No application code changed.

## Established P2P during Axion loss — 2026-09-27

- Successful text sessions remain reusable in both directions. Sending no
  longer requires Axion readiness when the development P2P experiment is enabled;
  the legacy adapter still requires it. Each outgoing row is persisted before
  trying direct delivery, and only a matching peer storage receipt marks delivery.
- Axion loss closes negotiating sessions but preserves ready ones. New sessions
  still require Axion. Missing receipts or a failed channel close the session;
  unavailable fallback leaves the original message pending in the local outbox.
- Idle sessions close after 60 seconds. A fixed 5-minute lease requires fresh
  server authorization even under continuing traffic. Activity does not renew
  that lease. Logout, room/lifecycle/block/call changes, room updates and Axion
  reauthentication retain session invalidation. This bounds stale authorization
  during an outage; it is not decentralized identity or application E2EE.
- All 334 unit tests pass, along with TypeScript and the 88-module dependency
  check. Added outage reuse, reverse-direction reuse, no-new-peer-during-outage,
  composition readiness, offline outbox receipt/queue and lifetime/idle coverage.
- Live emulator test used accounts 18 and 14 in their existing direct room.
  Temporary debugger instrumentation closed only each app's Axion WebSocket and
  redirected reconnect attempts to an unavailable loopback port. WebRTC and
  general network connectivity remained active. Constructors were restored in
  `finally`, with an additional automatic restoration timer. No production
  server, firewall, account credentials or device network settings changed.
- Both Axion readiness checks were false before and after simultaneous sends.
  Messages `00eb70e9-2a20-457a-b066-27c90d3efd0e` and
  `b16a0556-28b9-4c6e-b04d-eb006bcde890` reused session
  `0cafcf02-854c-4291-a1ac-80e502d42e2a`, logged native ingress via P2P and
  matching `peer_stored` receipts, and appeared in both UIs.
- Closing the direct peer during the outage left message
  `f4ae9196-fe33-457e-92cb-7f371480a5fe` queued. After restoration it delivered;
  concurrent recovery produced a P2P receipt and a late Axion hydration frame.
  Both database snapshots passed `quick_check` and contained exactly one row
  for each of the five messages in the completed run, including this overlap.
- This proves established-channel delivery under a simulated Axion outage on
  two emulators, not a full backend outage or physical-device outage reliability.
  Read-receipt metadata and unsupported content still rely on Axion. No backend
  deployment or native rebuild is needed; the experiment remains development-only.

## Physical cellular Axion-outage test — 2026-09-27

- Repeated the temporary Axion interruption test with Chaty (user 27) in
  `com.axonic.dev` on the Samsung phone and emulator-5556 (user 14), using
  room `0d17565f-4f64-4027-84d2-23b616bcdd4a`. Phone Wi-Fi remained off,
  cellular was active, and USB forwarded only the development server port.
- Both Axion sockets were closed and reconnects temporarily redirected to
  unavailable loopback. Both readiness checks were false before and after
  simultaneous direct sends. Phone message `ca8e2b36-9bea-4d45-b150-d648b16ae811`
  and emulator message `f48a8d5e-748e-480c-9e66-1dbd847b81a7` delivered using
  established P2P session `9f2acf10-3f4d-4b61-b73e-d539e612fd05`.
- Closing the phone's peer during the interruption left
  `60253090-af34-4c97-8964-ddf4df3a7831` queued. Restoring Axion recovered it;
  Axion and P2P recovery overlapped without duplicate database rows.
- Both databases passed SQLite `quick_check` and held exactly one row for
  each of the five test messages (two setup, two outage, one recovery).
  The phone UI displayed the messages and read receipts after recovery.
- Normal WebSocket constructors were restored, both Axion connections became
  ready, and temporary debugger helpers were removed. No app code changes,
  reinstall, production outage or backend deployment were required.
- Coverage is one real phone/carrier and one emulator, with a simulated Axion
  interruption. Initial discovery still needs Axion; this does not establish
  full server independence, all-backend outage behavior or two-phone reliability.

## Nearby Wi-Fi discovery implementation — 2026-09-27

The user deferred application E2EE and selected automatic same-Wi-Fi discovery
instead of manual pairing. Added an opt-in Android NSD/local-TCP signaling module
and a separate nearby P2P runtime that uses host candidates without STUN/TURN.
Existing cached direct-chat scope, local outbox, receipts, duplicate handling and
fallback are reused. Nearby identity claims are explicitly unverified; this is
for known test devices only, not a production or public-discovery rollout.

Both Android debug APKs build successfully, and 342 unit tests, TypeScript and
the 89-module dependency check pass. The phone's development chat control was
visually checked. Native installation and live NSD discovery remain pending
explicit user authorization; no apps were reinstalled and no backend deployed.
See `D:\Proyects\Axonic-app\docs\nearby-discovery.md` for artifacts, implementation
limits and the acceptance test. Do not claim fresh connections without Axion
have been verified until that live test passes.

## Nearby installation and first live checks — 2026-09-27

With explicit user approval, installed the nearby debug APKs using replacement
installation on the phone's `com.axonic.dev` and both emulators' `com.axonic`.
Existing accounts/messages were preserved and the phone release app was untouched.
Native module availability and foreground UI were verified on all three devices.

Phone Wi-Fi discovery resolved the test emulator's advertisement, but discovery
was not reciprocal. Both emulators run 36.4.9 with separate private networks and
the same `10.0.2.16` address. Their own fresh-discovery test, with existing P2P
sessions cleared and Axion interrupted, also failed to find reciprocal peers.
It aborted before message sends and restored Axion. Shared emulator Wi-Fi is
documented from 36.5; this topology does not establish two-phone behavior.

Phone Wi-Fi-loss and background cleanup checks passed; backgrounding removed
native NSD requests. Returned the app to the foreground, restored Wi-Fi, stopped
Nearby everywhere, removed test helpers, and confirmed Axion ready on all devices.
Fresh server-independent message delivery remains unverified and needs a second
physical Android test device on the same Wi-Fi or a validated shared emulator
network. No backend deployment or production outage was performed.

## Two-phone nearby messaging verified — 2026-09-27

Second physical phone `R3GL10CNS1K` joined the LAN with PC (user 22) signed into
the separate development build; first phone remained Chaty (user 27). Cached
their direct conversation `8718e58d-c1d2-4e0b-911f-4608eb273759` before testing.

Cleared old P2P sessions and interrupted Axion on both phones before enabling
Nearby. Both discovered each other and established fresh local connections for
simultaneous messages in both directions. Axion readiness was false before and
after sends. Nearby persistence acknowledgments confirmed delivery of
`c2941a10-0599-4c21-bebb-8236b71bfc82` and
`cf44119a-98ec-49af-a5a3-463a8d002a4f`.

Closing the direct sessions left `e62d9743-00c9-4d2d-833c-5d96ce9501d7` queued
until Axion returned. Both phone databases passed integrity checks and contain
exactly one row for each of the three messages, all read after recovery.
Restored normal Axion connectivity, stopped Nearby and removed test helpers.

Fresh same-LAN setup and text delivery without Axion are now verified on two
physical phones. This does not establish complete server independence: login
and contact/room setup used the backend; other internet access remained available.
No deployment, production outage, account wipe or release-app replacement.

## Nearby text with internet blocked — passed 2026-09-27

User blocked internet for both physical phones at the router while retaining LAN
access. Disabled mobile data on both, cleared existing P2P sessions, and enabled
Nearby afterward. Without any WebSocket override, both phones failed cache-busted
API-health and external HTTPS probes before and after delivery; Axion stayed down.

Reciprocal discovery, fresh local connections, and simultaneous text delivery
passed. Messages `f038c0e4-9114-4106-8305-c8640e4b02f2` and
`9b595609-d576-4902-a9c1-ab7b458b78e1` received nearby persistence acknowledgments,
with exactly one copy on each phone and both SQLite integrity checks passing.
Remote read receipts remain unverified offline; sender rows were delivered.

After closing the direct connection, `d1a530ff-0e9b-4416-9ca9-db1bb1f0ea77` stayed
pending on the sender and absent on the receiver. After the user restored internet,
verified exactly one copy of this queued message and both offline-delivered
messages on each phone, all marked read. Both recovery database integrity checks
passed. Stopped Nearby, removed test helpers, restored the original mobile-data
settings, and confirmed Axion ready on both phones and emulators with no outage
overrides. Signed-in cached-chat use is verified; offline login,
new-contact setup and calls are not covered by this milestone.

## Nearby lifecycle interruption checks — 2026-09-27

On the two physical phones, independently tested backgrounding, leaving the chat,
and Wi-Fi disconnect/reconnect while Axion was temporarily unavailable. Each
interruption disabled Nearby and caused the next PC message to queue. Returning
did not automatically enable Nearby; explicitly enabling it again restored
discovery and bidirectional local delivery before Axion was restored.

All 12 direct before/after sends had nearby storage acknowledgments. After Axion
restoration, all 15 test messages, including three queued messages, appeared
exactly once in each phone database and were read. Both integrity checks passed.
Restored normal connections and Wi-Fi, stopped Nearby, removed debugger helpers.

Manual recovery passes. Automatic resumption and automatic local outbox retry
remain future work; these tests do not claim either. Wi-Fi rediscovery briefly
showed two endpoints for one peer, suggesting stale advertisement handling needs
attention before automatic reconnection. These scenarios interrupt established
connections between messages, not persistence mid-flight or process termination.

## Automatic nearby resumption and outbox recovery — 2026-09-27

Added session-scoped opt-in recovery for the same account/chat. Returning from the
background, returning to that room, or restoring Wi-Fi now automatically resumes
Nearby. Native work still stops while ineligible; Stop, account changes and real
block-list changes clear consent. Recovery retries pending text with original IDs
through the existing send lock and durable receipt bookkeeping, in bounded batches.

Phone tests caught and fixed a block-cache refresh cancelling unchanged consent
and stale native discovery ports blocking reconnection. Native code now tries
other endpoints after failed connection attempts and explicitly chooses IPv4
from Android's address list. Built and updated both physical development apps,
preserving accounts/data; production apps and backend were unchanged.

Final background, Wi-Fi and room-reentry tests all resumed automatically and
delivered queued messages before Axion returned. All 15 messages have nearby
storage acknowledgments and exactly one row on each phone; both integrity checks
passed and all became read after normal connections returned. 350 automated
tests, TypeScript, 90-module dependency check and ARM64 debug build pass.

Stopped Nearby and cleared its consent after testing; Axion restored. These new
tests simulated Axion loss, not a repeat full internet block. Recovery is limited
to the foreground and the current process; background delivery, process restart,
offline identity setup, calls and application E2EE remain separate work.

### Automatic LAN preference — 2026-09-27

Removed the mobile development Nearby Enable/Stop control. Eligible foreground
cached direct chats now initiate LAN discovery automatically, retry unavailable
Wi-Fi with bounded backoff, and cancel/re-evaluate discovery on account, room,
block-list, lifecycle or call changes. Text still prefers an available LAN peer,
then an Axion-assisted WebRTC peer, then Axion delivery. Initial sends can fall
back while discovery is pending. No native rebuild or backend deployment needed.

355 mobile unit tests, TypeScript and the 90-module service cycle check pass.
The new no-toggle behavior has not yet been retested on physical phones.

User clarified the next routing goal includes provider private IP networks and
VPNs. Discovering and verifying reachable endpoints for those networks, comparing
paths, and server-independent internet discovery remain unimplemented. A private
IP alone is not evidence of peer reachability. Application E2EE remains deferred;
LAN identity claims remain unauthenticated and development-only.

### Host-address preference and no-toggle phone validation — 2026-09-27

The development text path now tries discovered LAN first, then a bounded
Axion-assisted host-only WebRTC session (no STUN/TURN), then ordinary STUN-enabled
WebRTC, then Axion delivery. Host-only setup has a 1.5-second open window;
ordinary setup gets 3 seconds; the combined assisted attempt is bounded at
7 seconds including lookup/receipt. Ready sessions are reused. Host-only SDP
preference is acknowledged by both peers; older/non-acknowledging peers fall
back to a fresh ordinary session. Existing signaling preserves the SDP extension,
so no backend deployment or native rebuild was needed.

Physical PC/22 and Chaty/27 tests passed without calling Nearby.start: initial
LAN discovery, background return and Wi-Fi loss/recovery. Queued messages reached
delivered while Axion was temporarily disconnected. With Nearby bypassed, native
stats confirmed a private Wi-Fi host/host pair, 16 ms RTT, and no public-discovery
attempt. Injected host-offer failure correctly fell back to ordinary WebRTC.
All 14 messages had matching peer-storage receipts and one row per device;
both databases passed integrity checks and all rows were read after recovery.
Wi-Fi/Axion restored, automatic discovery active, test globals removed.

363 mobile tests pass; TypeScript and the 90-module dependency check pass.
This is a route preference foundation, not complete serverless cross-network
discovery or shortest-path measurement. Platform-exposed provider/VPN addresses
may participate as host candidates, but actual ISP/VPN topology testing remains.
No credentials, account data, release installations, or backend deployment changed.

### Wi-Fi/cellular direct text verified — 2026-09-27

PC/22 remained on Wi-Fi and Chaty/27 on cellular-only throughout the confirmed
cross-network test. Host-only setup timed out, STUN-enabled direct WebRTC
succeeded, and native stats showed an IPv6 srflx/host pair over Wi-Fi/cellular.
Two messages succeeded with Axion available; two further messages succeeded
with Axion deliberately disconnected on both phones, each with a peer-storage
receipt. The current connection can survive signaling loss on this topology;
fresh server-independent discovery is still not implemented.

A preliminary two-message run was classified provisional because Chaty rejoined
Wi-Fi during it. All 6 messages across both runs exist once in each database;
both integrity checks pass. Normal Axion restored and temporary hooks removed.
Chaty remains cellular, PC Wi-Fi. No app code or backend changes required.
Next validation is a shared VPN/private routed topology; IPv4-only and other NAT
conditions are not covered by this IPv6 result.

### Both phones on cellular — 2026-09-27

User requested both Xfinity phones on mobile data. Verified Wi-Fi absent on both
before and after. Host-only attempt timed out at 1.5 seconds; STUN-enabled WebRTC
selected IPv4 srflx/srflx on cellular, with no relay. Two messages succeeded, then
two further messages succeeded with Axion disconnected on both devices. All four
messages had peer-storage receipts, were present once per database, and were read
after reconnect; integrity checks passed. Normal Axion restored, hooks removed,
Wi-Fi remains off on both. No application changes or deployment.

The result confirms direct IPv4 cellular messaging on this pair, not a private
carrier route. Candidate stats cannot establish whether mapped-address traffic
stays within the provider. The bounded host failure also does not prove private
reachability impossible. Fresh signaling still uses Axion. Evidence/details in
mobile docs/nearby-discovery.md and ignored builds/cellular-pair-evidence.json.

### First distributed-Axion foundation: foreground participation — 2026-09-27

The user clarified Axion's intended architecture: users' devices collectively
provide identity verification, discovery, relaying and encrypted offline custody.
Authorized all four test devices for the next step. Implemented the first piece:
Axion-assisted P2P now receives for cached direct rooms anywhere in the foreground
app. Screen navigation does not close sessions. Reuse is keyed to room and peer;
incoming persistence rechecks cached membership. Existing native LAN discovery
retains its single most recently opened room when navigating to non-chat screens.
It is not yet network-wide discovery. Background/calls/account/block teardown
remains in force.

All four devices passed cold chat-list direct delivery and continued delivery
with Axion disconnected (8 messages), with unread behavior verified. Two more
phone messages passed on LAN after leaving the chat screen with Axion unavailable.
All 10 stored once on intended devices, integrity checks pass. 368 mobile tests,
TypeScript and 90-module cycle check pass. Normal Axion restored, test hooks gone,
phones returned to cellular-only; no reinstall/deployment/account-data changes.

The encrypted third-party mailbox is the next piece, not delivered by this change.
Current builds expose no JS crypto/getRandomValues/subtle and lack expo-crypto;
choose reviewed cryptography/platform randomness and protected key persistence
before ciphertext-only bounded durable custody, authenticated retrieval, expiry,
and recipient-signed receipts. Never label a custodian's storage acknowledgement
as recipient delivery. No third-party message storage is enabled yet.

### Encrypted peer mailbox prepared — 2026-09-27

Implemented the next isolated development slice in the mobile repository:
AndroidKeyStore identity keys, platform hybrid encryption/signatures, explicitly
pinned test identities, durable ciphertext custody, bounded expiry/quotas,
recipient-signed receipts and foreground retry over distinct mailbox P2P sessions.
Recipient persistence validates the original cached direct room. Custodian storage
never marks sender delivery. The developer API does not yet integrate the normal
Send button or outgoing chat status. Existing Axion signaling still discovers
fresh sessions; no DHT/serverless identity/bootstrap or background hosting claim.

383 mobile tests and four Kotlin crypto JVM tests pass, as do TypeScript and the
93-module cycle check. Phone com.axonic.dev/arm64 and emulator com.axonic/x86_64
development APKs are built and verified under ignored builds/. All four devices
are connected; no installs or backend deployments were performed. Installation
requires explicit user permission under AGENTS.md before real-device Keystore and
three-device offline custody testing. Full detail and test procedure:
`D:\Proyects\Axonic-app\docs\mailbox-prototype.md`.

### Four-device mailbox validation passed — 2026-09-27

User explicitly approved installing the prepared updates. Updated both phones'
com.axonic.dev and both emulators' com.axonic in place; preserved signed-in
accounts and local data. Public keys were paired between the four test accounts.
Created the authorized 22–14 direct test room needed for the original chat.

With recipient 14 force-stopped, sender 22 deposited encrypted text at custodian
27 and remained pending. Both custodian 27 and fourth device 18 failed native
decryption; a forged receipt signed by 18 was rejected. Stopped sender, restarted
custodian, confirmed stable keys and retained ciphertext, then restarted recipient.
Custodian delivered over WebRTC; recipient saved exactly one unread chat message
and signed a receipt. Stopped recipient and restarted sender: custodian forwarded
the retained receipt and sender status became delivered. A repeated P2P envelope
after recipient restart produced no duplicate. Eight database quick_checks pass.

All four apps are foreground on chat lists, signed in, Axion connected; test nodes,
retry timers and globals are removed. Phones stayed cellular; emulator networks
were unchanged. No backend deployment or app code fix was required for this test.
The offline parties were force-stopped apps; signaling infrastructure stayed
available. This is not proof of serverless bootstrap or closed-app reception.
Normal Send-button integration remains the next slice. Detailed evidence and
limitations are recorded in the mobile `docs/mailbox-prototype.md`.

### Normal Send-button mailbox integration passed — 2026-09-27

Normal plain-text sends now try direct P2P, then explicitly paired encrypted
mailbox custody, then legacy Axion. Custody keeps the existing bubble pending;
only the recipient's verified signature confirms delivery. Durable digest bindings
retain the original message ID/content/timestamp, suppress redundant server copies
during valid custody (including cold-start recovery before re-pairing), and prevent
edited/deleted/other-account messages from inheriting an old envelope or receipt.

Actual phone Send-button test 22 → custodian 27 → recipient 14 passed. Recipient
was stopped for submission; sender restarted and retried without server handoff;
recipient later received automatically while sender was stopped. With recipient
stopped again, the custodian returned its saved signature to the restarted sender,
updating the normal chat bubble/list to delivered. Original ID/timestamp preserved,
exactly one chat row per endpoint, no chat row on relay/fourth device, eight database
integrity checks passed. 395 mobile tests, TypeScript and 94-module cycle check pass.

No native build/install or backend deployment for this slice. All four apps restored
to signed-in foreground chat lists, Axion ready, test forwarding/monitors stopped.
Explicit pairing is still needed to resume mailbox participation after restart;
automatic trusted-peer resumption is next. Fresh connections still use Axion
signaling. Details/evidence: mobile `docs/mailbox-prototype.md`.

### Hosted FirstNeuron custody — verified 2026-09-27

The DigitalOcean development neuron now uses the same mobile mailbox protocol
source, vendored with source hashes and a drift check. FirstNeuron account 34 was
verified through the existing API during provisioning; subsequent mailbox traffic
uses pinned device signatures, not backend authentication or Axion signaling.

Two emulators (18 and 14) completed encrypted custody, hosted service restart,
recipient return while sender was stopped, and signed delivery receipt return.
The normal send function also passed: the original outgoing row became delivered,
with no legacy `send_message` frame during initial submission. A historical receipt
outside the current paired set was found to block flushing; the shared engine now
skips it, with regression coverage. 398 mobile tests and six hosted tests pass,
along with TypeScript, the 95-module cycle check, and shared-source parity.

Deployment remains loopback-only, reached through an SSH tunnel. This proves
hosted ciphertext custody, not independent public reachability. The dashboard now
shows actual counts/capabilities and requires a masked login with an expiring
session. The service uses Node 22 under a restricted non-root account. General
discovery, peer signaling, live relay, FirstNeuron's own chat client and automatic
mobile pairing resumption remain future work. Next: public HTTPS peer ingress,
keeping the administrative dashboard private. See Axonic-neuron/README.md.

Final readiness caveat: sender emulator-5554/user 18 now requires sign-in after the
final development reload; the other three test accounts are connected. This occurred
after verification of the delivered normal-message row. No reinstall or wipe.
