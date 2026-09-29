# Первая реализация: распределение и точки интеграции

План и контракты пользователя находятся в `docs/specs/`. Пользователь уже утвердил архитектуру и поручил параллельную реализацию. Повторное утверждение архитектуры не требуется. ARCHITECTURE_v1.md отдельно не приложен; приоритет имеют CONTRACTS_v1.md и IMPLEMENTATION_PLAN_v1.md. Реальные D01–D06/D10–D13 отсутствуют: fixtures только synthetic draft, pilot fail-closed. Это первая реализация, не завершение всех эксплуатационных и предметных условий MVP.

Одна Python кодовая база, PostgreSQL, HTTP app и worker. Production image Python 3.13. Локальная среда Python 3.12; spawn задаётся явно в обеих версиях. Минимальные содержательные тесты, без тестов на каждую строку. Domain не импортирует ORM/application/adapters. UI — диалог MAX; отладочный CLI использует тот же application dispatcher и синтетический owner. Mini-app и отдельный пользовательский HTTP API не создаются.

## Task 1: Contracts/config/cases

Owned: `src/tsr/contracts/`, `src/tsr/config.py`, `src/tsr/domain/cases/`, `tests/test_contracts.py`, `tests/test_cases.py`.
Create immutable Pydantic DTOs from all contract sections, Result[T]/DomainError, canonical hash, candidate projections; public imports from `tsr.contracts`. Implement C01–03 and CASE01–05 pure behavior. Extra infrastructure record DTOs for inbox/identity/callback/job/outbox belong to contracts, never arbitrary cross-module dicts. Tell other agents exact export names early. Settings supplies env/secret file loading and demo-only startup without MAX token; pilot cannot start without reviewed data/privacy readiness.

## Task 2: Domain/data

Owned: `src/tsr/domain/catalog/`, `matching/`, `pricing/`, `routes/`, `src/tsr/application/catalog_service.py`, `src/tsr/operations/releases.py`, `data/`, `schemas/`, `tests/test_domain.py`, `tests/test_releases.py`.
Implement catalog/matching/pricing/routes and safe demo release importer. Public functions/signatures as spec. A `DemoRelease` named immutable DTO and `load_demo_release(root: Path) -> DemoRelease` expose profile, offers, route, sources, template refs and release ref to composition. Only explicitly synthetic data; at least 5 SKU/2 fictitious suppliers, unknown/from/mismatch fixtures. Independent monetary/route golden expectations. Activation never turns draft into reviewed.

## Task 3: Persistence/ports

Owned: `src/tsr/adapters/db/`, `src/tsr/ports/`, `migrations/`, `tests/test_db.py`.
PostgreSQL psycopg, explicit UoW, no SQLite production fallback. `Database(dsn: str, crypto: CryptoPort)` exposes `migrate()`, `uow()` context manager; exiting without commit rolls back. One central generic encrypted typed repository contract: `get(id)`, `get_owned(ctx,id,lock=False) -> Result[T]`, `insert(dto)`, `save(dto)`, `list_by_case(case_id)`, `list_by_owner(owner_id)`, plus specialized guarded methods. UoW repository names: cases, inputs, candidates, previews, confirmations, manifests, bundles, artifacts, handles, inbox, identities, outbox, work. DTO identities/owner/case/revision remain indexable; all sensitive payload encrypted. Supply JobRecord/InboxRecord/HandleRecord/IdentityRecord/OutboxRecord contracts/export names via coordination, do not duplicate Task 1 models. WorkRepository enqueue_unique(WorkRef), claim_next(kind,worker_id,now,lease_seconds), renew_lease(claim,now,lease_seconds), finish_if_claim(claim,now,status='succeeded'), recover_expired(now). Durable sending/unknown recovery, exact fence CAS; unique scoped dedupe. Minimal real PostgreSQL tests. Coordinate repository-specific signatures with app agent immediately.

## Task 4: Application/conversation

