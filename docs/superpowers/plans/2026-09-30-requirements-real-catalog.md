# Requirements and real catalog implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. The user already selected parallel development and requested corrective implementation against the approved requirements.

**Goal:** Verify the entire approved backlog, correct missing software, and run 10–15 real Ortonica/Medicamarket offers through selection, certificate arithmetic and documents without losing existing cases or fixtures.

**Architecture:** Preserve the modular Python monolith, PostgreSQL inbox/jobs/outbox and one spawned renderer. Add versioned public factual data, generic release loading, guarded catalog navigation and the operations specified in the existing plan. Keep expert, privacy, live MAX, hosting and user-research approvals separate from executable software checks.

**Tech Stack:** Existing Python 3.12+ / image 3.13, Pydantic, psycopg/PostgreSQL 16, FastAPI, httpx, AES-GCM, ReportLab and python-docx; no new broker or user HTTP surface.

**Spec:** `docs/specs/IMPLEMENTATION_PLAN_v1.md`, `docs/specs/CONTRACTS_v1.md`, `docs/specs/MVP_DATA_TASKS_v1.md`; baseline audit `docs/verification/requirements-audit-2026-09-30.md`.

## Global constraints

- Public HTTP remains POST `/webhooks/max`, GET `/health/live`, protected GET `/health/ready` only.
- Domain is pure: explicit IDs/time, no ORM/settings/network imports. Application owns semantic user transactions.
- Typed immutable contracts, scoped owner/revision/epoch/TTL/fence checks; preview and manifest content hashes agree.
- Money is integer kopeks; unknown differs from zero. An advertised "from" amount is not a precise SKU total.
- Certificate acceptance and fund contract are independent. No inferred approval or shipping cost.
- Demo flag permits synthetic draft only. Unreviewed public snapshots remain blocked. Public factual review records actual source verification; it does not substitute for medical expert review.
- New real catalog contains 10–15 offers from both requested suppliers. Existing synthetic 1.0 fixtures and their hashes remain intact.
- Case/manifest retention90days after activity, files7days, sensitive processed inbox24hours, minimal dedupe30days, backup7days.
- Long file/network IO occurs outside user SQL transactions; sending is durable before the external call. Ambiguous sends never auto-repeat.
- No merge/publish/live bot calls are implied by this change; update the existing draft PR with reviewable code and evidence.

## Review focus

- Variant selector and embedded price refer to the same exact offer; conflicting title/table/JSON facts are preserved.
- Every catalog page is reachable and stale page handles cannot select an old or foreign result.
- Switching active release preserves old input/materials, blocks stale confirmations and permits an explicit new case without deletion.
- Retention and orphan removal leave live claims intact and keep metadata pending if blob deletion fails.
- Restore authenticates the archive, uses an empty offline target and replays an independently supplied current deletion journal before readiness.

## Baseline

Local source `e720aa3`, equivalent GitHub PR#1 head `ceb6bc5`. Baseline full suite:91passed, one upstream warning (18.63s), real PostgreSQL and spawn. No Docker or live MAX result is inferred from this.

## Task1: Public factual data — domain_data

**Files:** New catalog/sources/category/demo/golden/release version1.1 under `data/`, factual evidence under `data/evidence/`, `docs/data/REAL_CATALOG.md`, `tests/test_real_catalog.py`.

**Interfaces:** Consume existing CatalogPack/SourcesRegistry/CategoryProfile/ReleasePackManifest. Produce a mixed demo release with genuinely verified public offers and explicit synthetic rule/user/route/template components.

- [ ] Collect12primary product pages (6Ortonica+6Medicamarket); record live selected offer, seller SKU, config, price kind, units, date, URL, locator and evidence hash.
- [ ] Validate that page ambiguities stay conflicting/unknown and all positive facts have source evidence. Public reviews remain draft until factual cross-check.
- [ ] Build the synthetic technical rule profile `manual-wheelchair-technical`1.1.0, using existing seat_width mm / max_user_mass kg / foldable fields; do not invent approved medical rules.
- [ ] Independently verify published facts, then record actual technical source reviewer/time/due date for public snapshots. Preserve unverified certificate/fund/delivery as unknown.
- [ ] Define first scenario:405mm,100kg,foldableyes, certificate10,000RUB; Ortonica Base200 exact14,500RUB yields conditional gap4,500RUB with unknown certificate acceptance and delivery. Validate against source before freezing expectations.
- [ ] Run schema/ref/hash/fact golden checks; signal ready to generic loader and root verifier.

## Task2: Generic release and strict lifecycle — contracts_cases

**Files:** contracts/config, bootstrap, operations/releases; tests contracts/bootstrap/releases.

**Interfaces:** `load_release(root,manifest)`→DemoRelease (legacy type retained), `load_demo_release(root)` wrapper; optional Settings.release_manifest; typed ReleaseRecord.packages; per-package readiness helpers. Navigation supports catalog/cases/new_case with bounded page/resource validation.

- [ ] Reproduce hardcoded demo loader/pointer mismatch and mixed public-draft rejection in targeted tests.
- [ ] Load the immutable active DB manifest/typed package content; auto-stage configured initial release only when appropriate. Never silently replace an existing active pointer.
- [ ] Register exact package/snapshot lifecycle dependencies and enforce current revocation/expiry at activation and use. Rollback has the same validation as activation.
- [ ] Permit reviewed public data plus explicitly synthetic drafts in demo; public draft and expired/revoked facts stay blocked. Pilot remains gated by all reviewed runtime dependencies and privacy.
- [ ] Preserve existing manifest hash verification when adding optional provenance; no silent rehash of historical files.
- [ ] Test source version switch, revocation of one dependency, invalid rollback and legacy startup.

