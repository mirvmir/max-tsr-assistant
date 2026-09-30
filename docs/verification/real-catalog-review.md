# Независимое ревью требований и реального каталога

Дата: 30 сентября 2026 года. Первый проход и окончательная проверка исправлений. R1–R6 закрыты; все 26 новых страниц просмотрены. Остались небольшая проблема вёрстки и явно перечисленные незавершённые комплектные/внешние требования.

Проверяется diff от e720aa3331356e7e4e7d6b6d97ab74f233e927ca, включая новые файлы. Итоговый code/data anchor — **13dd1190755fc3d23a9e36b909de22a04c3fc946**. Ранний HEAD65f7ec450cbfb569566cf40423609547caea6268 содержал только план/исходный аудит и не выдаётся за проверенную реализацию. Этот отчёт и final load evidence добавляются позднее как docs-only delivery snapshot. Источники требований: [IMPLEMENTATION_PLAN_v1.md](../specs/IMPLEMENTATION_PLAN_v1.md), [CONTRACTS_v1.md](../specs/CONTRACTS_v1.md), [MVP_DATA_TASKS_v1.md](../specs/MVP_DATA_TASKS_v1.md), [повторный аудит](requirements-audit-2026-09-30.md) и [план расширения](../superpowers/plans/2026-09-30-requirements-real-catalog.md). Отдельный ARCHITECTURE_v1.md не предоставлен; требования другого чата не выдумывались.

**Локальное расширение реального каталога принято в проверенном demo scope; весь MVP не принят.** Есть 12 реальных предложений, нормализованный каталог, доступ к каждой странице, ранжирование, условная арифметика и документы двух веток. R1–R6 исправлены и независимо перепроверены. Контроллер выполнил финальный whole suite162passed; reviewer выполнил scoped53/93/47/1tests и late leaf revocation probes, лично просмотрел26freshpages обеих datasets. Критических/Important нарушений owner/fence/epoch, арифметики, публикации/current send или возрождения удалённого кейса в проверенных сценариях не осталось. QA05 закрыт в требуемом offline scope:50actors/1000synthetic/actual5events/s и отдельная очередь100documents, errors0. Кроме внешних экспертных/live/privacy gates остаются комплектные локальные DB02, QA01/D08, сбор фактического route D04 и release kit; это ревью не объявляет их выполненными. Minor orphan hash page отмечена ниже.

Ниже сохранены воспроизводимые исходные замечания. Матрица отражает scoped recheck, а его новые доказательства приведены в конце. Результат 53 passed относится к согласованному состоянию до corrective wave; дополнительный прогон 93 passed относится к исправлениям R1–R6 и не подменяет окончательный full-suite результат контроллера.

## Замечания

### R1 — Important — валидатор пропускал неверный enum в конфликте наличия

src/tsr/domain/catalog/__init__.py, validate_catalog/check_fact: enum наличия проверялся только у Fact.known, а alternatives конфликтного Fact проверялись только на kind=code. Независимый pure probe создал конфликт in_stock/banana с корректным source evidence; validate_catalog вернул valid=True, ошибок нет. Это противоречит CONTRACTS §11: каждая known/alternative должна принадлежать in_stock/on_order/out_of_stock. Нужны одинаковая проверка всех альтернатив и отрицательный тест с точным путём ошибки. Текущие 12 предложения имеют допустимые значения; находка касается допуска следующего пакета, а не подменяет результат фактической проверки.

### R2 — Important — импорт мог одобрить неисполняемый шаблон

src/tsr/application/service.py, _artifact_specs, выбирал template_ref по подстроке ID; src/tsr/domain/documents/__init__.py, TEMPLATE_IDS/build_document_request, и src/tsr/adapters/render/__init__.py, SUPPORTED/render_document, принимали только demo-* версии 1.0.0. При этом validate_release проверял renderer/format и assets, но не соответствие исполняемому template registry.

В временной копии изменён только template_id покупки на new-purchase-layout, пересчитан hash TemplateRegistry. validate_release вернул valid с нулём ошибок; _artifact_specs('purchase') затем бросил ValueError, а render_document вернул VALIDATION_ERROR. Обработчик pilot дополнительно безусловно отклонял любую case_mode=pilot. До появления утверждённых шаблонов это безопасный отказ, однако его нельзя описывать как полностью готовый generic pilot renderer, ожидающий только внешнего reviewer.

Минимальное исправление текущего состава: fail-closed importer по реальному renderer registry и выбор ArtifactSpec по document_kind/format/exact ref. Поддержка будущих reviewed assets остаётся одновременно программной и предметной задачей DOC05/D05; нельзя обещать, что произвольный активированный шаблон заработает после одного изменения флага.

### R3 — Important — документы содержали технические ключи и неполный список вопросов

src/tsr/domain/documents/__init__.py печатал pricing.reasons/pricing.assumptions и route.next_steps как machine keys. В фактически скачанных PDF/DOCX видны delivery.not_known, certificate.conditions_not_confirmed, certificate.applicable_and_accepted, route.synthetic_model, route.verify_real_rules, route.no_approval_promise. В renderer MISSING_LABELS отсутствовали supplier_accepts_certificate и supplier_has_fund_contract, поэтому они также попадали в пользовательский текст.

