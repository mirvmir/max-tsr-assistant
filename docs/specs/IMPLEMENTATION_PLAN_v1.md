# План реализации модульного монолита с PostgreSQL и worker

Версия 1.0.0 от 30 сентября 2026 года. Основание — принятая пользователем ARCHITECTURE_v1.md. Все чекбоксы ниже относятся к будущей реализации и остаются незакрытыми. Подготовка этого плана не означает выполнения кода, получения реальных данных или прохождения нагрузочных тестов.

Комплект состоит из этого backlog, `CONTRACTS_v1.md`, `MVP_DATA_TASKS_v1.md`, схем `schemas/mvp-data.schema.json`, синтетических образцов `examples/` и протокола ревью. CONTRACTS — единый источник определения типов; названия DTO в таблицах ниже ссылаются на него. Пути будущего кода заданы относительно корня репозитория.

## 1 Результат реализации и границы

Реализуем обе основные ветки: выбор комплектации и карточка покупки; предварительная проверка применимого маршрута и пакет черновых документов. Сохраняем прогресс, объясняем неизвестности, поддерживаем возврат/исправление и скачивание в мобильном/веб-MAX.

Одна кодовая база и один образ приложения запускаются в ролях HTTP app и worker. Worker использует PostgreSQL inbox/jobs/outbox и один ограниченный дочерний render process. Длительные операции не выполняются в HTTP обработчике или внутри SQL транзакции. Государственные кабинеты, LLM, OCR, оплата, подача обращения и mini-app не входят в этот backlog.

## 2 Порядок зависимостей и ответственность

| Этап | Задачи | Результат |
| --- | --- | --- |
| Основа | C01–C03, DB01–DB05, параллельно D01–D14 | Контракты, миграции, данные staging, способ запуска |
| Сохранённый диалог | MAX01–MAX04, CASE01–CASE04, CONV01–CONV04, APP01–APP02, W01–W03 | Событие MAX → сохранённый ввод → продолжение |
| Покупка | CAT01–CAT04, MATCH01–MATCH03, PRICE01–PRICE03, DOC01/DOC04 → APP03/APP05 → DOC02/DOC03, FILE01–FILE03, W04–W06 | Выбранная комплектация → подтверждённая карточка → скачивание |
| Обращение | ROUTE01–ROUTE03, APP04, DOC05; расширение сценариев общего APP05 | Проверенный маршрут → неполнота/подтверждение → четыре файла |
| Защита и восстановление | APP06, CASE05, W07–W09, OPS01–OPS04, QA01–QA06 | Повторы, версии, доступ, удаление, восстановление и нагрузка |
| Сдача | REL01–REL03 | Проверяемый бот, чистый Docker запуск, фиксированная версия и комплект |

Участник 1 ведёт инфраструктуру, MAX, транзакции, worker и развёртывание. Участник 2 ведёт данные, предметные правила, UX, шаблоны и эталоны. Контракты и сквозные прогоны делают оба. Предметный эксперт нужен для D01/D04; он не заменяется разработчиком. Зависимости этапов — порядок интеграции, а не обещание сроков в часах.

Зависимости кода направлены так: contracts/ports → pure domain → application → подключение DB/MAX/files/render в composition root. Здесь стрелка означает «следующий слой использует предыдущий». Domain не импортирует application, worker или адаптеры. Указанные ниже DB/D-задачи возле доменного модуля — зависимости интеграции/данных, а не разрешение SQL-зависимости. DOC01/DOC04 определяют pure модель и состав файлов до APP05; APP05 использует их, а worker соединяет APP05 с renderer/storage. Цикла импорта нет.

## 3 Общий стандарт для каждой задачи

Входы и выходы задачи заданы таблицей публичных функций её модуля. Ошибки — единый `Result[T]/DomainError` из CONTRACTS §2; конкретные коды приведены рядом. Приёмка требует реализованных типов, поведения нормального/неизвестного/ошибочного входа и указанного критерия. Ожидаемые данные ссылаются на D-задачи, а тестовые заменители всегда имеют demo метку.

Обозначения функций: **граница** — разрешена вызову другим модулем; **внутренняя** — helper реализации. Только application владеет пользовательской транзакцией. Репозитории не делают commit самостоятельно. Каждая mutating функция проверяет owner и необходимые CaseGuard/DialogGuard; workers дополнительно проверяют claim. Публичные Python функции не превращаются в HTTP endpoints.

## 4 Модуль contracts и базовая конфигурация

Пути: `src/tsr/contracts/`, `src/tsr/config.py`, `src/tsr/ports/`. Зависимостей на другие модули нет. Необходимые данные: соглашения CONTRACTS, настройки D11.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| validate_dto(type_name, payload) | Имя модели, JSON-compatible значение | Result[DTO] | Проверка схемы/версии, без IO |
| canonical_bytes(value, projection) | Типизированное значение, заданная проекция | bytes | Детерминированное представление для hash |
| content_hash(value, projection) | Тот же вход | SHA256 string | Не включает собственное поле hash |
| load_settings(env, secret_reader) | Имена настроек, разрешённый reader | Result[Settings] | Секреты доступны только нужным адаптерам |

Внутренние функции: `normalize_decimal`, `normalize_unicode`, `validate_money_minor`, `reject_unknown_schema`, `redact_setting`. Settings включает пути secret files, БД, MAX API URL, HTTPS URL, режим, TTL, лимиты ввода/файла, lease и число render slots; точные defaults тестируются и описываются в README.