## Task3: Persistent health and retention — persistence

**Files:** DB adapter, maintenance migration, operations/maintenance, tests maintenance/DB.

**Interfaces:** `record_worker_heartbeat(worker_id,now,release_commit,bot_scope,capacity)`, `collect_health(now,scope,release_ref)`→HealthSnapshot; `run_retention(now,policy,db,files,bot_scope)`→DeleteReport. DB public ports supply active claims, referenced blobs and deletion journal; lifecycle methods retain explicit UoW ownership.

- [ ] Write real PG regressions for fresh/stale heartbeat and queue age, TTL boundaries, foreign scope and deletion IO failure.
- [ ] Store privacy-safe operational timestamps and heartbeat/capacity. Add compatible migration/backfill for existing encrypted cases.
- [ ] Retire inactive cases through tombstone/epoch; remove expired private artifacts and sensitive DTO payloads while retaining required dedupe/FKs.
- [ ] Schedule safe orphan cleanup with active staged claim protection; file IO outside SQL and failure leaves retryable metadata.
- [ ] Expose exact per-package lifecycle and independent deletion journal exports/replay for backup.
- [ ] Verify idempotence, lease/fence isolation and absence of sensitive plaintext in new operational columns.

## Task4: UI for the complete catalog — application_flow

**Files:** application/conversation/resources, tests application.

**Interfaces:** Existing execute_command/action/text and frozen CompareRecords; guarded `NavigatePayload` catalog pages3offers and owner case pages5cases.

- [ ] Reproduce inaccessible offers4+ and synthetic provenance text in meaningful UI tests.
- [ ] Add catalog paging without changing semantic revision; reuse bound ComparisonRecords and validate each selection/current guard.
- [ ] Show real snapshot sources/date, precise model/config/supplier, lower-bound prices and unknown conditions. Preserve demo user/model warnings without falsely labelling public prices synthetic.
- [ ] Support explicit new case/role choice and owner-bound case list; preserve legacy input/history on profile mismatch, no automatic conversion/deletion.
- [ ] Apply exact dependency readiness at preview/confirmation/render/publication/material authorization; permit pilot only under validated reviewed/privacy policy.
- [ ] Test offer12selection, stale/foreign pages, source review expiry, old case/new case and frozen hash equality.

## Task5: File lifecycle and encrypted backup/restore — documents_files

**Files:** operations/backup, public files adapter, document warnings, Dockerfile PG client dependency; tests backup/files/documents.

**Interfaces:** Existing BackupReceipt/RestoreReport; create_backup and restore_backup plus encrypted VerifiedDeletionJournal export/load. Restore requires empty offline target and explicit source quiescence/cutover attestation.

- [ ] Reproduce absence of roundtrip backup/replay; test authentication/path safety and missing/stale journal rejection before SQL.
- [ ] Create bounded encrypted archive of consistent database and ciphertext blobs; no keys in the archive or sensitive plaintext on disk; enforce7day retention.
- [ ] Restore to an empty offline target, verify file metadata, replay independent current deletion epochs and only then mark ready.
- [ ] Record operator requirement: stop source writers, export current journal, supply cutoff, restore, verify, start. Do not claim automatic external-journal completeness or dual-write safety.
- [ ] Extend orphan API with active claim protection and preserve existing remove/read APIs.
- [ ] Ensure documents distinguish real published facts from synthetic user/technical model/route/template; visual QA on first real scenario.

## Task6: Runtime/operator wiring — max_http

**Files:** HTTP/MAX/worker/CLI/demo; tests MAX/worker/CLI; optional load-check tool.

**Interfaces:** Call Task2 release operations, Task3 health/maintenance, Task5 backup; LocalTransport/application facade remain the only demo pipeline.

- [ ] Wire worker heartbeat and periodic maintenance; keep render/send work independent and claims bounded.
- [ ] Readiness verifies recent worker, exact active release and dependencies, critical configuration and configured MAX subscription; restore pending must not be ready.
- [ ] Safe group guidance uses only verified actor/private target, without group case content or guessed IDs.
- [ ] Add CLI import/activate/rollback/revoke/health/retention/backup/journal/restore and fixture-driven `demo --dataset public|synthetic`; no duplicated workflow.
- [ ] Coordinate shared outbound quota if multiple workers are supported; preserve single-worker limit otherwise label W01 partial honestly.
- [ ] Run offline steady/burst workload with50actors/1000synthetic snapshots and100documents when available; report measured results rather than infer capacity.

## Task7: Integration and independent verification — root + independent_review

**Files:** requirements audit, first scenario/runbook/status, verification report; existing PR#1.

- [ ] Check every original implementation/D/F ID against actual evidence; external prerequisites remain explicit.
- [ ] Cross-check real product facts and uncertainties against primary source evidence before activating public data.
- [ ] Run old and public catalog scenarios through actual installed CLI, real PostgreSQL/spawn/file authorization; inspect resulting PDF/DOCX.
- [ ] Run full suite once implementation stabilizes, then only justified corrective regressions and one scoped final re-review.
- [ ] Preserve code/secret privacy, compare delivered GitHub tree with the checked checkout, update draft PR and link instructions/assessment.

## Execution boundaries

Agents share one existing feature checkout and own the files listed above. No independent stage/commit/push and no worker-created reviewers. Root integrates. Supplied user architecture and implementation plan authorize corrective work; missing expert approval or live credentials never become an invented pass.
