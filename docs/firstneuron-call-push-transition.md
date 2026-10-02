# FirstNeuron call-push transition

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
