# Technical HTTP API

[OpenAPI 3.1 JSON](openapi.json) and [DATA-API.yaml](DATA-API.yaml) describe the implemented technical endpoints. Both files are generated from `tsr.http.create_app(...).openapi()`; there is no public OpenAPI/docs route.

| Method and path | Authorization | Meaning |
| --- | --- | --- |
| POST `/webhooks/max` | `X-Max-Bot-Api-Secret` | Receive a bounded MAX update, minimize it and atomically commit inbox + job before 200 |
| GET `/health/live` | None | Process responds |
| GET `/health/ready` | `X-Readiness-Secret` | DB connectivity is available; no settings, queue content, secrets or internal error details are returned |

A webhook 200 contains `{"status":"accepted"}` for new and duplicate updates alike. It does not acknowledge scenario completion or delivery. Invalid secret returns 401; invalid schema 400; excessive body 413; unsupported content type/encoding 415; missing webhook configuration or durable storage failure 503. Body limits come from `max_body_bytes`; user text comes from `max_input_chars`. Only uncompressed JSON is accepted.

Live success is `{"status":"live"}`. Ready success is `{"status":"ready"}`. Ready is fail-closed when its credential is not configured (503), missing/incorrect credentials return 401, and unavailable DB returns 503. Error bodies contain a safe `detail` code. No user commands, downloads, case data or administrative operations are exposed over HTTP.