- [ ] **C01** Создать общие DTO/enums и Pydantic модели по CONTRACTS; результат — один набор типов для всех модулей. Приёмка: чужая major-версия и лишние поля отклоняются, null/unknown/0 различаются.
- [ ] **C02** Реализовать канонизацию candidate hash и ManifestContent hash, общий для preview/manifest. Приёмка: изменение owner, значения, версии правила, генератора или комплектации меняет hash; порядок ключей JSON — нет; служебные IDs подтверждения не меняют content hash.
- [ ] **C03** Настройки и dependency injection для двух ролей запуска. Приёмка: отсутствие обязательного секрета даёт безопасную startup ошибку, значения не попадают в лог.

Ошибки: VALIDATION_ERROR, UNSUPPORTED_SCHEMA. Объявления портов здесь задают интерфейсы, реализации DB/MAX/Files находятся в адаптерах.

## 5 Модуль persistence

Пути: `src/tsr/adapters/db/`, `migrations/`. Зависимости: C01–C03. Данные: таблицы из MVP_DATA_TASKS §4; реальные пакеты ещё не нужны.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| uow_factory.begin() | Транзакционная политика | UnitOfWork | Открывает короткую транзакцию |
| CaseRepository.lock_owned(ctx, guard) | ActorContext, CaseGuard | Result[CaseSnapshot] | Row lock, проверка доступа/версии/epoch |
| InputRepository.append_revision(ctx, guard, revision) | Контекст, guard, InputRevision | Result[InputRevision] | Неизменяемая запись |
| WorkRepository.enqueue_unique(work) | WorkRef | WorkRef | ON CONFLICT по scoped dedupe |
| WorkRepository.claim_next(kind, worker_id, now, lease) | Тип и доступная ёмкость вызывающего worker | ClaimedJob/null | SKIP LOCKED, новый fence_token |
| WorkRepository.renew_lease(claim, now) | ClaimedJob | bool | CAS, не продлевает чужой claim |
| OutboxRepository.begin_send_if_allowed(claim, intent, permit_inputs) | Claim и связанные данные | Result[SendPermit] | Durable sending, до HTTP |
| uow.commit()/rollback() | Текущая UoW | None | Только владелец application/worker операции |

Остальные методы репозиториев перечислены в CONTRACTS §10. Внутренние функции: `map_row_to_dto`, `encrypt_payload_columns`, `apply_optimistic_guard`, `build_partial_ready_index`, `translate_db_error`. Domain не импортирует SQLAlchemy.

- [ ] **DB01** Миграции identity, cases, input revisions/candidates и callback handles. Приёмка: независимость case_revision/dialog_revision, уникальные привязки owner, запрет записи после tombstone.
- [ ] **DB02** Миграции каталогов/пакетов/release/lifecycle/source audit. Приёмка: immutable content и отдельные mutable lifecycle записи; атомарный active pointer.
- [ ] **DB03** Миграции preview/confirmation/manifest/bundle/artifact/upload tokens. Приёмка: owner-scoped ключи и FK; нельзя сослаться на артефакт другого кейса.
- [ ] **DB04** Миграции inbox/jobs/outbox/send attempts/cleanup. Приёмка: unique dedupe, fence/lease, поиск готовых задач, раздельные состояния.
- [ ] **DB05** Реализовать UoW и порты, rollback и необходимые индексы. Приёмка: сбой перед commit не оставляет Case без соответствующего Inbox/Outbox; репозитории сами не коммитят.

Индексы: `(owner_id, case_id)`, активные кейсы владельца; `(category_id, profile_version)`; уникальные source/snapshot/version IDs; unique dedupe; partial ready jobs `(kind,next_attempt_at,priority)`; expiring records; `(case_id,manifest_hash,format,document_kind)` с owner scope. БД не публикуется наружу.

## 6 Модуль max_adapter

Пути: `src/tsr/adapters/max/`. Зависимости: C01–C03, DB01/DB04; данные D10/D11. Входные структуры MAX остаются только в этом модуле.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| receive_webhook(headers, body) | HTTPS запрос | HTTP response | Проверка, atomic inbox+job, ACK после commit |
| normalize_update(verified_update, identity) | Проверенное событие и actor mapping | Result[NormalizedEvent] | Минимизация/дедупликационный ключ |
| render_max_view(view) | ViewModel | MaxMessagePayload | Экранирование, текст и кнопки |
| upload_file(material_permit, stream, metadata) | MaterialPermit, bytes stream, metadata | Result[UploadResult] | Upload до стадии sending; токен хранится через защищённый порт |
| send_view(send_permit, payload) | SendPermit, привязанный текст | TransportResult | Внешний вызов |
| send_material(send_permit, attachment_ref) | SendPermit и owner-scoped токен | TransportResult | Одно вложение конкретному адресату |
| edit_known_message(send_permit, message_id, payload) | Разрешение и известное своё сообщение | TransportResult | Не ищет/угадывает чужой message_id |
| answer_callback(send_permit, callback_id, answer) | Callback из исходного события | TransportResult | Снимает ожидание кнопки/сообщает результат |
| inspect_subscription(now) | Время | SubscriptionHealth | Эксплуатационная проверка |

Внутренние: `verify_webhook_secret`, `validate_payload_size`, `derive_event_key`, `resolve_private_actor`, `classify_max_response`, `classify_network_failure`, `escape_max_text`, `apply_request_limiter`. Явные типы MaxMessagePayload/HTTP response/VerifiedUpdate принадлежат адаптеру, domain их не видит.