Карточка покупки и карточка ТСР собирали вопросы только из comparison.questions. У Base 200 сравнение трёх параметров полное, но доставка и приём сертификата неизвестны: документ сообщал «Вопросы из сравнения отсутствуют». UI уже использовал supplier_questions(comparison,quote), поэтому объяснение на экране и в документе расходилось. Нужны русские объяснения всех причин/предположений/следующих действий и вопросы из matching + pricing + route. Это критерии DOC02/D09, выполнимые локально.

Ранние скачанные файлы также предшествовали исправлению «Артикул: 5048» на «Код предложения продавца» и условным подписям денежных сумм. В текущем исходнике эти две правки уже есть. После всех правок требуется новый реальный CLI прогон и повторная визуальная проверка; старый PDF не доказывает новый результат.

### R4 — Important — freshness не сравнивал актуальные версии алгоритмов

src/tsr/application/service.py, _freshness, проверял generator/profile/catalog/release/offer/templates, но не сравнивал content.matching_algorithm_ref/content.pricing_algorithm_ref с текущими application refs. Независимый probe построил согласованный typed ManifestContent v1.0.0, затем поменял только current matching_ref на v2.0.0: _freshness(old_content,now).ok остался True. Отдельная смена pricing_ref дала тот же результат.

CONTRACTS §7 требует новый preview при новой версии алгоритма. Нужны две exact-ref проверки перед подтверждением/render/publication/current send, а исторический доступ должен сохранить отдельное предупреждённое разрешение. Эта находка не утверждает, что алгоритмы уже изменились в текущем release; она проверяет обещанный механизм проверки версий.

### R5 — Important для масштаба — экранная пагинация не ограничивала чтения и вычисление

src/tsr/adapters/db/database.py, EncryptedRepository._list/list_by_case/list_by_owner, читал и расшифровывал все записи истории. _cases_view/_materials_view ограничивали уже загруженный результат. _catalog_view первоначально сравнивал и записывал весь каталог в одной пользовательской UoW, а _comparisons искал cached результаты через полную историю. Поэтому отображение трёх предложений и пяти кейсов ещё не означало bounded DB query или короткую транзакцию при 1000 snapshots.

В corrective wave добавляется find_for_guard; проверка, попавшая между изменениями application и DB owner, один раз получила AttributeError отсутствующего метода. Этот промежуточный сбой передан контроллеру; его нельзя считать окончательной регрессией до синхронизации. DB05/QA05 следует закрывать по итоговому bounded query интерфейсу и измерению, а не числу элементов UI.

### R6 — остаток OPS02/W09 — эксплуатационные сообщения и измерения

Worker сохраняет heartbeat и privacy-safe queue counts/ages. Однако первоначальные logs worker содержали fixed message и иногда extra={job_id,code}; correlation/trace ID и formatter для extra не обнаружены. OPS02 требует безопасный correlation ID в alert. Постоянные compute/delivery duration метрики отсутствовали; отдельное измерение в load harness не является автоматически production telemetry W09. Нужна точная фиксация, что добавлено в corrective wave и что остаётся partial.

## Проверенные инварианты и границы

| Проверка | Наблюдение |
| --- | --- |
| Active release | Bootstrap читает (mode,bot_scope) pointer и точный immutable DB ReleaseRecord; не заменяет существующий pointer настроенным demo. Пакеты сверяются с hash-pinned файлами; missing package отклоняет старт. При переключении требуется restart/reload, что должно быть описано оператору. |
| Scoped lifecycle | Release/profile/catalog/route/sources/template/assets и отдельные source/snapshot/template refs имеют lifecycle. Public draft не допускается demo-флагом. Revoked/expired package блокирует activation/rollback/comparison/preview/render/publication/current material. |
| Замороженное содержание | Catalog/ref release/supplier добавлены как optional provenance; заполненные поля входят в hash. Canonical projection исключает только три новых None поля и сохраняет прежние null поля; старые fixtures не изменены. Preview копируется в ManifestContent без пересчёта. R4 относится к проверке текущих алгоритмов. |
| Выбор всех предложений | Реальные 12 offers проходят четыре страницы по три. Complete стоит перед incomplete/mismatch, lower-bound price не становится exact. Стабильные сохранённые ComparisonRecords сохраняют IDs/timestamps/hash; чужие/stale/out-of-range handles отклоняются. |
| Продолжение | Восстановление pending Candidate не меняет InputRevision до подтверждения. «Мои кейсы» и явный новый кейс сохраняют прежний ввод; новый кейс сначала спрашивает роль. Legacy profile не переводится молча в новый. |
| Retention | Case/manifest 90d; artifacts 7d; processed sensitive inbox 24h; minimal dedupe 30d; backups 7d. Case retirement использует tombstone/epoch/journal, foreign scope и текущие claims защищены. IO failure оставляет metadata/ref для повтора. Orphan sweep откладывается при live render и соблюдает grace. |
| Restore | Archive и журнал аутентифицируются, каждый ciphertext blob проверяется до SQL. Только пустая offline DB; scope/source_id/cutover проверяются. Отдельный persistent gate закрывает startup/application/material/transport до metadata checks, monotonic deletion replay и physical cleanup. Restore drill сохраняет retained ciphertext и удаляет tombstoned case/blob. |
| Внешний журнал | Journal export требует операторской остановки source writers и текущего cutover. Автоматическое независимое external journal dual-write не реализовано и не обещано. Восстановление без текущего journal, со stale journal, wrong blob key, tampered archive или в непустую DB отклоняется. |
| Доставка | Durable sending предшествует HTTP; upload отдельно. Owner/revision/epoch/TTL и freshness перепроверяются при authorize_delivery. Unknown не повторяется автоматически; явно подтверждённый retry получает новый permit. Уже начатую внешнюю отправку нельзя гарантированно отозвать. |
| HTTP | Только webhook/live/protected-ready. Ready проверяет DB, worker, exact release/dependencies, secrets, subscription и restore state; пустой MAX token остаётся degraded. Group content не используется для кейса; статическая подсказка направляется проверенному private actor. |
| IO | Рабочие DB connect/statement/lock/idle timeouts заданы; backup snapshot имеет отдельную границу. Render использует один spawn process и hard timeout. Bound отдельного statement не означает общую wall-clock границу произвольно длинной транзакции; R5/QA05 остаются существенными. |

