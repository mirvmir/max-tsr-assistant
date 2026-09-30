# Technical HTTP API

[OpenAPI 3.1 JSON](openapi.json) and [DATA-API.yaml](DATA-API.yaml) describe the implemented technical endpoints. Both files are generated from `tsr.http.create_app(...).openapi()`; there is no public OpenAPI/docs route.

| Method and path | Authorization | Meaning |
| --- | --- | --- |
| POST `/webhooks/max` | `X-Max-Bot-Api-Secret` | Receive a bounded MAX update, minimize it and atomically commit inbox + job before 200 |
| GET `/health/live` | None | Process responds |
| GET `/health/ready` | `X-Readiness-Secret` | DB, fresh scoped worker/version, exact active release and dependency lifecycle, critical secrets, HTTPS MAX subscription, and restore barrier are ready |

A webhook 200 contains `{"status":"accepted"}` for new and duplicate updates alike. It does not acknowledge scenario completion or delivery. Invalid secret returns 401; invalid schema 400; excessive body 413; unsupported content type/encoding 415; missing webhook configuration or durable storage failure 503. Body limits come from `max_body_bytes`; user text comes from `max_input_chars`. Only uncompressed JSON is accepted.

Live success is `{"status":"live"}`. Ready success is `{"status":"ready"}`. Ready is fail-closed when its credential is not configured (503 with `detail=not_ready`); missing/incorrect credentials return 401. With a valid credential, an unavailable readiness dependency returns 503 and `{"status":"degraded"}`. Other error bodies contain a safe `detail` code. No settings, queue content, secrets, dependency failure details, user commands, downloads, case data or administrative operations are exposed over HTTP.

The local demo can run with no MAX token; it then remains degraded for live-bot readiness. Operator-only `tsr health` exposes bounded scoped queue ages/counts and aggregate operational metrics; it is not a new HTTP endpoint. See [RUNBOOK](../RUNBOOK.md) for subscription inspection, retention, backup, deletion-journal cutover and offline restore.
