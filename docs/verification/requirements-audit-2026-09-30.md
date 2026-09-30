# Повторный аудит всех требований — 30 сентября 2026

Проверенный исходный HEAD: `e720aa3331356e7e4e7d6b6d97ab74f233e927ca`. Это аудит состояния **до нового расширения**, а не итоговое ревью будущих изменений. Основания: все задачи [IMPLEMENTATION_PLAN_v1.md](../specs/IMPLEMENTATION_PLAN_v1.md), [CONTRACTS_v1.md](../specs/CONTRACTS_v1.md) и [MVP_DATA_TASKS_v1.md](../specs/MVP_DATA_TASKS_v1.md). Отдельный ARCHITECTURE_v1.md не предоставлен; недостающие требования не домысливались.

**Весь backlog не выполнен.** Предыдущий положительный вердикт относился только к синтетической первой реализации. Код уже поддерживает два локальных сценария с сохранением состояния, расчётом и файлами, но обычный UX не открывает весь каталог, runtime привязан к demo release, а существенная часть эксплуатации отсутствует. Эти недостатки нельзя списывать на отсутствие токена MAX или экспертных данных.

Статусы: **done** — критерий реализован и имеет локальное доказательство; **partial** — выполнена часть, ниже назван остаток; **missing** — требуемый работающий механизм отсутствует; **external gate** — приёмка требует внешнего материала, доступа или участников. Done не означает медицинское, юридическое или production approval. Ссылки E01–E17 ниже указывают конкретные исходники и тесты; тесты прочитаны, новый полный прогон в этом read-only аудите не проводился. Последний независимый полный прогон предыдущего ревью: 91 passed на `49e878c`, PostgreSQL 16/Python 3.12; это не новый результат на текущем HEAD.

## Главные программные пробелы, которые можно закрывать сейчас

1. **Общий загрузчик активного release — DB02/CAT02/OPS01.** `bootstrap.py:52` всегда загружает `data/releases/demo-1.0.0/manifest.json`; активная запись БД только проверяется. После активации другого release приложение продолжит держать старый объект, а freshness начнёт отказывать. Импорт хранит manifest/review metadata, не нормализованное содержимое пакетов. Нужны точная загрузка active content, проверка assets и тест restart после переключения.
2. **Доступ ко всем предложениям — CAT04/APP03.** `application/service.py:215` берёт `self.release.offers[:3]`. `DemoCatalog.list_candidates` имеет cursor, но не используется основным диалогом. Ограничение сравнения 1–3 предложениями правильно по CONTRACTS; неверно отсутствие перехода к остальным. Ограничения ровно на пять SKU в DTO нет: пять — размер текущего fixture. Нужны страницы/подборка и проверка выбора десятого–пятнадцатого предложения, стабильных cursors, сохранения unknown.
3. **Lifecycle каждого пакета — DB02/CAT03/ROUTE03/W08.** `ReleaseRepository.insert` создаёт lifecycle только release; `revoke_package` фактически отзывает release ref, а `route_lifecycle` создаётся in-memory active. Нет независимого отзыва profile/catalog/snapshot/route/template и проверки всех зафиксированных refs на confirm/render/publish/current send.
4. **Операционный CLI — OPS01.** Есть validate/import/activate/revoke Python functions, но CLI имеет лишь `validate-data`, migrate/demo/cleanup/serve/worker. Нужны import/activate/rollback/revoke с actor/reason, атомарностью и проверками expiry/review. Семантическая матрица ещё неполная: например GoldenCase expected проверяется на непустоту, не на обязательный состав для каждого scenario; registry algorithms/template связи требуют отдельного отрицательного набора.
5. **Retention — OPS03.** Settings содержит только TTL чтения файла семь дней, candidate/preview/handle TTL. Нет исполняемой политики case/manifest 90 дней, processed sensitive inbox 24 часа, dedupe 30 дней и физической expiry файлов. Существующий cleanup обслуживает только ручные tombstones.
6. **Безопасный orphan janitor — FILE03/OPS03.** Storage умеет `purge_orphans`, но application/worker/CLI не вызывают его с согласованным набором DB references и active claims. Нельзя просто удалять всё по возрасту: нужен контроль текущего fence/lease и гонки stage→publish.
7. **Реальная диагностика — OPS02/W09.** Worker вызывает отсутствующий `record_worker_heartbeat` через optional getattr; `/health/ready` проверяет только `db.ping`. Нет persistent heartbeat, oldest queue age/counts, release health и безопасных operational alerts. `inspect_subscription` есть, но не подключён к эксплуатационному пути.
8. **Backup/restore — OPS04/QA04.** Есть DTO и runbook-ограничение; рабочего согласованного backup/restore с отдельной защитой ключей, tombstone replay и закрытым доступом до сверки нет. Код и локальный restore drill можно сделать без live MAX; реальное размещение и отдельное хранилище — отдельный gate.
9. **MAX groups/subscription — MAX01/OPS02.** Группы безопасно очищаются и игнорируются (`ingress.py:91–92`), но требуемый безопасный ответ «перейдите в личный диалог» отсутствует. Не нужно сохранять групповой пользовательский текст или выполнять кейс в группе. Подписка проверяется методом transport, но нет команды/мониторинга проверки и регистрации; live успешность требует доступа.
10. **Измерения и ограничение IO — W01/W09/QA05.** Shared rate limiter только внутри одного transport process, DB/история не ограничены страницей, SQL не имеет общей wall-clock границы. Есть hard render timeout и bounded close render child, но доставка/SQL может пережить graceful deadline. Локальную steady/burst нагрузку, 1000 snapshots и 100 документов можно измерить сейчас; без неё число workers и p95 не доказаны.

