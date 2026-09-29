# MAX boundary (first implementation)

Checked against current official documentation on **2026-09-29**. The official developer documentation is hosted at `dev.max.ru`. Official content was retrieved through web search; direct page fetch did not work in this execution environment. No MAX credentials were provided. These are local fixture and PostgreSQL checks, not a live MAX or mobile/web validation.

| Operation | Official source | Adapter behavior |
| --- | --- | --- |
| Webhook | [POST subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions) | Constant-time `X-Max-Bot-Api-Secret` check; JSON size bound; no ACK before DB commit |
| Updates | [Update](https://dev.max.ru/docs-api/objects/Update), [bot setup](https://dev.max.ru/docs/chatbots/bots-coding/prepare) | Millisecond timestamps; start, private text, private callbacks; other events minimized to ignored |
| Send | [POST messages](https://dev.max.ru/docs-api/methods/POST/messages) | Raw access token in Authorization header; private user ID resolved from encrypted identity; receipt from returned message body `mid` |
| Edit | [PUT messages](https://dev.max.ru/docs-api/methods/PUT/messages) | Known owner-bound persisted message receipt required; `success: true` checked |
| Callback answer | [POST answers](https://dev.max.ru/docs-api/methods/POST/answers) | Verified callback ID and bound answer; checks `success: true`; CallbackReceipt has no invented message ID |
| Upload | [POST uploads](https://dev.max.ru/docs-api/methods/POST/uploads), [media flow](https://dev.max.ru/docs-api/use-cases/sending-messages/media) | File type; multipart field `data`; signed HTTPS upload URL; token stored through encrypted repository writer |
| Subscription inspection | [GET subscriptions](https://dev.max.ru/docs-api/methods/GET/subscriptions) | Verifies configured URL and required update types; does not claim remote secret verification |

The current documented base URL is `https://platform-api2.max.ru`. Bot authentication is `Authorization: <access_token>`; the token is not a query parameter or a Bearer token. HTTPS webhook port 443 and a trusted certificate are deployment requirements. The documented webhook response window is 30 seconds. Receiving an HTTP 200 acknowledges durable acceptance of the event; it does not mean the scenario or delivery succeeded.

## Ingress

Only the trusted platform envelope resolves identity. User text, start payload and callback handle cannot supply `owner_id` or an outgoing chat ID. Private message sender/callback user IDs are HMAC-indexed with the bot scope. Raw MAX names, usernames, contacts, attachments, and forwarded-message metadata are discarded. Platform recipient IDs are encrypted as part of IdentityRecord, and lookup/dedupe indexes contain HMAC values. The event payload keeps only bounded text or opaque callback handle plus verified callback ID. Start payload is limited to the server's `start`, `help`, and `resume` allowlist.

Group/channel and unsupported updates are persisted as ignored events with empty payload and no delivery identity. They cannot change a user's case and do not trigger a case-content response. A group guidance reply is not implemented in this first version. Malformed supported update schemas are rejected. There is no raw update storage.

The webhook transaction upserts identity, inserts inbox with scoped uniqueness, and enqueues exactly one process_inbox job. A duplicate returns 200 after its transaction commits. Any storage/commit failure returns 503. PostgreSQL work runs in a thread pool so the HTTP event loop does not execute blocking DB I/O.

## Transport and recovery

The composition root supplies an explicit httpx.Client and trusted owner-bound recipient, attachment and known-message resolvers. The application durably enters `sending` and saves SendPermit before any send operation. View/callback/material payload hashes must match the saved typed payload. Expired permits or foreign targets are rejected before network I/O. Attachment tokens are decrypted inside this boundary; UploadResult exposes a UUID reference only. Uploaded does not mean processed or delivered. Sending an historical file also requires an acknowledged warning and shows a short historical warning in the message.

`attachment.not.ready` is a definite, safe-to-retry rejection using the existing upload reference and existing rendered artifact. HTTP 429 is a definite retryable rejection. Other explicit 4xx rejections are permanent unless otherwise documented; explicit platform `success: false` is a rejection. Timeout, HTTP 408, ambiguous 5xx, redirects, malformed successful responses or a missing receipt produce `unknown`, with no automatic safe resend. A recovery worker must preserve `delivery_unknown`; only explicit user retry may create a new send attempt. Upload retries can leave remote orphan tokens but do not send duplicate messages or re-render artifacts.

The transport enforces an in-process gate of 30 API requests/second and at most one send/edit/callback operation per recipient every 0.5 seconds. It reports a retryable local rejection rather than sleeping. This first deployment uses one worker. Multiple processes need a shared bot-wide limiter before increasing worker count. Network timeouts are explicit, redirects disabled, response sizes bounded. Russian copy and user values are sent as plain text with no `format` field, preserving the 4000-character bound without HTML entity expansion; one opaque callback action per row remains readable on mobile. Signed uploads are allowlisted to documented MAX upload hosts; bot Authorization and Cookie headers are removed. Attachment filenames are the artifact UUID plus `.pdf` or `.docx`.

No user-facing download, administration or identity HTTP API exists. The CLI's deterministic transport is configured outside this HTTP module. Render processes do not receive token, settings or database connections.

## Trusted TLS CA bundle

The official API pages linked above advise adding the Ministry of Digital Development certificate to the trusted certificate list for `platform-api2.max.ru`. The live transport constructs `ssl.create_default_context()` and passes that verified SSLContext to httpx with `trust_env=False`. Certificate chain and hostname verification are always enabled; there is no `verify=False` path.

When the deployment needs an approved additional trust chain, set `TSR_MAX_CA_BUNDLE` to a readable PEM CA bundle path (Settings.max_ca_bundle). The bundle is supplied by the operator, mounted read-only, and loaded through `ssl.create_default_context(cafile=...)`; no certificate is downloaded automatically. A custom bundle defines the trust anchors, so include the approved roots required by both the MAX API and signed upload hosts. Leaving the setting empty uses the system default trust store. A missing or invalid file fails startup instead of weakening TLS verification. Local tests exercise default verification and an explicitly supplied synthetic CA; live connectivity remains pending credentials/deployment.

For the optional Compose deployment, set `TSR_MAX_CA_BUNDLE_HOST` to the operator-provided complete PEM bundle (ordinary approved roots plus the trusted Ministry root), then use `docker compose -f compose.yaml -f compose.max-ca.yaml up -d --build`. The override mounts the bundle read-only at `/run/max-ca/ca-bundle.pem` in the app and worker and sets `TSR_MAX_CA_BUNDLE` to that container path. The default Compose deployment uses the default system trust store and does not mount a custom certificate bundle.