- [ ] **MAX01** Проверенный вход и mapping событий; безопасный ответ для групп. Приёмка: неверный секрет не сохраняет событие, дубликат даёт 200 без нового job, недоступная БД не даёт успешный ACK.
- [ ] **MAX02** Отправка текста, кнопок, callback answer, ограничение запросов. Приёмка: payload соответствует актуальному официальному API; token передаётся только предусмотренным заголовком и не логируется.
- [ ] **MAX03** Upload и отправка/повторный доступ к файлам. Приёмка: attachment.not.ready не запускает новый render; токен другого owner не принимается.
- [ ] **MAX04** Таблица ошибок transport confirmed/definitely_rejected/unknown и MessageReceipt/CallbackReceipt. Приёмка: timeout после возможного принятия не объявляется безопасным повтором; известный HTTP ответ и предметный success проверяются вместе; callback answer не требует выдуманного message_id.

Входящие ACK, ответ callback и доставка результата — три разные операции. Нормальный ACK не подтверждает успех пользовательского сценария.

## 7 Модуль cases

Пути: `src/tsr/domain/cases/`, application helpers для доступа. Зависимости C01–C02, DB01, DB03. Чувствительный ввод хранится зашифрованно.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| initialize_input(input_revision_id, ctx, case_id, profile, role, now) | Новые IDs, профиль, подтверждённая роль | InputRevision | Pure начальная ревизия с явными пропусками |
| create_case(ctx, case_id, profile_ref, initial_input, now) | Actor, новый ID, категория/начальный ввод | CaseSnapshot | Pure модель со ссылкой на input; сохраняет application |
| propose_value(case, candidate_id, field_spec, raw_value, now) | Снимок, новый ID, схема поля, ввод | Result[Candidate] | Без изменения InputRevision |
| confirm_value(case, input_revision_id, candidate, candidate_hash, previous_input, now) | Новый ID ревизии и проверенный кандидат | Result[CaseMutation] | Новый immutable InputRevision и revision |
| select_snapshot(case, comparison, quote, snapshot_id) | Связанные результаты | Result[CaseMutation] | Меняет выбор, инвалидирует подтверждение |
| choose_branch(case, branch) | purchase/support | Result[CaseMutation] | Меняет смысл результата |
| invalidate_dependents(case, change) | Значимое изменение | InvalidationPlan | Comparison/preview/confirmation/jobs/history |
| authorize_owned(ctx, record_owner, guard, current) | Контекст и версия | Result[AccessDecision] | Проверка, без IO |
| mark_case_deleted(case, now) | Снимок | CaseDeletionPlan | Новая epoch, tombstone, cancellation IDs |

`CaseMutation={new_case,new_input_revision|null,invalidation:InvalidationPlan}`; `InvalidationPlan={clear_confirmation,expire_handles,mark_bundles_historical,cancel_old_jobs}`; `AccessDecision={allowed,scope}`; `CaseDeletionPlan={new_epoch,access_revoked_at,cancel_pending:true}`. Эти значения не коммитят данные самостоятельно.

Внутренние: `validate_role`, `normalize_field_value`, `compare_candidate_hash`, `bump_semantic_revision`, `bump_dialog_revision`, `check_same_profile`.

- [ ] **CASE01** Создание/чтение/список принадлежащих кейсов. Приёмка: представитель работает в своём кейсе, знания UUID недостаточно для доступа.
- [ ] **CASE02** Кандидаты ввода и подтверждения. Приёмка: старая кнопка первого значения не подтверждает второе, неизвестное сохраняется явно.
- [ ] **CASE03** Выбор предложения/ветки и инвалидирование зависимых результатов. Приёмка: смена цены/комплектации/региона/суммы не оставляет старое подтверждение действующим.
- [ ] **CASE04** Разделение semantic/dialog revision и восстановление прогресса. Приёмка: «Мои материалы» и heartbeat не делают manifest устаревшим.
- [ ] **CASE05** Tombstone/epoch и план очистки. Приёмка: после удаления нет нового доступа, публикации и разрешения отправки; начатый внешний вызов отдельно учтён.

Ошибки: ACCESS_DENIED, NOT_FOUND, STALE_REVISION, STALE_CANDIDATE, CASE_DELETED, INCOMPATIBLE_PROFILE.

## 8 Модуль conversation

Пути: `src/tsr/domain/conversation/`, `src/tsr/application/presenter.py`. Зависимости C01, CASE01–CASE04; данные D09. Модуль не вызывает MAX и не коммитит БД.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| resolve_intent(event, case, resolved_handle, now) | NormalizedEvent, снимок, handle | Result[CommandDraft] | Выбор разрешённого действия |
| next_step(case, completeness, domain_result) | Состояние и результат | StepDecision | Переход сценария |
| build_view(case, domain_result, locale) | Типизированный результат | ViewDraft | Представление с ActionIntent, без handles |
| build_comparison_view(comparison_set) | До трёх предложений | ViewDraft | Отличия и неизвестности по параметрам |
| build_error_view(error, resume_action) | DomainError | ViewDraft | Понятное восстановление |
| bind_action_handles(view_id, draft, handles, expires_at) | ID и сохранённые application handles | ViewModel | Pure привязка для MAX presenter |

`CommandDraft={type,payload,required_guard}`; `StepDecision={step,required_fields[],available_actions[]}`. Настоящий ActorContext и CommandEnvelope добавляет application. Внутренние: `describe_missing`, `paginate_offers`, `compose_supplier_questions`, `bind_message_parameters`, `validate_action_for_step`.