Дополнительно: `Application._execute` безусловно отвергает `start_case` в pilot (`service.py:241–242`), независимо от готовности данных. Это программный запрет первой версии, а не проверка достаточности reviewed пакетов. Снимать его до подключения реальной release/privacy policy нельзя. Demo с реальными public snapshots должен сохранять синтетическую маркировку пользовательского ввода, но не называть реальные источники вымышленными; текущие loader/activation требуют отдельного явного решения для такого смешанного demo состава.

## Реестр доказательств

| Код | Исходники и проверка |
| --- | --- |
| E01 | `src/tsr/contracts/models.py`, `contracts/canonical.py`, `config.py`, `ports/__init__.py`; `tests/test_contracts.py::test_schema_extra_fields_and_result_exclusivity`, `test_unknown_and_zero_remain_distinct_and_known_needs_evidence`, `test_frozen_manifest_hash_binds_owner_generator_configuration_and_excludes_envelope_ids`, `tests/test_secrets.py` |
| E02 | `migrations/001_initial.sql`, `src/tsr/adapters/db/database.py:67–194,270–510`; `tests/test_db.py::test_explicit_commit_rollback_owner_and_encrypted_payload`, `test_scoped_job_dedupe_takeover_and_stale_fence_cas`, `test_tombstone_cleanup_fenced_and_clears_sensitive_ciphertext` |
| E03 | `src/tsr/operations/releases.py:60,190,219,238,251`, `bootstrap.py:42`, `adapters/db/database.py:546–611`; `tests/test_releases.py::test_release_hash_tampering_and_path_escape_rejected`, `test_atomic_demo_activation_cannot_promote_draft_or_revoked`, `tests/test_bootstrap.py::test_real_bootstrap_migrates_activates_and_restarts` |
| E04 | `src/tsr/adapters/max/ingress.py:60,156`, `http.py:28–106`; `tests/test_max.py::test_webhook_auth_and_minimization`, `test_http_refuses_unauthenticated_and_commit_failure`, `test_atomic_webhook_commit_and_duplicate_with_real_postgres` |
| E05 | `src/tsr/adapters/max/transport.py`, `presenter.py`, `runtime.py`; `tests/test_max.py::test_transport_timeout_is_unknown_and_callback_receipt_has_no_message_id`, `test_owner_binding_rejection_and_attachment_not_ready_do_not_claim_delivery`, `test_upload_emits_reference_and_does_not_forward_bot_token`, `test_live_transport_keeps_certificate_verification_for_default_and_custom_ca`; `tests/test_db.py::test_confirmed_receipt_must_match_persisted_delivery` |
| E06 | `src/tsr/domain/cases/__init__.py:20–190`, `application/service.py:45–335`; `tests/test_cases.py`, `tests/test_application.py::test_candidate_is_not_saved_before_confirmation_and_stale_handles_fail`, `test_change_offer_and_branch_preserves_confirmed_input_and_invalidates_result` |
| E07 | `src/tsr/domain/conversation/__init__.py:103–338`, `application/service.py:179–223,390–460`; `tests/test_application.py::test_verified_start_requires_explicit_role_and_errors_offer_recovery`, `test_long_answers_review_in_full_pages_and_material_pages_stay_bounded`, `test_terminal_render_failure_restores_review_instead_of_permanent_preparing` |
| E08 | `src/tsr/domain/catalog/__init__.py:5–102`, `application/catalog_service.py:14–40`, `application/service.py:215,304–329`; `tests/test_domain.py::test_catalog_semantics_units_and_stable_incomplete_pagination`, `test_unknown_fact_and_route_addressee_evidence_references_resolve` |
| E09 | `src/tsr/domain/matching/__init__.py:44–139`, `domain/pricing/__init__.py:12–83`; `tests/test_domain.py::test_synthetic_matching_known_mismatch_and_unknown`, `test_money_input_precision_and_business_limit`, `test_negative_certificate_precedence_unknown_shipping_and_large_total`, `test_conflict_optional_unknown_and_independent_supplier_conditions`, `test_invalid_matching_input_kind_is_validation_error` |
| E10 | `src/tsr/domain/routes/__init__.py:9–117`, `application/service.py:478–572`; `tests/test_domain.py::test_route_four_statuses_and_draft_pilot_block`, `test_unknown_fact_and_route_addressee_evidence_references_resolve` |
| E11 | `src/tsr/application/service.py:573–826,922–1050`; `tests/test_application.py::test_both_frozen_confirmed_flows_and_required_bundle_readiness`, `test_current_release_revocation_blocks_publication_and_current_material`, `test_durable_material_send_unknown_requires_explicit_retry_and_new_permit`, `test_crash_recovery_exhaustion_is_atomic_and_continue_prepares_fresh_preview` |
| E12 | `src/tsr/domain/documents/__init__.py:79–220`, `adapters/render/__init__.py`, `templates/registry.json`, `assets/fonts/`; `tests/test_documents.py`, `docs/verification/documents-qa.md` |
| E13 | `src/tsr/adapters/crypto/__init__.py`, `adapters/files/__init__.py:31–150`, `operations/cleanup.py:6–35`, `adapters/db/database.py:481–496`; `tests/test_files.py`, `tests/test_cleanup.py::test_generated_blob_cleanup_owner_epoch_and_idempotence`, `test_cleanup_failure_does_not_clear_artifact_metadata` |
| E14 | `src/tsr/worker/dispatcher.py:32–336`, `worker/recovery.py`, `adapters/db/database.py:348–380,454–468`; `tests/test_worker.py::test_real_stuck_child_times_out_and_render_slot_is_reusable`, `test_close_terminates_stuck_render_child_within_bound`, `test_uncorrelated_render_result_cannot_stage_or_publish`, `test_worker_retries_upload_only_under_current_fence_and_durable_state`, `test_crashed_renderer_exhaustion_through_worker_is_terminal_and_fenced`; `tests/test_db.py::test_recovery_marks_abandoned_failed_sending_unknown_without_reviving_jobs` |
| E15 | `src/tsr/cli.py:32–123`, `demo.py`, `bootstrap.py`; `tests/test_smoke.py::test_both_demo_branches_through_worker_to_download`, `tests/test_bootstrap.py`; `README.md`, `docs/FIRST_SCENARIO.md`, `docs/RUNBOOK.md`, `Dockerfile`, `compose.yaml`, `compose.max-ca.yaml` |
| E16 | `data/categories/manual_wheelchair/1.0.0/profile.json`, `data/catalog/mvp/1.0.0/catalog.json`, `data/sources/1.0.0/sources.json`, `data/routes/ru-alt/fund-topup/1.0.0/route.json`, `data/demo/1.0.0/profiles.json`, `data/golden/1.0.0/cases.json`, `data/releases/demo-1.0.0/manifest.json`; `tests/test_releases.py::test_demo_release_is_explicitly_synthetic_and_complete` |
| E17 | `docs/api/openapi.json`, `docs/api/DATA-API.yaml`, `docs/integrations/MAX.md`, `tests/fixtures/max/text.json`, `resources/messages.ru.json`, `src/tsr/application/messages.ru.json`, `docs/IMPLEMENTATION_STATUS.md` |