## Публичные факты и уверенность цены

Реальный CatalogPack и normalized_catalog.json содержат одинаковые 12 записей: шесть Ortonica и шесть Medicamarket. SourceRecord указывает на фактическое извлечение с hash; URL/дата/locator, оригинальный HTTP-body hash и отдельная повторная проверка сохранены. [FACTUAL_AUDIT.md](../data/FACTUAL_AUDIT.md) и factual-audit.json фиксируют 12 свежих HTTPS GET и 151 успешную проверку контроллером. Review относится только к опубликованным техническим фактам; reviewer_id=codex-root-public-fact-audit, reviewed_at=2026-09-30T01:10:11.200322Z, due=2026-10-07T01:10:11.200322Z. Это не medical/legal/profile approval.

| Предложение | Проверяемый смысл |
| --- | --- |
| Ortonica Base 200, offer 5048 | Физическая ширина 40,5cm → 405mm, 130kg, точная опубликованная цена 14 500 RUB → 1 450 000 копеек. Offer ID 5048 отличается от опубликованного артикула 00031193. |
| Сертификат 10 000 RUB | Синтетически заявлен пользователем. При unknown acceptance покрытие 1 000 000 и gap 450 000 копеек являются условными. Unknown delivery сохраняет total=null. |
| Medicamarket Base 200 | Цена «от 16 724» не даёт точные coverage/gap/total. Конфликт шин H1/table сохранён в configuration/evidence, не создана фиктивная дополнительная SKU. |
| Medicamarket Base Lite 300 | 33 141/31 957 RUB сохраняются в conflicting price; price_kind=unknown. Расчёт не выбирает удобную сумму. |
| Medicamarket Base Lite 350 | out_of_stock/on_order сохраняются как conflicting availability. Не объявлено подтверждённое наличие. |
| Условия заказа | Для всех offers delivery, exact-order certificate acceptance и fund contract остаются unknown. Banner/JSON-LD shippingRate=0 не превратились в бесплатную доставку или true. |

Независимый reviewer проверил структуру/refs/hashes/численные ожидания и audit evidence. Полные исходные HTML не сохранены, поэтому архивной воспроизводимости всех первоначальных body не заявляется. Попытка дополнительного web open получила restricted/error для двух страниц, а Medicamarket Base Lite 300 — кешированный результат двухнедельной давности; кеш не использовался для опровержения свежего прямого GET и не подменяет фактическую проверку контроллера. Обновление снимков после due требует новой честной проверки и новой версии; автоматическое обновление продавцов не реализовано.

## Матрица 70 задач реализации

Done означает локально реализованный критерий в проверенном режиме, а не live/pilot approval. Partial указывает конкретный программный/комплектный остаток; external gate требует доступа, утверждения или участников. Матрица обновлена после scoped recheck, whole suite162, fresh artifact QA и обоих offline load modes; неизмеренную production/network capacity она не обещает.

