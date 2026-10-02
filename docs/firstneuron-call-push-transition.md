# FirstNeuron call-push transition

## API routing activated and live-tested (October 2, 2026)

With the user's approval to activate and test, set `NEURON_REGISTERED_PUSH_ENABLED=true` on Railway's MyChat API service and applied its single staged variable change. Railway reported a successful deployment and backend health returned 200. Only emulator users 18 and 14 had verified bindings at activation; unbound installations continue using legacy addresses. This flag is service-wide, not a permanent user allowlist: future verified bindings also use registered routing. Worker/Beat settings were not changed, so background retry jobs have not been migrated by this activation.

Normal production REST message delivery from emulator 18 to backgrounded emulator 14 returned 200, produced one FirstNeuron sent receipt, displayed the exact labeled test notification, and appeared in the receiver's local message store. Message ID: `0a285f77-7146-4e1f-b215-bef7019946a0`. A normal voice-call initiation produced a visible Android incoming-call notification containing its exact call ID and Accept/Decline actions; the call was then ended. Call ID: `82edbd43-834c-4f9c-a308-da5025ef24f1`. The FirstNeuron receipt digest matched the reconstructed version-2 payload including the authenticated bridge's user/installation target, proving the new routing path was used. An earlier call `f4c33da8-9d00-4660-ab46-2af3677b291d` was ended before notification observation; its gateway receipt was sent, but UI evidence is claimed only for the second call. Both calls were ended and the receiver restored to the foreground.

Evidence is in ignored mobile `builds/registered-routing-live-test.json` and `builds/registered-routing-railway.png`. These tests establish background message delivery and call invitation notifications, not connected call audio/video, force-stopped-app behavior, or Django-independent calling. No application rebuild/reinstall or source commit was performed. Roll back API selection by setting the flag to false and applying that service's variable update. Next: align and verify worker retry routing before claiming all notification paths are migrated.

## Background wake retention deployed (October 2, 2026)

The gateway now separates the 24-hour registration refresh deadline (`until`) from private push-address retention (`wakeUntil`). Fresh authenticated registration permits delivery for up to 30 days, capped by that signed identity record's expiry. Ordinary mobile hourly refresh remains unchanged. Older rows without `wakeUntil` retain their original deadline until the device actually registers again; deployment does not silently extend them. Every lookup still checks the latest locally pinned signed record and authorized device. This does not grant an expired identity/session network admission.

Logout removes the registration, known device revocation blocks delivery immediately, and definitive FCM invalid-token responses remove the exact address used. A late failure for an old address cannot remove a rotated replacement. Unknown/ambiguous provider results do not remove registrations or trigger duplicate sends. The longer offline lease also means that unreachable best-effort logout can rely on token deletion or expiry for up to the remaining lease; revocations unknown to this host cannot be inferred.

Deployed with identity preserved and health passing; rollback `/opt/axonic-neuron-before-wake-20261002`. All 65 hosted tests passed, including simulated 25-hour inactivity, unrelated registration, restart, token ownership, rotation, logout, known revocation, legacy-row compatibility and signed-record expiry. Shared-core parity passed. Both development emulators were reloaded without reinstalling or changing accounts and renewed automatically. Read-only resolution of their real registrations at a simulated clock 25 hours ahead succeeded for both; roughly 710 hours remained on each lease. No host clock or stored timestamps were changed, and this was not a real 25-hour device sleep test.

The one-day retention blocker is resolved, superseding the older notes below. This remains bounded offline support: an app must reconnect before its signed identity/wake lease expires to remain push-addressable. `NEURON_REGISTERED_PUSH_ENABLED` has not been enabled globally; controlled normal-chat and call testing of that backend switch remains next. No backend redeployment or mobile production rebuild is required for this hosted retention change itself.

## Verified migration binding prepared (October 2, 2026)

After the user confirmed backend deployment, both development emulators automatically created durable bindings (users 18 and 14). FirstNeuron remained healthy with two live registrations. A labeled version-2 message notification to backgrounded emulator user 14 deliberately supplied an invalid legacy fallback token. The gateway resolved its private registration, FCM accepted it, and Android displayed the exact test text. Repeating the identical signed request returned the stored outcome with one matching receipt and one notification entry. Test message ID: `5ca36cfe-e949-4e55-8d2b-cb825f444f5e`. This was a direct gateway notification fixture, not a normal Django chat-send or call test. The receiver was returned to the foreground. Global routing settings were not changed; the 24-hour registration lease remains the next production-cutover issue.