Application присваивает correlation_id ошибке, создаёт view_id и непредсказуемые handles, сохраняет их с owner/guards в той же UoW и вызывает bind_action_handles. Pure conversation не генерирует UUID, не обращается к БД и не доверяет payload кнопки.

- [ ] **CONV01** Карта шагов и разрешённых действий: начало, ввод, сравнение, выбор, ветка, уточнение, preview, подготовка, материалы. Приёмка: нет перехода в документ без подтверждения.
- [ ] **CONV02** Назад/изменить/продолжить/помощь и обработка неподходящего текста. Приёмка: ввод не теряется, известные поля повторно не запрашиваются без причины.
- [ ] **CONV03** Короткие карточки, сравнение двух/трёх предложений, словесные статусы. Приёмка: unknown не скрыт цветом или эмодзи, читаемо на mobile/web.
- [ ] **CONV04** Тексты ожидания/ошибки/неполного/исторического результата. Приёмка: пользователь понимает, что готово и какое действие доступно.

## 9 Модуль catalog

Пути: `src/tsr/domain/catalog/`, `src/tsr/application/catalog_service.py`. Зависимости C01/C02, DB02, D01–D03/D14.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| validate_catalog(pack, profiles, sources) | CatalogPack и связанные данные | ValidationReport | Pure межполевая проверка |
| list_candidates(query, catalog_port) | CatalogQuery | Result[OfferPage] | Read-only обращение к репозиторию |
| get_exact_snapshots(ids, release_ref, catalog_port) | ID и версия release | Result[OfferSnapshot[]] | Возвращает неизменяемые снимки |
| assess_offer_freshness(snapshot, lifecycle, now) | Снимок, статус, время | FreshnessDecision | Pure |
| get_field_evidence(snapshot, field_key, sources) | Поле и реестр | Evidence[] + Source metadata | Для объяснения/UI/document |

Внутренние: `validate_sku_identity`, `validate_field_types`, `resolve_source_refs`, `detect_conflicting_values`, `safe_candidate_filter`.

- [ ] **CAT01** Схемы поставщика/варианта/снимка и provenance. Приёмка: каждое значимое поле связано с источником или unknown.
- [ ] **CAT02** Разрешение точных версий и отбор кандидатов. Приёмка: unknown не отбрасывается фильтром как mismatch, свойства линейки не наследуются автоматически.
- [ ] **CAT03** Свежесть/отзыв/новая цена/история. Приёмка: новый snapshot не переписывает выбранный старый, отозванный не используется как проверенный.
- [ ] **CAT04** Пагинация и короткая подборка. Приёмка: стабильный порядок в фиксированном release, limit ограничен, возвращается next_cursor.

## 10 Модуль matching

Путь: `src/tsr/domain/matching/`. Зависимости C01/C02, D01/D08; входные данные предоставляет application через catalog.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| compare_offer(comparison_id, input_revision, snapshot, profile, algorithm_ref, now) | Новый ID, фиксированные DTO/версии | Result[ComparisonResult] | Pure |
| compare_many(comparison_ids_by_snapshot, input_revision, snapshots, profile, algorithm_ref, now) | IDs от application, до установленного лимита кандидатов | Result[ComparisonResult[]] | Pure |
| rank_comparisons(comparisons, quotes, sort_mode) | Результаты, сортировка | OrderedComparisonRefs | Pure, объяснимое ранжирование |
| supplier_questions(comparison, quote, route_eval) | Неизвестности трёх расчётов | SupplierQuestion[] | Pure |

`OrderedComparisonRefs={comparison_ids[],reason_keys[]}`. Внутренние: `normalize_unit`, `compare_field`, `apply_eq/gte/lte/in_set/within_range`, `aggregate_class`, `deduplicate_questions`.

- [ ] **MATCH01** Сравнение по полям и четырём статусам, проверка профиля. Приёмка: known/mismatch/unknown/unspecified проходят эталоны, неизвестное не равно совпадению.
- [ ] **MATCH02** Полнота, ранжирование и объяснение. Приёмка: дешёвое предложение с неизвестным обязательным параметром не становится подтверждённо подходящим.
- [ ] **MATCH03** Вопросы поставщику и границы единиц/операторов. Приёмка: сравнение воспроизводится по версии, профильная погрешность не появляется из кода без утверждения.

Ошибка некорректной структуры — VALIDATION_ERROR/INCOMPATIBLE_PROFILE. Отсутствие подходящего предложения — нормальный пустой/объяснённый результат.

## 11 Модуль pricing

Путь: `src/tsr/domain/pricing/`. Зависимости C01/C02, D08. Ни API сертификата, ни реестр остатков не вызываются.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| quote_purchase(input_revision, snapshot, algorithm_ref) | Сумма пользователя, цена, доставка, условия | Result[PricingResult] | Pure |
| validate_money_input(raw_text, locale) | Текст поля | Result[Money] | Pure |
| describe_quote(result) | PricingResult | QuoteExplanation | Pure текстовые ключи/параметры |

`QuoteExplanation={lines:message_key/parameters[],warnings[]}`. Внутренние: `calculate_coverage`, `calculate_gap`, `derive_certificate_use`, `derive_delivery_total`, `make_calculation_evidence`.