| ID | Статус | Доказательство или остаток |
| --- | --- | --- |
| C01 | done | Immutable strict DTO, typed payload, schema major/extra rejection, unknown/zero; tests/contracts. |
| C02 | done | NFC/content hash, envelope exclusion, new optional provenance и legacy compatibility; tests/contracts. |
| C03 | done | Settings, secrets, две роли, startup refusal, явные IO bounds; live credentials D11 отдельно. |
| DB01 | done | Owner FK, semantic/dialog revision, encrypted immutable inputs, tombstone; tests/db/application. |
| DB02 | partial | Typed immutable packages, scoped pointer/lifecycle/audit работают; indexed candidate/catalog repository и полная портовая поверхность не закончены. |
| DB03 | done | Owner-scoped preview/confirmation/manifest/artifact/attachment bindings; tests/contracts/db/application. |
| DB04 | done | Inbox/jobs/outbox/send attempt/cleanup, unique dedupe, leases/fences/ready indexes; tests/db. |
| DB05 | done локально | SQL pages и exact resume/question/manifest/publish/receipt/identity/upload lookup, bulk fencing/backfill проверены47tests+lastcache1test. Пер decrypt limit подтверждён; глобальная wall-clock deadline произвольной transaction не обещана. |
| MAX01 | done | Verified ingress/minimization/ACK after commit/dedupe; static private-chat guidance без group case content. |
| MAX02 | partial | Typed transport, plain text/buttons/callback и DB shared quota реализованы; актуальная live совместимость/пограничная распределённая нагрузка не доказаны. |
| MAX03 | done | Upload отдельно, owner-bound token, attachment-not-ready не создаёт render; actual two-client download QA03. |
| MAX04 | done | confirmed/definitely_rejected/unknown, receipt binding, timeout не safe retry; tests/max/db. |
| CASE01 | done | Owner access/list, new case/role, UUID другого owner не разрешает доступ; bounded query часть R5. |
| CASE02 | done | Pending immutable candidate, explicit unknown, stale/foreign/expired confirmation rejected. |
| CASE03 | done | Offer/branch/input changes invalidate confirmation/jobs and preserve confirmed input. |
| CASE04 | done | Independent revisions, resume/materials/history, legacy profile сохранён без conversion. |
| CASE05 | done | Tombstone/epoch/cleanup journal and refusal of new current access/publication/send. |
| CONV01 | done | Обе карты шагов; никакого документа до exact preview confirmation. |
| CONV02 | done | Back/edit/resume/help, pending candidate restoration, known input сохранён. |
| CONV03 | partial | Все 12 offers, три короткие карточки, словесный unknown; mobile/web readability требует QA03. |
| CONV04 | done | Preparing/error/partial/historical/unknown-delivery explanations and recovery actions. |
| CAT01 | done | Supplier/variant/snapshot/Fact/evidence модель работает; enum known и conflicting alternatives проверен, R1 исправлен. |
| CAT02 | done | Exact profile/active release, source/variant identity, unknown не отбрасывается; indexed масштаб DB02 отдельно. |
| CAT03 | done | Immutable snapshots, exact leaf lifecycle, history/revocation/expiry; алгоритмы R4 отдельно. |
| CAT04 | done | All pages reachable, stable order/limit; CatalogQuery cursor bounded20; UI по3, stale handles rejected. |
| MATCH01 | done | Field statuses и complete/incomplete/mismatch, exact profile, conflict not match; tests/domain/real_catalog. |
| MATCH02 | done | Application использует completeness-first rank; exact price only, incomplete не повышается до complete. |
| MATCH03 | done | Registered operators/units, supplier question generation, no invented tolerance; medical D01 отдельно. |
| PRICE01 | done | Integer kopeks, strict precision/input max, zero/negative/NaN/large total tests. |
| PRICE02 | done | Negative certificate precedence, from/conflict no exact total, conditional/unknown shipping preserved. |
| PRICE03 | done | Calculated provenance/assumptions; gap не является одобрением фонда; document projection R3. |
| ROUTE01 | done | Fixed predicate registry, source refs/no eval; approved real conditions D04 отсутствуют. |
| ROUTE02 | done | Four statuses, role checklist/region, acceptance и contract независимы; tests/domain. |
| ROUTE03 | partial | Draft/review/lifecycle/expiry работают; полноценный реальный пакет и practical review D04 отсутствуют. |
| APP01 | done | Atomic command+processed+outbox, owner/dedupe/savepoint rollback; tests/application/db. |
| APP02 | done | Generic active case/input/resume/new role/legacy preservation; pilot start uses data/privacy gates. |
| APP03 | done | All real offers, exact input/profile/comparison/quote; purchase создаёт только purchase PDF. |
| APP04 | partial | Route preview/addressee/checklist/missing fields связаны; renderer dispatch R2 исправлен, утверждённое реальное содержание D04 отсутствует. |
| APP05 | done | Frozen hash/confirmation/jobs/required bundle/late publish; exact algorithm freshness и opaque template dispatch проверены после R2/R4. |
| APP06 | done | Explicit historical material and duplicate retry; tombstone/epoch blocks new access. |
| DOC01 | done | Pure content projection из frozen manifest, provenance/versions/hash. |
| DOC02 | done локально | Human pricing reasons/assumptions/route actions/missing labels и combined supplier questions проверены tests и fresh10actualfiles. Conditional4500RUB/unknownshipping сохраняются. |
| DOC03 | done с minor polish | All26freshpages (8PDF+2DOCX) просмотрены; Cyrillic/output bound/escaping/font checks passing, bboxoutside0. Public product имеет hash-onlypage4; это minor layout, не lost content. |
| DOC04 | done | Purchase1/support4 required ArtifactSpecs, only complete bundle ready; partial readiness explicit. |
| DOC05 | partial | Four local draft files есть; exact registry binding и fixed trusted engines поддерживают свежий public reviewed pilot registry. Official approved route/template assets и их предметная пригодность отсутствуют. |
| FILE01 | done | AES-GCM, ciphertext private volume, UUID server filenames, no public download endpoint/traversal. |
| FILE02 | done | Owner/epoch/TTL/current/historical permits, old revision не генерируется при download. |
| FILE03 | done | Atomic stage before publication, scheduled lease-safe orphan cleanup, IO retry/restore ciphertext. |
| W01 | done в local scope | Three job kinds, independent renderer/sender/shared quota tested. Offline50dialogs и separate100docs завершены без durable errors; вход нагрузки facade, поэтому HTTP/process_inbox ingress throughput и живой MAX этим не измерены. |
| W02 | done | Claim/lease/fence CAS, renewal, one slot before claim; persistent worker heartbeat. |
| W03 | done | Durable inbox transition and dedupe/atomic recovery; tests/db/application/worker. |
| W04 | done | Spawn1, bounded IPC/output/hard timeout, leaf checks, exact algorithm freshness и typed template registry ребёнку; 93 scoped tests. |
| W05 | done | Publication guard/hash, last required file single notification; safe orphan sweep. |
| W06 | done | Upload separate, current authorize before durable sending, receipt/outcome semantics. |
| W07 | done | Bounded safe retry/crash exhaustion, sending→unknown, no automatic ambiguous resend. |
| W08 | done | Edit/delete/package revoke before publish/current material, exact algorithms; независимый late source/snapshot revoke перед authorize_delivery отклоняет current send, предупреждённая история сохранена. |
| W09 | done в local scope | Persistent queue/compute aggregates/safe trace logs, bounded hot DB ports/render kill/graceful shutdown и offline p95/queue/completion проверены. Hard render bounds не выдаются за гарантированную production end-to-end latency. |
| OPS01 | done | CLI validate/import/activate/rollback/revoke/audit; enum alternatives и registry dispatch/ambiguity negatives проходят. |
| OPS02 | done локально | Protected readiness/heartbeat/queue/subscription/secrets/restore gate; SafeJSONFormatter показывает opaque trace/code без exception text, persistent bounded metrics. |
| OPS03 | done | Scheduled retention90/7/24h/30d, retryable physical erase and global claim-safe janitor; tests/maintenance. |
| OPS04 | partial | Encrypted consistent DB+blob backup/current journal/offline restore drill есть; separate durable hosting/key placement/cutover practice не проверены. |
| QA01 | partial | Independent boundary/numerical tests и per-scenario expected validator есть; полный executable GoldenCasePack/document oracle corpus ещё не представлен. |
| QA02 | done | Real PG crash/fence/delete/stale/owner/receipt fault tests, spawn regressions; live crash не требуется для local injection. |
| QA03 | external gate | Local CLI обе ветки есть; нужны настоящие mobile/web MAX, HTTPS/token/subscription/download protocol. |
| QA04 | done | Access/TTL/path/crypto/deletion/restore antiresurrection tests; live/provider privacy границы отдельно. |
| QA05 | done offline | Actual5.0events/s50actors1000synthetic/125files и отдельная paused-worker queue100documents, errors0; оба JSON/methodology проверены ниже. MAX моделируется, HTTP/provider measurements отсутствуют. |
| QA06 | external gate | Нет реальных участников, чередования обычного пути/бота и наблюдаемой пользы. |
| REL01 | partial | Dockerfile/Compose/lock/secrets/init/restart code; Docker binary/build и ≤5min clean startup не измерены. |
| REL02 | partial | README/runbook/API/first scenario обновляются; presentation PDF/service slide/submission access kit отсутствуют. |
| REL03 | external gate | Не подтверждены live deployment/bot/smoke/availability/final delivered commit; контроллер фиксирует GitHub tree отдельно. |