`POST /api/users/neuron-binding/` now requires a Django login and an active installation owned by that user. It issues a 60-second, domain-separated HMAC ticket for the requested cryptographic account and the hash of that installation's current FCM token. The ticket contains no token or password and is marked no-store. The mobile foreground registration worker obtains this ticket automatically and submits it on its authenticated axon. Older backends remain compatible: direct registration continues and binding is retried.

FirstNeuron verifies the ticket against the authenticated account and submitted registration token. It durably pins the legacy user/installation to that account/device, rejects conflicting ownership claims, and does not publish these private migration anchors to the identity directory. Reinstallation with a different identity or switching accounts on the same installation requires a future explicit migration/recovery policy; there is deliberately no silent overwrite. This is transitional Django-account linkage, not an authority over cryptographic identities.

Both gateway routes accept authenticated version-2 jobs containing a legacy user/installation target. FirstNeuron selects that target's current directly registered token. Unbound installations use the supplied legacy fallback token; bound but revoked, unauthorized or expired registrations fail closed. Duplicate receipts remain durable across restart and contain neither tokens nor payload content. Django still supplies the fallback token and selects active installations/preferences; this does not yet remove its device table or notification responsibilities.

FirstNeuron support is deployed, health passed, identity unchanged, rollback `/opt/axonic-neuron-before-binding-20261002`. Both emulator registrations remain present. Unsigned public call/message requests return 403. Validation: 650 mobile tests, 60 hosted tests, TypeScript, service-cycle checks, shared-core parity, and isolated backend migration/push tests. No new production build, account replacement or application reinstall was performed.

Next deployment: deploy the backend through the normal Railway workflow; no schema migration or new secret is required. Leave `NEURON_REGISTERED_PUSH_ENABLED` absent/false initially. Once the ticket endpoint is live, verify both emulators bind automatically. The optional flag enables version-2 target routing; do not enable it broadly until live background delivery and long-inactivity handling are verified. Current direct registrations expire after 24 hours without foreground renewal, and bound expired registrations intentionally cannot fall back around that lease. Address that wake/renewal policy before production cutover. The opt-in local-account receiver/unlock design remains separate.

## Direct device registration added

The development mobile runtime now registers its FCM token directly with FirstNeuron over the authenticated axon, using negotiated push-registration-v1 support. The gateway derives account and device from the authenticated peer; the request cannot supply another owner. Only a device authorized by the latest locally pinned signed record can register. Tokens stay in private gateway storage and are never included in the public identity directory.

Registration replaces that device's previous token, refuses a token already bound to a different device/account, refreshes hourly and after token rotation, and expires after 24 hours without renewal. Backgrounding preserves the registration. Logout requests revocation before teardown and attempts Firebase token deletion, with bounded waits; offline revocation is best effort and lease expiry is the fallback. Old clients remain compatible. The opt-in local-account recovery prototype is not wired to this legacy-notification registration yet, because its locked-account receiver still needs a dedicated design.

FirstNeuron was deployed with unchanged identity and rollback /opt/axonic-neuron-before-registration-20261002. Both development emulators automatically registered their distinct cryptographic identities; no tokens were exposed in evidence. 648 mobile tests and 56 hosted tests passed, along with TypeScript and the dependency check. Token rotation, cancellation, revocation, device authorization and expiry were tested using disposable fixtures. Existing emulator accounts were not logged out or replaced.

This is parallel registration, not destination cutover: production notifications still obtain addresses through the working Django bridge. Django relays chat identity proofs but has no durable authoritative mapping from numeric users to these cryptographic accounts. A verified migration binding is required before the old call/message requests can select the new registry safely. Do not remove Django push registration or credentials yet. Production phones need these mobile changes in a subsequent app build; no native rebuild is required for development hot reload.

## Activation verified

After the user deployed the backend and configured Railway on October 2, 2026, live tests confirmed both bridges. A call invitation from emulator user 18 to emulator user 14 produced one FirstNeuron FCM-accepted call receipt and an Android incoming-call notification while the receiver was backgrounded. The test call was ended afterward.

A labeled test message through the production REST message-send path produced one FirstNeuron FCM-accepted message receipt. The background receiver displayed the exact test content with Reply and Mark as read actions, and the message was found in its local database. The receiver was returned to its existing app afterward. This verifies background delivery, not force-stopped or terminated-app behavior. It does not prove Django-independent call coordination or peer-only push triggering. Local test evidence: Axonic-app/builds/production-push-bridge-test.json (ignored).

## Completed October 2, 2026

