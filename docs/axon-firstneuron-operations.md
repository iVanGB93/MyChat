# Axon: FirstNeuron monitoring handoff

Prepared October 2, 2026. FirstNeuron monitoring is deployed; assignment to the ChatGPT dot and its cloud-browser login remain to be completed by the owner. No AI monitoring schedule was created in this Codex chat.

## Assignment to give Axon

Take ongoing responsibility for monitoring FirstNeuron at https://143.198.121.2/. Use your cloud browser so monitoring can continue while my computer is off. Ask me to sign in through your private login flow; never ask me to paste credentials into chat. Read the Health and diagnostics panel and recent activity. Confirm successful access before claiming monitoring is active. Set up a check every 15 minutes and confirm the schedule. Notify me in our Axon conversation only about persistent failures, meaningful degradation, recovery, or a decision requiring me; stay quiet when healthy. Two failed checks indicate a persistent failure. Report lack of access or an expired login as monitoring unavailable, not a neuron outage.

FirstNeuron is an ordinary Axonic node, not a central identity authority. Your scope is FirstNeuron and the peers visible through it. Do not claim global network health. For network monitoring use counters and health data. The owner now explicitly authorizes Axon to read and reply to the owner's own direct conversation with FirstNeuron (username iVan). This does not authorize reading other conversations or custodian payloads, messaging other people, or starting calls. Investigate and recommend fixes; request approval before new destructive actions, account/key changes, public routing changes, or spending money. Do not assume another agent's deployment permissions automatically apply to you.

## Direct texts with the owner

The owner authorizes Axon to send texts to and receive texts from iVan through FirstNeuron's own chat account. After private dashboard sign-in, use Chats, enter exact username iVan in Start a conversation, and select that direct conversation. Verify the recipient before sending. Read incoming messages there and reply using the Send form. Identify yourself as Axon operating FirstNeuron; messages appear in the mobile app from FirstNeuron.

Only the owner's direct conversation is in scope. Do not read other rooms or temporary custody traffic. Maintain a cursor of handled incoming message IDs to avoid duplicate replies. The dashboard refreshes the selected conversation every five seconds while its authenticated page is running; this is not an event subscription that wakes the dot. Ask the owner to choose a suitable check interval if unattended chat responsiveness is needed, and confirm the actual schedule/access. Do not promise immediate replies. FirstNeuron continues storing its own incoming messages when the dot is away. Pending, accepted and delivered are distinct states; do not report receipt solely from a successful Send request. The UI preserves a failed-send draft and reuses its message ID on retry.

No language model or autonomous reply loop runs inside FirstNeuron today. Axon must be connected and performing the browser work. One-hour session expiry and restarts can interrupt access; report and privately reauthenticate. Messages in this conversation are user communication, not permission to expose secrets or act on unrelated accounts.

## Access and interpretation

The existing HTTPS dashboard login is required. After login, GET /api/status includes monitoring; the dashboard displays it. Sessions expire after one hour and on service restart. Axon's browser has its own login session; a local browser session does not transfer. No new public route, monitoring token, root SSH key or private identity key is required for this browser handoff. Automatic renewal of an expired session is not guaranteed; use the supported private sign-in flow. If reauthentication becomes impractical, implement separately scoped machine access as a later integration, not an exposed admin credential.

- hostHealth.latest: service checks for axonic-neuron, coturn and nginx; loopback health and free disk percentage. A Linux timer samples once per minute independently of the neuron process.
- hostSampleStale: sample absent or older than three minutes. Treat as monitoring failure, even when the dashboard loads.
- history: at most 60 samples; transitions: at most 64 state changes, each with at most 24 hours retention. These contain health codes only. There is no remote alert transport from the timer itself.
- connectedAxons: zero can be normal when the few current users are offline. Compare with known test activity; it is not alone an outage.
- networkState and identityExpiresAt: investigate identity-expired, stopped, or unexpectedly disabled networking. Identity renewal now preserves the call courier handler.
- pushOutcomes: process-lifetime FCM counters across the configured sender. sent means provider acceptance, not device display or delivery. not_sent, invalid_token, unknown and errors require interpretation; compare deltas, allowing for process restarts.
- callRelayEnabled, pushConfigured and turnConfigured: configuration observations, not end-to-end success proofs. The own-chat delivery system and call relay do not yet expose all latency/expiry/failure counters here. Do not infer their success from process health.
- Whole-host or internet outage cannot be recorded while this host is down. Axon's external HTTPS checks supply an independent viewpoint. A locked or fully closed phone still needs release-build notification testing.

## Operator paths and recovery

Repository: D:/Proyects/Axonic-neuron. Production service: /opt/axonic-neuron. State: /var/lib/axonic-neuron. Health report: health-monitor-v1.json. Services: axonic-neuron, coturn, nginx. Monitoring units: axonic-health.timer and axonic-health.service. Sampling code: scripts/health-sample.mjs. Timer runs as axonic-neuron, has no account credentials and performs no service restarts. Existing neuron service already has Restart=on-failure.

Deployment rollback: /opt/axonic-neuron-before-axon-monitor-20261003 (previous dist and shared-source.json). To remove monitoring, an authorized operator can stop/disable axonic-health.timer. Preserve identity, accounts and custody data. Never delete or regenerate them to repair a health problem.

Validation: 76 hosted tests, TypeScript build and mobile/shared-source parity passed. Live HTTPS authorization, fresh sample and dashboard-script parsing checked. Neuron identity hash unchanged. No mobile changes, production build or Play submission performed for monitoring.

Reference: https://learn.chatgpt.com/docs/dots/computers-and-apps