## Матрица 14 задач данных

| ID | Статус | Доказательство или остаток |
| --- | --- | --- |
| D01 | external gate | Технический synthetic draft профиль не является экспертно утверждённым медицинским правилом. |
| D02 | done в requested demo scope | 12 конкретных публичных offers6+6, exact SKU/config/units, conflict/from/unknown сохранены; tests/real_catalog. |
| D03 | done для фактических снимков | Registry/evidence/hash/URL/observed/reviewer/due/use_basis и root factual audit есть; автоматическое переиспользование/обновление источников не утверждено. |
| D04 | partial + external gate | Synthetic route остаётся моделью. Полное получение опубликованных условий/адресата ещё отсутствует и технически возможно; practical/expert approval требует владельца процесса. |
| D05 | partial | Draft layouts/content, human explanations и exact runtime registry исправлены. Official approved non-synthetic assets/применимость отсутствуют; будущие реальные формы требуют проверки, fixed engines не обещают исполнять произвольные templates. |
| D06 | done | Licensed DejaVu regular/bold, Cyrillic/render/embedding/hash/license проверки. |
| D07 | done | Synthetic fixtures двух веток/ролей, значения/пропуски, без real user documents. |
| D08 | partial | Independent numerical expectations, golden form validation и local outputs есть; полный golden execution/document snapshots corpus не закончен. |
| D09 | partial | Pricing/route/missing machine-key leakage R3 исправлена и tested; полный key/placeholder corpus audit и client/user acceptance отсутствуют. |
| D10 | partial | Official MAX notes и synthetic errors/response tests; полный отдельный fixture corpus и live contract proof отсутствуют. |
| D11 | external gate | MAX token/HTTPS/subscription/limited live access не предоставлены; защита секретов реализована. |
| D12 | external gate | Правила сдачи/FAQ/канал служебного слайда не предоставлены; отсутствующий источник не объявлен прочитанным. |
| D13 | external gate | Retention реализован, но real pilot owner/basis/policy/representation approval не получены. |
| D14 | done для mixed demo | Compatible refs/hashes/package DB content/scoped activation/audit работают; synthetic parts не заявлены reviewed pilot pack. |