Owned: `src/tsr/application/` except catalog_service.py, `src/tsr/domain/conversation/`, `resources/`, `tests/test_application.py`.
Implement user flow and application orchestration including both branches, saved confirmed inputs, stale handles, frozen preview/manifest/hash, exactly one bundle per confirmation, request material, historical warning, tombstone. `Application(db, release, settings, clock=None, ids=None)` is composition facade; `execute_command(CommandEnvelope)->Result[CommandResult]`; `handle_inbox(ClaimedJob)->Result[CommandResult]`; expose `prepare_result_preview(ctx,guard,now)`; `publish_render_result(claim,staged)`; `request_material` and `authorize_delivery`. Same facade powers trusted CLI simulator and MAX worker. Only application owns semantic commits. Conversation returns ViewDraft; binds opaque DB handles to ViewModel. Russian mobile-first short text, next/back/edit/unknown/help/resume/materials/delete confirmation, no accuracy percentages or approval promises. First scenario uses profile fields and synthetic values, with explicit input confirmation. Coordinate exact signatures with Tasks 1/3/5/6 immediately.

## Task 5: Documents/crypto/files

Owned: `src/tsr/domain/documents/`, `src/tsr/adapters/render/`, `src/tsr/adapters/crypto/`, `src/tsr/adapters/files/`, `templates/`, `assets/`, `tests/test_documents.py`, `tests/test_files.py`.
Implement DocumentModel from frozen manifest; purchase one PDF; support DOCX+3 PDFs. ReportLab and python-docx share plain content; Cyrillic, long fields, visible missing values, demo watermark and model route warning. Renderer exported `render_document(RenderRequest)->Result[RenderedBytes]` isolated no settings/DB/keys imports, bounded bytes. `Crypto(key: bytes, key_id='local-v1')` AES-GCM encrypt/decrypt; `PrivateFiles(root: Path, crypto)` stages encrypted blobs before publication. Public `stage_encrypted(rendered,claim,manifest_ref)->Result[StagedArtifact]`, `read_authorized_artifact(permit,record)->Result[bytes]`, remove_blob/purge_orphans. Copy licensed DejaVu font and license to assets. Render child uses explicit spawn (root handles worker). Validate generated PDFs/DOCX visually. Templates are newly created demo templates; no invented official form.

## Task 6: MAX/HTTP

Owned: `src/tsr/adapters/max/`, `src/tsr/http.py`, `tests/test_max.py`, `tests/fixtures/max/`, `docs/integrations/`, `docs/api/`.
Read current official MAX API docs with web tools and record check date/links. Implement webhook verification/size validation/identity HMAC/minimized NormalizedEvent and atomic inbox+job commit; no ACK before DB commit. Only POST /webhooks/max, GET /health/live and protected GET /health/ready. FastAPI app factory `create_app(settings, db, application=None)`; no public downloads/admin API. Async IO or thread offload sync PG. Transport `MaxTransport` accepts typed permits, encrypted identity resolver callback and authorized attachment refs; headers, inline keyboard handles, callback answers, file upload, timeout→unknown, definite rejection→retryable classification. Russian ViewModel presenter (resolve resources message keys). CLI simulator can dump these same messages; MAX identity cannot be supplied by user payload. Demo-only without token is supported with deterministic local transport outside HTTP. Coordinate worker signatures early.

## Controller integration

Owned by root: `src/tsr/worker/`, `src/tsr/cli.py`, composition/bootstrap, deployment, README/runbook/first-scenario, integration smoke tests and independent reviewer coordination. Agents do not stage/commit/push shared changes; root commits integrated changes. Never edit another task's owned files without controller routing. Report implemented behavior, test commands/results, blockers and export signatures in a small file under `.superpowers/reports/` and send completion message. Task review is by independent root/reviewer, no worker-spawned reviewers.

## Review focus

Stale candidates/previews; negative certificate conditions and unknown delivery; unknown route and synthetic marks; foreign owner access and tombstone; stale fence and ambiguous sending; required artifact readiness and no plaintext at rest. Minimum verification covers both branches and these safety invariants. Real MAX/mobile/web, load, deployment and pilot are pending absent credentials/verified data; no success claims for them.