Отсутствия установлены по всему дереву `src/tsr`, миграциям, CLI и именам тестов, а не только по декларациям IMPLEMENTATION_STATUS. Например DTO HealthSnapshot/BackupReceipt не заменяют работающие collect_health/create_backup.

## Матрица исходного implementation backlog

| ID | Статус | Доказательство и оставшаяся часть |
| --- | --- | --- |
| C01 | done | E01: strict DTO/schema/extra fields, unknown/zero и typed payload; неполнота Protocol surface не меняет реализованное поведение |
| C02 | done | E01: NFC/canonical content, owner/revisions/generator/versions включены; envelope исключён |
| C03 | done | E01/E15: настройки, secret files, две роли, безопасный отказ startup; фактические deployment credentials — D11 |
| DB01 | done | E02/E06: owner FK, независимые revisions, immutable input, tombstone guards |
| DB02 | partial | E03: immutable release metadata и active pointer есть; public package content/indexed catalog и per-package lifecycle отсутствуют |
| DB03 | done | E02/E11: scoped manifest/bundle/artifact/attachment и owner checks; не означает завершённый retention |
| DB04 | done | E02/E14: inbox/job/outbox/send attempt/cleanup, dedupe, fence/lease/ready index |
| DB05 | partial | E02: UoW/rollback/commit ownership есть; часть портов и каталоговые/expiry индексы из плана отсутствуют, list queries не bounded |
| MAX01 | partial | E04: проверка секрета/minimization/dedupe/ACK; group content игнорируется безопасно, предусмотренного безопасного ответа нет |
| MAX02 | partial | E05: text/buttons/callback и process limiter; актуальная живая совместимость не проверена, shared multiple-worker limiter отсутствует |
| MAX03 | done | E05/E11: upload отдельно, binding owner/manifest, attachment-not-ready не делает render; фактический download двух клиентов — QA03 |
| MAX04 | done | E05/E14: confirmed/rejected/unknown, receipt correlation, timeout не safe retry |
| CASE01 | done | E02/E06: собственный кейс/представитель/list/access, чужой UUID не разрешает доступ |
| CASE02 | done | E06: candidate immutable, подтверждение hash+guard, старые кнопки и unknown |
| CASE03 | done | E06/E11: смена выбора/ветки/ввода инвалидирует подтверждение и старые jobs |
| CASE04 | done | E06/E07: semantic/dialog revision разделены, resume/materials не меняют manifest content |
| CASE05 | done | E02/E13: tombstone epoch, запрет нового доступа/publish/send и pending cleanup; retention отдельно |
| CONV01 | done | E07/E11: основные шаги двух веток, документ только после подтверждения |
| CONV02 | done | E06/E07: Back/изменить/resume/help, подтверждённый ввод сохраняется |
| CONV03 | partial | E07/E08: текстовые unknown/короткие карточки; все SKU недоступны, mobile/web читаемость ещё не подтверждена |
| CONV04 | done | E07/E11: preparing/failed/partial/historical/unknown delivery с действием продолжения |
| CAT01 | done | E08/E16: supplier/variant/snapshot/Fact evidence модель и валидатор; реальные сведения — D02/D03 |
| CAT02 | partial | E08: exact profile/ref и сохранение unknown работают; runtime demo-only, нет общего активного каталога |
| CAT03 | partial | E08/E11: immutable snapshot и whole-release revoke/history; независимый package lifecycle отсутствует |
| CAT04 | partial | E08: stable cursor/limit в DemoCatalog есть, но обычный UX всегда первые три и не вызывает этот порт |
| MATCH01 | done | E09: four field statuses, exact profile, empty-required incomplete, конфликт не match |
| MATCH02 | partial | E09: completeness/rank_comparisons есть; приложение не использует rank для отбора, первые три определяются порядком fixture |
| MATCH03 | done | E09: вопросы, operators/units/limits; отсутствие медицинского approval явно отделено в D01 |
| PRICE01 | done | E09: копейки, точность/NaN/минус/zero, 100 млн вход и 200 млн итог |
| PRICE02 | done | E09: negative precedence, from/unknown shipping, conditional, known zero, отсутствие ложного total |
| PRICE03 | done | E09/E12: calculated provenance/assumptions, gap не объявляется одобрением фонда |
| ROUTE01 | done | E10: fixed registry/no eval, source refs, unknown; реальные predicates требуют D04 |
| ROUTE02 | done | E10: четыре статуса, role checklist, независимость ЭС/договора и региона |
| ROUTE03 | partial | E10/E03: draft pilot blocked, address/expiry есть; real reviewed pack и независимый lifecycle не подключены |
| APP01 | done | E02/E04/E11: atomic command+processed+outbox, owner и повтор inbox |
| APP02 | done | E06/E07/E15: создание/подтверждение/resume/restored DB state; pilot intentionally hard-blocked |
| APP03 | partial | E08/E11: purchase не создаёт заявление и exact input/profile; выбор всех реальных предложений и active catalog отсутствуют |
| APP04 | partial | E10/E11: route preview и omissions/source; пока synthetic route, generic region pack loading отсутствует |
| APP05 | done | E01/E11/E14: frozen content, confirmation/hash, required files, stale publish/fence запрещены |
| APP06 | done | E11/E13: historical permit, явный duplicate warning/retry, deletion revoke |
| DOC01 | done | E01/E12: модель исключительно из frozen manifest, hash/versions фиксированы |
| DOC02 | done | E12: purchase PDF с ценой/доставкой/unknown/source/questions, без заявления |
| DOC03 | done | E12: PDF/DOCX, кириллица, plain text, bounded bytes и QA samples; не все реальные длинные данные ещё проверены |
| DOC04 | done | E11/E12: required artifact set и partial readiness/omissions |
| DOC05 | partial | E12/E16: четыре нужных файла рендерятся; approved route/адресат/шаблон отсутствуют, внешнее D04/D05 |
| FILE01 | done | E13: AES-GCM/private server paths/no static/path escape |
| FILE02 | done | E11/E13: owner/TTL/historical read без regeneration old revision |
| FILE03 | partial | E13: stage-before-publish, rollback orphan недоступен и manual case cleanup; active-claim-safe scheduled orphan cleanup отсутствует |
| W01 | partial | E14: отдельные render/delivery capacity, inbox quota; shared distributed limiter и измерение starvation/latency отсутствуют |
| W02 | done | E14/E02: lease/fence/claim renewal и render slot before claim; persistent process heartbeat относится OPS02 |
| W03 | done | E04/E11/E14: atomic inbox handling, dedupe/rollback |
| W04 | partial | E14: spawn max_workers=1, bounded IPC/hash/timeout; freshness умеет whole release, не все independently revoked packages |
| W05 | done | E11/E14: manifest/fence/hash, required-artifacts readiness, unique notification; удаление rollback orphans — FILE03 |
| W06 | done | E05/E11/E14: durable sending before external HTTP, upload separate, owner binding и transport outcomes |
| W07 | done | E14: cap/backoff/scoped recovery, sending→unknown, failed bundle terminal recovery |
| W08 | partial | E11/E13/E14: delete/edit/whole-release revoke гонки защищены; отзыв отдельного пакета не реализован |
| W09 | partial | E14: bounded render kill/close и recoverable jobs; нет общей SQL/delivery deadline, queue/compute/delivery metrics |
| OPS01 | partial | E03/E15: validate/stage/activation/revoke functions; CLI, generic content loader, lifecycle и полная negative semantic matrix отсутствуют |
| OPS02 | partial | E04/E05/E14: protected DB-only ready, live endpoint, transport inspect method; persistent heartbeat/queue/subscription health/alerts отсутствуют |
| OPS03 | missing | E13: manual tombstone cleanup не run_retention; нет 90/7/24h/30d scheduled policy и sweep |
| OPS04 | missing | E01/E15: DTO/runbook не выполняют backup/restore, consistency/deletion replay/access barrier |
| QA01 | partial | E09/E16: содержательные unit/contract boundaries есть; fixture GoldenCasePack не полностью исполняемый независимый expected harness, second profile expansion не показан |
| QA02 | done | E02/E11/E14: реальные PG crash/fence/delete/stale/correlation regression; live MAX crash не требуется для mocked fault injection |
| QA03 | external gate | E15 подтверждает local transport end-to-end; двух настоящих MAX clients/credential/HTTPS протокола нет |
| QA04 | partial | E01/E11/E13: owner/expired/path/secrets/delete checks есть; restore resurrection test отсутствует программно |
| QA05 | missing | Нет 5 events/s, 50 dialogs, 1000 snapshots и отдельного 100-doc run с p95/queue/error/completion; локально выполнимо |
| QA06 | external gate | Нет реальных участников, чередования обычный путь/бот и наблюдаемого результата; software test не заменяет исследование |
| REL01 | partial | E15: Dockerfile/Compose/lock/secrets/CLI; чистый Docker build/restart и ≤5 минут не измерены, Docker недоступен в прошлой среде |
| REL02 | partial | E15/E17: README/runbook/OpenAPI3.1/DATA-API; presentation PDF, закрытый slide, подтверждённый комплект доступов отсутствуют |
| REL03 | external gate | Версия локального/GitHub кода фиксируется; живой доступный bot/deployment/smoke/availability не подтверждены |