FirstNeuron now hosts a dedicated, optional FCM call-push gateway. It is a transitional hosted capability, not an identity authority. Ordinary phones never receive the Firebase service credential or bridge secret.

The bridge preserves the current production app's incoming_call data-only payload. Django still selects active devices and applies existing call notification preferences, then delegates the FCM send when configured. No device-table copy is required: the token exists in the gateway request only. Receipts contain hashes and outcomes, not tokens, names, or message content. Receipt retention is ten minutes with a bounded capacity of 1,000 entries.

The server accepts a dedicated HMAC-authenticated POST at https://143.198.121.2/v1/call-push, behind TLS and request limits. It restricts payloads to incoming calls, checks short expiry, and persists a per-call/token reservation before sending. Ambiguous sends are not retried through Django; an accepted/unknown outcome is not proof that a phone rang. The existing Expo path remains for installations without an FCM token. Message push still uses Django.

## Activate after backend deployment

Deploy the prepared backend changes through the normal Railway workflow. Set these three Railway variables from the owner-only, ignored local file `.env.neuron-push`:

- NEURON_CALL_PUSH_URL
- NEURON_MESSAGE_PUSH_URL
- NEURON_PUSH_BRIDGE_SECRET

Do not paste the secret into chat, commit it, or include it in an app build. Keep Django's existing Firebase credential: message delivery and diagnostics still use it. No mobile build is required for this bridge. Production routing remains unchanged until these variables are enabled on the deployed backend.

Rollback the backend route by clearing NEURON_CALL_PUSH_URL. Let in-flight requests settle before testing again; never retry an ambiguous invitation through both senders.

## Deployed host

- Code rollback: /opt/axonic-neuron-before-push-20261002
- Optional gateway listener: 127.0.0.1:8083
- Protected credential: /etc/axonic-neuron-fcm.json (root-owned, service-group readable)
- Protected bridge configuration: /etc/axonic-neuron-push.env
- Systemd drop-in: /etc/systemd/system/axonic-neuron.service.d/push.conf
- Proxy rollback: /etc/nginx/conf.d/neuron-https.conf.before-push-20261002

## Evidence and remaining work

Read-only PostgreSQL audit: 33 active registrations, 21 with FCM tokens. No user/password/message exports or database writes were performed during this audit. FirstNeuron's identity hash was unchanged after deployment and health passed. Firebase validate_only accepted a test-emulator token without delivering a notification. Public HTTPS rejected an unsigned request (403), and authenticated then rejected an invalid body (400). No real call was placed during this step.

Validation: 51 hosted tests, 16 isolated backend push/call tests, shared mobile/hosted protocol parity, and zero known production dependency vulnerabilities.

Next: enable and verify the bridge using emulator calls; implement identity-bound direct token registration, refresh and revocation; then move signed call invitations, call state, WebRTC signaling and local history off Django/Axion. Background calls for fully local accounts still require a receiver/unlock design. This bridge does not complete those stages or move audio/video relay responsibilities.

## Message-notification bridge added

FirstNeuron now also accepts authenticated POST /v1/message-push on the same protected listener. It uses a separate receipt file and distinct per-message/token IDs. Django's send_message_push delegates compatible new_message FCM payloads when NEURON_MESSAGE_PUSH_URL is enabled. The current content, media metadata, sender, reply fields and notification coordination fields remain data fields for the existing app receiver; this transitional bridge does not introduce end-to-end encryption for legacy notification payloads. FirstNeuron does not persist that content. Firebase's existing default message TTL is preserved, and message notifications are not collapsed together. Calls retain their short TTL.

Before any remote send, unsupported/missing identifiers, conflicting aliases, oversized payloads or blocked media fields select the unchanged Django FCM sender. No fallback occurs after an ambiguous remote attempt. Expo-only devices and silent message_recovery_hint notifications still use Django. The latter still reconnects Axion, so moving it requires a mobile recovery change rather than only a provider switch. Enabling this bridge does not yet eliminate Django's device registration or notification-selection responsibilities.

Latest validation: 53 hosted tests and 21 isolated backend tests passed. Both public HTTPS routes reject unsigned requests (403) and authenticate/reject invalid signed bodies (400). Firebase validation-only accepted a sample new_message request for a test emulator without delivering it. Code rollback: /opt/axonic-neuron-before-message-push-20261002. Proxy rollback: /etc/nginx/conf.d/neuron-https.conf.before-message-push-20261002. Identity preserved and health confirmed after deployment. Actual background notification delivery remains to be tested after the backend route is enabled.