## F01–F20

| Функции | Итог |
| --- | --- |
| F01–F03 | Local role/input/confirmation/resume/help работают; medical/client acceptance D01/QA03 отдельно. |
| F04–F09 | All12offers/exact compare/rank/money/source/date/select сохранены; combined document questions/human explanations R3 и exact algorithm freshness R4 исправлены, tested и проверены в freshactualfiles. |
| F10 | Real-catalog purchase→PDF/download подтверждён локально; живой MAX download QA03. |
| F11–F12 | Модельный route/четыре draft files; opaque template registry dispatch R2 исправлен, fresh reviewed registry проверен с fixed engines. Реальные условия/официальные approved assets D04/D05 остаются partial. |
| F13–F15 | Frozen preview/manifest, owner current/historical access, pending resume/case pages; exact bounded history/query ports R5 исправлены и tested. |
| F16–F17 | Crash/unknown recovery, offline restore+current journal, tombstone/retention; safe trace logs и scoped persistent queue/compute aggregates R6 исправлены, deployed operational drill отдельно. |
| F18 | Два настоящих MAX клиента — external gate. |
| F19 | Real-source release/lifecycle/CLI, enum alternatives и exact importer/renderer registry compatibility реализованы и tested. Indexed catalog repository DB02 и полный golden corpus QA01/D08 остаются отдельными локальными partial. |
| F20 | Comparative user study/observed benefit отсутствует; процент улучшения не выдумывается. |

## Проверки reviewer

Выполнено независимо на реальной PostgreSQL; каждый тест использует собственную random schema, restore — собственную временную DB. Внешний MAX не вызывался.

    TSR_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres PYTHONPATH=src /workspace/scratch/2928286d106b/.venv/bin/python -m pytest tests/test_backup.py tests/test_maintenance.py tests/test_bootstrap.py tests/test_application.py tests/test_contracts.py tests/test_real_catalog.py tests/test_releases.py -q
    53 passed in 19.27s

Дополнительно: pure probes R1/R2/R4, git diff --check (clean на момент проверки), неизменность старых version1.0 fixtures, чтение actual scenario-public.log/scenario-synthetic.log и source audit; public artifacts4PDF+1DOCX rasterized. Все 15 страниц (12PDF+3DOCX) просмотрены. PDF char-bbox inspection нашёл 0 символов вне страницы и 0 за body margin. Содержательные R3 дефекты выявлены, поэтому визуальная аккуратность не означает готовность текста.

DOCX был отрендерен canonical Documents skill renderer с bundled primary runtime/LibreOffice; PDF — системным Poppler. Intermediates находятся вне репозитория, /workspace/scratch/2928286d106b/review-artifacts. Reviewer не менял код, не staging/commit/push; единственный authored repository файл — этот отчёт.

Финальный полный pytest и обе нагрузки выполнены контроллером; reviewer проверил их переданные результаты и реальные JSON, что отражено ниже. GitHub delivery snapshot фиксируется контроллером. Эти действия отделены от лично выполненных reviewer проверок. Docker/live MAX/privacy approval/medical expert review/user study не выполнялись и не объявляются успешными.

## Выполненный план scoped recheck

1. Проверить R1/R2/R4 отрицательными probes и целевыми тестами на согласованном app/DB/renderer интерфейсе.
2. Повторить actual installed CLI обеих dataset/веток после R3; проверить новые PDF/DOCX, conditional arithmetic и frozen hash.
3. Подтвердить late source/snapshot revoke между material request и authorize_delivery, сохранение warned historical access.
4. Оценить методику и actual throughput в QA05: passed=true без достигнутой нагрузки не закрывает требование, facade timing не является HTTP/MAX latency; separate100doc result нужен отдельно.
5. Получить final whole-suite результат и зафиксировать проверенный commit/tree. Полный MVP не объявлять готовым, пока перечисленные внешние и комплектные gates остаются незакрытыми.

## Scoped recheck corrective wave

Лично выполнен повторный прогон на согласованном app/DB/renderer интерфейсе:

    TSR_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres PYTHONPATH=src /workspace/scratch/2928286d106b/.venv/bin/python -m pytest tests/test_domain.py tests/test_documents.py tests/test_releases.py tests/test_contracts.py tests/test_paging.py tests/test_application.py tests/test_worker.py -q
    93 passed in 20.83s