- [ ] **PRICE01** Парсинг рублей в копейки, отдельный предел ввода и диапазон результата, точность/нулевые значения. Приёмка: отрицательное, NaN, бесконечность и лишние знаки не принимаются; два входа по 100 млн корректно дают итог 200 млн рублей.
- [ ] **PRICE02** Полный и условный расчёт, неизвестная цена/доставка, непринятие и неприменимость ЭС. Приёмка: certificate_applicable=no всегда даёт coverage=0/gap=P при точной P, даже если продавец принимает ЭС; все граничные D08 эталоны и отсутствие ложного общего итога.
- [ ] **PRICE03** Объяснение и calculated provenance. Приёмка: сумма разницы не называется одобренной помощью фонда.

## 12 Модуль routes

Путь: `src/tsr/domain/routes/`; загрузка через application/RouteRepository. Зависимости C01/C02, DB02, D04/D08.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| validate_route_pack(pack, sources, template_registry) | RoutePack и ссылки | ValidationReport | Pure |
| build_route_context(input_revision, snapshot, policy, now) | Подтверждённый ввод, снимок, EvaluationPolicy | RouteContext | Pure mapping, включая route_answers и demo/pilot |
| evaluate_route(context, pack_or_none, lifecycle, now) | RouteContext и точный пакет | Result[RouteEvaluation] | Pure |
| build_checklist(pack, role, answers) | Роль и сведения пользователя | ChecklistItem[] | Pure |
| assess_route_freshness(pack, lifecycle, now) | Версия и состояние | FreshnessDecision | Pure |

Внутренние: `check_region`, `check_category`, `run_registered_predicate`, `aggregate_route_status`, `resolve_checklist_applicability`, `describe_next_steps`. Реестр handler_id фиксируется в коде; отсутствующий handler — ошибка импорта, а не выполнение текста из JSON.

- [ ] **ROUTE01** Пакет региона и фиксированные predicates. Приёмка: только разрешённые условия, прямые source refs, неизвестные сведения не становятся true.
- [ ] **ROUTE02** Четыре результата применимости и чек-лист по роли. Приёмка: статус ветерана не равен одобрению, приём ЭС не равен договору фонда, другой регион → not_covered.
- [ ] **ROUTE03** Неполнота, адресат, срок проверки и отзыв. Приёмка: непроверенный пакет не выдаётся за реальный путь; полноценная ветка требует D04.

## 13 Модуль application

Путь: `src/tsr/application/`. Это единственный оркестратор пользовательских сценариев; он связывает модули через указанные публичные функции и порты. Зависимости: контракты, cases/conversation, нужные доменные модули и DB порты.

| Функция границы | Вход | Выход | Что вызывает и сохраняет |
| --- | --- | --- | --- |
| handle_inbox(claim, ports) | ClaimedJob process_inbox | Result[CommandResult] | Нормализованный вход → conversation → нужный use case; единый commit |
| execute_command(command, ports) | CommandEnvelope | Result[CommandResult] | Owner/guards, dispatch; вызывается только доверенным входом |
| collect_and_confirm_input(command, uow) | propose/confirm candidate | CommandResult | cases + InputRepository + presenter |
| compare_and_select(command, uow) | compare/select | CommandResult | catalog → matching → pricing → cases |
| evaluate_next_step(command, uow) | choose branch/route answers | CommandResult | routes + checklist + next_step |
| prepare_result_preview(ctx, guard, now, uow) | Actor и актуальный кейс | Result[ResultPreview] | Чтение точных версий, расчёты, freshness, hash |
| confirm_and_enqueue(command, uow) | Preview ID/hash | Result[DocumentBundle] | Confirmation+Manifest+Bundle+jobs атомарно |
| publish_render_result(claim, staged, ports) | Claim и ciphertext blob | Result[ArtifactRecord] | Проверки, publish, bundle readiness, уведомление |
| request_material(command, uow) | Artifact ID и current/historical | Result[DeliveryIntent] | Owner, TTL, MaterialPermit, outbox |
| authorize_delivery(claim, intent, uow) | Claim, DeliveryIntent | Result[SendPermit] | Guards, freshness, durable sending |
| delete_owned_case(command, uow) | Подтверждённое удаление | Result[DeleteReceipt] | Tombstone/epoch/cancel/cleanup |

Внутренние: `resolve_actor_from_inbox`, `build_guard_from_handle`, `load_fixed_domain_inputs`, `assert_fresh_preview`, `append_view_outbox`, `check_required_artifacts`, `map_domain_error_to_view`. Транспорт/MAX не вызывается внутри пользовательской UoW.

- [ ] **APP01** Транзакционный dispatcher команд с idempotency и доступом. Приёмка: повтор Inbox не применяет изменения снова; rollback не оставляет частичный результат.
- [ ] **APP02** Создание кейса, ввод, возврат и сохранение. Приёмка: полный диалог переживает рестарт, case/dialog revision не смешаны.
- [ ] **APP03** Сравнение, выбор и покупка. Приёмка: snapshot, comparison и quote относятся к одному input/profile; покупка не создаёт заявление.
- [ ] **APP04** Ветка обращения и preview. Приёмка: пакет/адресат/чек-лист связаны с регионом, UI показывает пропуски и источники.
- [ ] **APP05** Общий для обеих веток preview, подтверждение, manifest, jobs, публикация и выдача. Приёмка: frozen proposed_content включает generator/release; один manifest_hash совпадает в preview и manifest, metadata envelope исключён; старая revision не публикуется, partial bundle не объявлен готовым.
- [ ] **APP06** Материалы, явный повтор доставки и удаление. Приёмка: historical read отделён от regeneration, unknown send требует отдельного действия, deletion epoch блокирует новый доступ.