## Матрица задач данных

| ID | Статус | Доказательство и остаток |
| --- | --- | --- |
| D01 | external gate | E16 synthetic draft profile с тремя параметрами; экспертное утверждение units/operators/required/tolerance отсутствует. Разработчик не должен заменять его пометкой reviewed |
| D02 | partial | E16 пять synthetic offers двух вымышленных suppliers. Реальных Ortonica/Medicamarket snapshots нет. Новый запрос увеличивает цель до 10–15 конкретных предложений, минимум двух источников |
| D03 | partial | E16 source registry/evidence refs форма есть, URL example.invalid; нет real URL/observed/locator/use basis/reviewer доказательств |
| D04 | external gate | E16 synthetic route ru-alt; опубликованные условия, полный checklist/addressee/practical approval отсутствуют. Поиск источников возможен сейчас, процессная верификация отдельно |
| D05 | partial | E12 templates/specs/render+QA сделаны; внешняя проверка содержания/адресата/применимости и approved non-synthetic template отсутствует |
| D06 | done | E12 DejaVu regular/bold+license, embedding/Cyrillic проверены; имя approved-font.ttf не обязательно при корректных manifest refs |
| D07 | done | E16 synthetic profiles для обеих веток и ролей; no real user documents, тесты E09/E15 |
| D08 | partial | E16 golden JSON и E09/E12 независимые assert examples; нет полного expected-per-scenario schema/harness, approved real-source эталонов и полного набора экспортируемых expected documents |
| D09 | partial | E07/E17 русский словарь/labels/help есть; тексты распределены по нескольким файлам и inline, нет полного placeholders/message-key audit и client/user acceptance |
| D10 | partial | E04/E05/E17 official API notes и synthetic tests есть; fixtures corpus по типам/ошибкам неполон, только text.json отдельным файлом; live proof отсутствует |
| D11 | external gate | E01/E15 защищённые secret paths/config/CA готовы; рабочий bot token/HTTPS/subscription/access и live smoke не предоставлены |
| D12 | external gate | Актуальные исходные FAQ/срок/канал/правила доступа не предоставлены; submission checklist не завершён |
| D13 | external gate | Pilot blocked по config и приложению; утверждённый владелец/основание/политика/представительство отсутствуют. Программный retention из OPS03 всё равно можно реализовать заранее |
| D14 | partial | E03/E16 согласованный synthetic manifest/asset hashes/atomic pointer; real/mixed demo release, public package persistence и полный per-package audit ещё отсутствуют |