| Замечание | Текущий результат |
| --- | --- |
| R1 | Исправлено. Known и каждая conflicting availability alternative проверяются по одному enum; отрицательный test проверяет недопустимую alternative. |
| R2 | Исправлен локальный runtime gap. Importer отклоняет duplicate/ambiguous document_kind+format и неизвестный engine. App и domain выбирают exact frozen template_ref по kind/format. Typed registry передаётся child renderer; новые opaque IDs/версии работают с reportlab-v1/python-docx-v1, чужая ref/version/kind/engine отклоняется. Legacy demo без registry остаётся узкой совместимостью. Pilot принимает только свежий public reviewed registry; это не доказательство approved official assets. |
| R3 | Исправлено. Русские причины/предположения/действия/пропуски и вопросы matching+pricing+route, seller code/conditional money проверены tests и всеми26freshpages. Forbidden8keys отсутствуют во всех10actualfiles. |
| R4 | Исправлено. Current matching_ref/pricing_ref сравниваются exact equality. Оба алгоритма имеют tests: смена версии блокирует current material, historical предупреждённый доступ сохраняется, frozen hash не меняется. |
| R5 | Исправлено локально. SQL pages bound decrypting (case5/artifact8/bundle3), count без decrypt, comparisons guard+input+snapshot lookup LIMIT3/index006. Ranking1000 pure; persist visible3/reuse IDs/hashes. Migration007/exact hot ports/bulk fencing прошли47tests, последняя uploadcache LIMIT1/backfill test отдельно1passed. |
| R6 | Исправлена проверяемая локальная часть. SafeJSONFormatter allowlists fixed event/code/opaque UUID и не раскрывает exception text; tests включают секретный exception. Scoped persistent aggregates count/total/last/max для queue/compute трёх job kinds не содержат owner/job/trace/payload и имеют фиксированный набор имён. Actual capacity и отдельная100doc нагрузка относятся к QA05. |

На раннем R5 recheck перечисленные `_latest_question`, `_normal_view`, `_confirm_preview`, `_actor_for`, `publish_render_result`, `_invalidate` и start_case ещё читали всю историю. После следующей corrective wave эти **hot paths исправлены**: current dialog/guard/confirmation/owner+case lookup, get_many максимум4published IDs с проверкой manifest/revision/epoch, SQL bundle transition/job fencing и owner role-handle invalidation. Job invalidation сохраняет durable sending/unknown и право записать поздний исход; stale renderer fence отклоняется. Migration007 backfills оперативные references и encrypted-key HMAC receipt index без изменения legacy encrypted DTO/hash; metadata не содержит raw message/callback IDs.

Лично повторно выполнено:

    TSR_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres PYTHONPATH=src /workspace/scratch/2928286d106b/.venv/bin/python -m pytest tests/test_application.py tests/test_paging.py tests/test_backup.py tests/test_max.py -q
    47 passed, 1 warning in 21.53s

Warning — Starlette TestClient/anyio BlockingPortal deprecation, не failure. Hot-path test запрещает full-history ports; отдельные tests проверяют объём decrypt, foreign/current guard/expired candidate, receipt type+owner+target binding/backfill, bulk fencing и восстановление backup. Это дополнительный прогон, его число не суммируется с93как число уникальных tests.

Последний выявленный R5 lookup `AttachmentRepository.find_for_artifact` также исправлен: owner+artifact+manifest hash SQL filter, observed_at DESC/id DESC, LIMIT1; decoded record снова проверяет identity/hash. Metadata backfill не меняет encrypted DTO. Лично выполнен `tests/test_paging.py::test_attachment_cache_exact_hash_latest_and_backfill_bound_one_decode`: **1passed in0.50s**; проверены latest/exact/foreign/hash/legacybackfill и максимум один decrypt. Offline migration/backfill и maintenance backup inventory имеют иной размер работы, чем пользовательский hot path; overall capacity проверяется измерением, timeout отдельного SQL не выдаётся за глобальную deadline транзакции.

Независимые probes позднего отзыва выполнены дважды на **реальном public release1.1.0**, в отдельных автоматически удалённых тестовых PostgreSQL схемах. После успешного `request_material(current)` и `get_material_context` отозван отдельно leaf SourceRecord ref и отдельно leaf Snapshot UUID/catalog1.1.0. В обоих случаях `authorize_delivery` вернул DATA_REVOKED, outbox остался pending, frozen manifest hash не изменился. Новый historical request с warning_acknowledged=True получил send permit. Проверены application admission/DB guards; renderer publication в этих probes использовал типизированный тестовый StagedArtifact, HTTP/upload/реальная передача файла не выполнялись. Первая попытка snapshot probe завершилась ошибкой reviewer script (ошибочное обращение к отсутствующему OfferSnapshot.version), затем повторена с верной catalog.version; это не дефект продукта.

Hash compatibility повторно прошла в tests/contracts: canonical projection исключает ровно три additive None поля, legacy raw payload/hash сохраняется; обогащённые catalog/release/supplier меняют hash, foreign supplier отклоняется, добавление registry в RenderJobContext не меняет manifest hash. Full source/profile expert approval отсутствует и не подменяется этими проверками.