## 14 Модуль documents

Пути: `src/tsr/domain/documents/`, `src/tsr/adapters/render/`, `templates/`. Pure DOC01/DOC04 зависят только от C01/C02 и точных DTO; renderer — от DocumentModel и D05/D06. APP05/FILE01 нужны для интеграции публикации, domain documents их не импортирует. Данные D05/D06/D08.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| build_document_request(manifest) | Immutable DocumentManifest | DocumentRequest | Pure, состав файлов по ветке |
| build_document_model(manifest, document_kind) | Зафиксированные сведения | Result[DocumentModel] | Pure, единый контент PDF/DOCX |
| validate_document_model(model, template_spec) | Модель и версия шаблона | ValidationReport | Pure, обязательные разделы/пропуски |
| render_document(request) | RenderRequest | Result[RenderedBytes] | Дочерний процесс, только чтение утверждённых шаблонов/шрифта |
| inspect_rendered_output(rendered, expected_model) | Файл и эталон | ValidationReport | QA-проверка содержимого; визуальный QA отдельно |

Внутренние: `build_purchase_sections`, `build_application_sections`, `build_product_card`, `build_checklist_sections`, `mark_missing_fields`, `escape_markup`, `write_pdf`, `write_docx`. Renderer не получает SQL session/секреты и не загружает сетевые ресурсы.

- [ ] **DOC01** Manifest projection и единая DocumentModel. Приёмка: поля из изменённого Case не подмешиваются, все версии зафиксированы.
- [ ] **DOC02** Карточка покупки PDF с ценой, доставкой, источниками и вопросами. Приёмка: неизвестное отображено, заявления нет.
- [ ] **DOC03** ReportLab и python-docx render, кириллица/длинные поля/лимит bytes. Приёмка: PDF/DOCX совпадают по данным и не содержат исполняемой пользовательской разметки.
- [ ] **DOC04** Bundle/artifact spec и неполный результат. Приёмка: ready только при наличии всех обязательных артефактов; пропуски видны и в файле, и в чате.
- [ ] **DOC05** Заявление DOCX+PDF, карточка ТСР PDF и checklist PDF для D04. Приёмка: approved template, правильный адресат/роль, отсутствие медицинских заключений.

## 15 Модуль private files и crypto

Путь: `src/tsr/adapters/files/`, `src/tsr/adapters/crypto/`. Зависимости C03, DB03, D11/D13.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| stage_encrypted(rendered, claim, manifest_ref) | RenderedBytes и claim | Result[StagedArtifact] | Шифрует и атомарно записывает приватный blob |
| read_authorized_artifact(permit, record) | MaterialPermit, ArtifactRecord | Result[bounded byte stream] | Проверяет разрешение, читает/расшифровывает |
| remove_blob(blob_ref) | Серверная ссылка | DeleteReport | Идемпотентное удаление |
| purge_orphans(active_refs, active_claims, now) | Ссылки/claims и время | DeleteReport | Удаляет только неиспользуемые просроченные blobs |
| encrypt/decrypt(payload, key_id) | Bytes, разрешённый ключ | EncryptedBlob/bytes | Проверенная криптобиблиотека |

Внутренние: `safe_server_path`, `atomic_ciphertext_write`, `verify_plaintext_hash`, `enforce_size_limit`, `validate_key_metadata`. Ключи не передаются в render process. Blob сам по себе не доступен пользователю без опубликованного ArtifactRecord.

- [ ] **FILE01** Приватное хранение и шифрование, server-generated имена. Приёмка: ciphertext на диске, нет публичной static раздачи и directory traversal.
- [ ] **FILE02** Авторизованное чтение/TTL/исторические материалы. Приёмка: чужой UUID/attachment token не раскрывает файл, expired не запускает старую revision.
- [ ] **FILE03** Orphan cleanup, удаление и восстановление файловых ссылок. Приёмка: rollback оставляет недоступный orphan, cleanup не удаляет активный render, опубликованная запись не ссылается на незаписанный файл.

## 16 Worker и его обработчики

Пути: `src/tsr/worker/dispatcher.py`, `handlers.py`, `recovery.py`. Зависимости DB04/DB05, application, documents, private files, MAX. Один worker — роль того же образа; дочерний render process ограничен одним slot в начальной конфигурации.

| Функция границы | Вход | Выход | Вызовы и эффект |
| --- | --- | --- | --- |
| run_worker(settings, ports) | Конфигурация/порты | Процесс до shutdown | Dispatcher, heartbeat, handlers, recovery |
| claim_available(kind, capacity, now) | Реально свободная ёмкость | ClaimedJob/null | WorkRepository claim |
| process_inbox_job(claim) | process_inbox claim | Result[CommandResult] | Application.handle_inbox |
| render_artifact_job(claim) | render claim | Result[ArtifactRecord] | Load manifest → render slot → stage → application.publish |
| deliver_outbox_job(claim) | delivery claim | TransportResult | Prepare/upload → authorize_send → MAX → CAS outcome |
| heartbeat_claim(claim, now) | Текущий claim | bool | Условное продление |
| classify_retry(work_kind, outcome) | Вид и исход | RetryDecision | safe/none/explicit_user и next_attempt |
| recover_expired_work(now) | Время | RecoveryReport | Безопасные повторы, sending→unknown |
| shutdown_gracefully(deadline) | Время окончания | ShutdownReport | Прекращает новые claims, завершает/отпускает допустимые |