## Контракты и трассировка F01–F20

CONTRACTS §1–2 (DTO/errors/guards/versions), §3–5 (Fact/values/matching/money), §6–9 (commands/frozen content/delivery/transactions) в значительной части реализованы E01/E06/E09/E11/E14. Независимый аудит не обнаружил оснований отменять ранее подтверждённые owner/fence/epoch/send инварианты. Однако §10 перечисляет порты, которые пока частично существуют лишь как DTO/Protocol или in-memory service; §11 требует не только shape/hash, но **полную** семантическую матрицу activation. При добавлении public snapshots эти пробелы становятся функциональными, а не косметическими.

| Функция | Итог трассировки |
| --- | --- |
| F01–F03 начало/ввод/пояснение | E06/E07 локально реализованы; D01/D09 и QA03 ограничивают предметную/client приёмку |
| F04 комплектация | E08 partial: exact snapshot есть, все реальные предложения не доступны |
| F05–F06 сравнение/деньги | E09 локально реализованы; ranking wiring partial, реальные значения ещё D02/D03 |
| F07 источники/даты | E08/E12 форма и synthetic label есть; реальные source evidence отсутствуют |
| F08 вопросы | E09/E07 реализованы; вопросы формируются из unknown, не обещают соответствие |
| F09 выбор | E06/E11 сохранён и invalidated при изменении |
| F10 покупка | E11/E12/E15 локально работает, реальный каталог и live download не доказаны |
| F11 маршрут | E10 алгоритм есть, D04/external review отсутствуют |
| F12 документы обращения | E12 четыре файла есть, approved data/forms отсутствуют |
| F13 подтверждение | E01/E11 frozen preview/manifest подтверждены |
| F14 скачивание | E13/E15 local encrypted-storage→download; live MAX gate |
| F15 продолжение/материалы | E07/E11 реализованы, history DB reads пока не bounded |
| F16 восстановление | E14 crash/unknown recovery есть; OPS04 restore и W09 metrics/deadline отсутствуют |
| F17 удаление | E13 manual tombstone cleanup работает, scheduled retention отсутствует |
| F18 два клиента | QA03 external gate, нельзя закрыть Python-тестом |
| F19 данные/публикация | E03/E08 partial: generic active release, package lifecycle и CLI незавершены |
| F20 польза | QA06 external gate, процент улучшения не измерен |