Методика load harness исправлена: cadence независим от compute, observed rate имеет обязательный lower bound98%target; documents mode останавливает worker после setup и подтверждениями создаёт отдельную очередь100render jobs, сбрасывая измерения setup. Reviewer прочитал actual regenerated [load-check.json](../qa/load-check.json) и отдельный [document-load-check.json](../qa/document-load-check.json), проверил контроль actual rate/queued-documents и методику в tools/load_check.py. Их числа приведены ниже; прежний JSON с ошибочной методикой не используется.

## Итоговые фактические результаты

Контроллер на зафиксированном code/data commit 13dd1190755fc3d23a9e36b909de22a04c3fc946 выполнил полный `pytest -q` с локальной PostgreSQL: **162 passed, 1 upstream warning за 41,10 с**. Reviewer не выдаёт этот полный прогон за лично исполненный; собственные scoped команды и probes перечислены выше. Все собственные тестовые schemas после probes удалены, live MAX calls отсутствуют. `git diff --check` после отчёта чистый.

Installed CLI заново прошёл public и legacy dataset в обеих ветках: public purchase1PDF/support4files ready, price14 500RUB/certificate10 000RUB/conditionalgap450 000kopeks/ownTotalnull; synthetic purchase1/support4 ready/gap2 000 000kopeks. Local downloads содержат8PDF+2DOCX. Root text/font checks дополнены независимым render+visual QA:

| Dataset/artifact | Страниц | Результат |
| --- | ---: | --- |
| Public purchase19abd706…PDF | 3 | Conditional4500RUB, unknown delivery, seller offer5048, human supplier questions, facts/date/source/versions/hash. |
| Public product189cad4a…PDF | 4 | Те же frozen facts; fund-contract question; page4 содержит только hash — minor UX. |
| Public application4a29743d…PDF /fbb93b2f…DOCX | 3/3 | Учебный адресат, имя/role/region, real selected product, model route/unknown missing fields, frozen hash. |
| Public checklistbfe3c97a…PDF | 3 | Required/optional/not-applicable states сохранены; human actions, unknown acceptance/fund contract. |
| Legacy4PDF /98f51bab…DOCX | 8/2 | Synthetic provenance явно отмечена;120000−100000=20000RUB и известный учебный zero shipping не перенесены в public данные. |

Все **26страниц** лично просмотрены, без найденных clipping/overlap/повреждённой кириллицы. Poppler rasterization и canonical DOCX/LibreOffice renderer выполнялись вне git; intermediates `/workspace/scratch/2928286d106b/review-artifacts/final`. Независимый pdfplumber char-bbox check для8actualPDF+2DOCX-renderPDF: outside_canvas0 в каждом файле; bodymaxx1 publicPDF543.94pt/publicDOCX550.42pt при ширинеA4≈595pt. Все8исходных R3machine keys отсутствуют в10freshfiles. Содержательные assertions и visual QA взаимно дополняют друг друга, они не заменяют двух MAX клиентов.

**Minor UX M1:**13source citations в каждом public document увеличивают объём; в product PDF последняя page4 почти пуста и содержит только frozen hash. Содержание цело, hash связан с подтверждением, purchase PDF3pages читабелен. Этот scoped correction не включает новый layout refactor после freeze; при следующей работе стоит держать hash с относящимся к нему блоком и компактнее представлять источники. Существующая модельная форма остаётся draft и не объявляется UX/user-study accepted official form.

| Offline workload | Dialogs | Separate documents |
| --- | ---: | ---: |
| Actors /synthetic snapshots | 50/1000 | 40setup/1000 |
| Steady target /observed events/s | 5/5.0 | Не steady test |
| Initial queued render jobs | Integrated dialog work | 100before worker resume |
| Ready/requested/localdownload files | 125/125/125 | 100/100/100 |
| Completed dialogues /errors | 50/0 | 40/0 |
| Total elapsed /document phase | 218.79s | 79.844s inclsetup/26.197s |
| p95queue /facade /render /localdelivery | 3120.652/66.26/88.181/15.566ms | 9384.696/57.855/88.208/20.013ms |
| Queue/render/delivery samples | 1425/125/1300 | 420/100/320 после setup reset |
| Durable jobs /outbox | 1425succeeded/1300confirmed | 1140succeeded/1040confirmed inclsetup |

Оба actual JSON: passed=true, guarded_work_rejections={}, errors0, external_max_calls0, public_snapshots0. Dialogs cadence measured first/last steady admission исключает completion wait; initial50start burst измерен отдельно. Documents queueing100renderjobs заняло1.119s при paused worker; timing samples сброшены после setup, итоговые durable counts включают setup и не смешаны с phase samples. Один spawn renderer и один sender; local typed receipt/upload/download simulations. Это измеренная ёмкость данных offline сценариев с application facade admission. HTTP/webhook/process_inbox ingress, proxy, provider API limits и реальные mobile/web downloads не измерены; p95render включает polling/spawn, p95localdelivery не называется network latency. Экспертное/pilot утверждение данных и user study остаются внешними/комплектными gates матрицы.