`RetryDecision={mode:none|safe|explicit_user,next_attempt_at|null,reason_code}`; `ShutdownReport={finished_ids[],released_safe_ids[],unknown_send_ids[]}`. Обработчики maintenance запускаются расписанием worker/операционными CLI командами, не создают четвёртую платформу очередей.

- [ ] **W01** Dispatcher трёх фиксированных видов задач и квоты. Приёмка: короткие действия не ждут PDF, стареющие документы не голодают, rate limit общий для исходящего транспорта.
- [ ] **W02** Claim/lease/fencing и heartbeat. Приёмка: старый fence не продлевает/не завершает/не публикует; render claim берётся только при свободном slot.
- [ ] **W03** Inbox handler и атомарный переход processed. Приёмка: crash до/после commit не применяет команду дважды.
- [ ] **W04** Render handler с bounded ProcessPool и IPC: `ProcessPoolExecutor(mp_context=multiprocessing.get_context("spawn"), max_workers=1)`. Render entrypoint не загружает Settings/ORM/секреты при импорте. Приёмка: дочернему процессу не переданы сессии/ключи/токены, отсутствует наследование памяти через fork, максимальный файл ограничен, отзыв/expiry/revision проверяются до работы и публикации. Это ограничение ресурсов в доверенном контейнере, не отдельный security sandbox.
- [ ] **W05** Публикация артефактов и завершение bundle. Приёмка: последний обязательный файл создаёт ровно одно локальное уведомление по unique key; старый render удаляется как orphan.
- [ ] **W06** Delivery handler, upload tokens, отправка/редактирование/callback answer. Приёмка: durable sending до HTTP, актуальный owner/guard, definite failure отличается от unknown.
- [ ] **W07** Recovery/ограниченные retries/backoff. Приёмка: expired render можно перехватить с новым fence; recovered sending не переотправляется автоматически; повторы конечны.
- [ ] **W08** Гонки удаления, отзыва пакета и редактирования. Приёмка: tombstone запрещает новую публикацию и send permit; ранее начатая внешняя отправка отражена в статусе.
- [ ] **W09** Graceful shutdown, capacity и эксплуатационные метрики. Приёмка: процесс не теряет принятые задания, корректно отличает queue delay/compute/delivery delay.

Для MVP предлагаются max attempts=5 для безопасно повторяемых внутренних/определённо отвергнутых операций, lease=60 с и heartbeat=15 с, настраиваемые значения. Они проходят тесты на целевом хосте и не объявляются лимитами MAX. Network timeouts задаются явно; неопределённая отправка не попадает в safe retry независимо от attempts.

## 17 Модуль operations

Пути: `src/tsr/operations/`, `docs/runbook.md`, `deploy/`. Зависимости DB/worker/Files, D01–D14.

| Функция границы | Вход | Выход | Эффект |
| --- | --- | --- | --- |
| validate_release(root, manifest) | Разрешённые файлы и registry | ImportReport | Dry-run, без записи |
| import_release(root, manifest, actor_key) | Валидный пакет | ImportReport | Staging, сохраняет immutable content |
| activate_release(ref, actor_key, mode, now) | VersionRef и политика | Result[ActivationReceipt] | Атомарно переключает active pointer |
| revoke_package(ref, reason, actor_key, now) | Пакет и основание | LifecycleReceipt | Отзыв в lifecycle и аудит |
| collect_health(now) | Время | HealthSnapshot | Минимальная диагностика |
| run_retention(now, policy) | Время/сроки | DeleteReport | Очистка payload, кандидатов, файлов и удалённых кейсов |
| create_backup(destination, policy) | Защищённое место | BackupReceipt | Согласованные шифрованные данные |
| restore_backup(backup_ref, deletion_log) | Backup и журнал удалений | RestoreReport | До открытия пользовательского доступа |

`LifecycleReceipt={ref,status,changed_at,actor_key}`; `BackupReceipt={backup_id,created_at,encrypted_location,manifest_hash}`; `RestoreReport={restored_version,consistency_errors[],deletions_applied,ready}`. Внутренние: `safe_load_json_yaml`, `verify_hashes`, `resolve_pack_dependencies`, `check_review_policy`, `redact_logs`, `sweep_expired_handles`, `check_backup_consistency`.

- [ ] **OPS01** CLI validate/import/activate/rollback/revoke с аудитом и полной семантической матрицей CONTRACTS §11. Приёмка: проверены kinds/units/refs/review/assets/expected, ошибки имеют путь поля; нет частичной активации и обхода reviewed/expiry при rollback.
- [ ] **OPS02** Health/ready, heartbeat, queue age, ошибки MAX/пакетов, подписка и секреты. Приёмка: логи не содержат ПД; alert показывает безопасный correlation ID.
- [ ] **OPS03** Retention и удаление по policy. Приёмка: очистка идемпотентна, FK не мешают отделить удалённый payload от минимального dedupe.
- [ ] **OPS04** Backup/restore, runbook и проверки размещения. Приёмка: применяются tombstones, файлы согласованы с БД, пользовательский доступ до проверки закрыт.

## 18 Сквозная проверка и задачи сдачи