## Приёмка нового расширения реальными источниками

Для 10–15 предложений нужны новые snapshot IDs, supplier/seller_sku/variant отдельно; exact configuration нельзя брать из неопределённого списка ширин и приписывать всем моделям. Для каждого known факта фиксируются URL, locator, observed date и source ref. Непубликованные доставка/приём ЭС/договор фонда остаются unknown. Цена «от» не становится exact, карточка линейки не даёт конкретную комплектацию. Автоматически собранная публикация не является экспертно reviewed медицинским правилом или подтверждением отношений продавца с фондом.

Локальная цепочка приёмки должна быть: новая source registry → schema/semantic validation → normalized catalog → доступ к любому предложению через UX → exact-profile matching → money calculation с unknown/conditional → frozen preview/confirmation → PDF/DOCX с теми же источниками/значениями. Нужны положительный и отрицательный сценарии, оба поставщика и выбор за пределами первых трёх. Synthetic fixtures следует сохранить для воспроизводимых граничных тестов. Активировать pilot только по факту completed code+reviewed data+privacy gates, а не общим флагом.

Для последующего финального ревью нужны: точный additive diff, результаты нового полного набора на реальном PG, фактический CLI сценарий на новом release, отчёт локальной нагрузки с методикой (если выполнен), retention/restore отрицательные тесты и список оставшихся внешних gates. Этот аудит не меняет код и не утверждает будущие исправления выполненными.