- [ ] **QA01** Unit и contract проверки D08 для matching/pricing/routes: unknown, conflict, границы, неподдерживаемый регион, типы и версии. Зависимости C/MATCH/PRICE/ROUTE; результат — эталонные проверки, не процент покрытия ради числа.
- [ ] **QA02** Интеграционные транзакции и аварийные точки: lease истёк во время render; MAX принял сообщение перед crash; delete между blob write и publish; старые candidate/preview handles. Зависимости DB/APP/W; результат — сохранённые инварианты.
- [ ] **QA03** Две ветки end-to-end на mobile/web MAX, включая файлы, возврат, исправление, отсутствие подходящих вариантов и повторный вход. Зависимости все F01–F18 и D11; результат — протокол двух клиентов с ожидаемыми материалами.
- [ ] **QA04** Доступ/сохранение/удаление: чужие IDs/handles, expired blob, восстановление backup, запрещённые payload и отсутствие секретов в Git/логах. Зависимости CASE/FILE/OPS; результат — отсутствие утечек и возрождения удалённого кейса.
- [ ] **QA05** Steady/burst нагрузка по принятой архитектуре: 5 событий/с, 50 активных диалогов, 1000 снимков; отдельно 100 документов. Зависимости W/APP; результат — p95/queue/completion/error metrics и фактическая ёмкость. Нагрузку MAX моделируем, живую доставку проверяем отдельно.
- [ ] **QA06** Сравнительный пользовательский тест: эталон, обычный путь/бот, чередование порядка, время и доля корректного завершения, ошибки и незавершённые попытки. Зависимости D08/CONV/QA03; результат — наблюдения без неподтверждённого процента улучшения.
- [ ] **REL01** Dockerfile, Compose, lock, `.dockerignore`, `.env.example`, миграции/seed. Приёмка: одна команда запуска, повторный запуск сохраняет данные, полная сборка ≤5 минут после загрузки базового образа.
- [ ] **REL02** README, архитектурная схема, API OpenAPI 3.1 + DATA-API.yaml для технических endpoints, тестовые данные/доступы, PDF-презентация и закрытый служебный слайд. Приёмка: всё соответствует фактическому commit, нет рабочих секретов в публичных файлах.
- [ ] **REL03** Размещение, живая smoke проверка, release tag/commit/hash, план доступности на проверку. Приёмка: бот и обе ветки доступны; переданная версия зафиксирована, незавершённое явно обозначено.

HTTP endpoints первого релиза: `POST /webhooks/max`, `GET /health/live`, защищённый `GET /health/ready`. API комплект — добровольное консервативное выполнение условных требований, не новая пользовательская функция. Админ API и download по публичному ID не создаются.

## 19 Трассировка функций и требований

| Функции архитектуры | Задачи | Данные и доказательство |
| --- | --- | --- |
| F01 начало/роль/режим | CASE01, CONV01, APP02 | D07/D09, QA03 |
| F02 ввод | CASE02, APP02, PRICE01 | D01/D07, QA01/03 |
| F03 пояснение/unknown/правка | CASE02/04, CONV02 | D09, QA03 |
| F04 комплектация | CAT01/02, APP03 | D02/D03 |
| F05 сравнение | MATCH01/02 | D01/D08, QA01 |
| F06 деньги | PRICE01–03 | D02/D08 |
| F07 источники/даты | CAT01/03, DOC01 | D03 |
| F08 вопросы | MATCH03, CONV03 | D09 |
| F09 сохранение выбора | CASE03, APP03, DB01 | QA02/03 |
| F10 покупка | APP03/05, DOC02 | D02/D05, QA03 |
| F11 маршрут | ROUTE01–03, APP04 | D04/D08 |
| F12 документы обращения | APP04/05, DOC05 | D04/D05, QA03 |
| F13 подтверждение | CASE02, APP05, DOC01 | QA02 |
| F14 скачивание | FILE01/02, W04–06, MAX03 | D05/D06, QA03 |
| F15 продолжение/материалы | CASE04, CONV02, APP06 | QA03 |
| F16 ошибки/восстановление | MAX04, W02/07/09, OPS04 | QA02 |
| F17 удаление | CASE05, APP06, W08, OPS03 | D13, QA04 |
| F18 два клиента | CONV03/04, QA03 | D09/D11 |
| F19 данные/публикация | CAT01–04, OPS01 | D01–D06/D14 |
| F20 польза | QA06 | D08, протокол теста |

Обязательные ограничения T01–T11 принятой архитектуры закрываются темой/двумя ветками, QA03, D03/лицензиями, C03/QA04, D12/D13, REL01–03. Оценочные критерии: ценность/UX — CONV+QA06; масштабирование — точные профили/RoutePack и тест добавления второго fixture профиля без правки ядра; интеграция — APP/DB/MAX; стабильность — W/QA02; безопасность — CASE/FILE/QA04; комплектность — REL. Технологические меры не заменяют продуктовую проверку или презентацию.

## 20 Условия завершения MVP

- [ ] Проверенные реальные снимки и RoutePack подготовлены, либо ограничение/модельность показаны одинаково в продукте и материалах; модель не объявлена реальной интеграцией.
- [ ] Пользователь завершает обе ветки от начала до скачиваемого результата в двух клиентах MAX.
- [ ] Unknown/conflict не превращаются в подтверждённое соответствие, бесплатную доставку или одобрение фонда.
- [ ] Версии, подтверждения, повторы, права и удаление проходят аварийные проверки.
- [ ] Нет блокировки диалога длинным render; ёмкость подтверждена измерениями либо честно ограничена.
- [ ] Docker, версии зависимостей, README, API комплект, презентация и доступный бот соответствуют одному release.

Разделы с рабочими токенами, реальными пользовательскими данными и материалами пилота заполняются в предусмотренном защищённом месте. Они не добавляются в examples или открытый репозиторий.
