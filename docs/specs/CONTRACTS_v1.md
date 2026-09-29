# Контракты модулей помощника по ТСР

Версия 1.0.0 от 30 сентября 2026 года. Основание — принятая ARCHITECTURE_v1.md. Это спецификация будущей реализации, не готовое приложение. Публичный интерфейс модуля означает Python-функцию или Protocol внутри монолита; он не означает новый HTTP API.

## 1 Общие правила

- На границах модулей передаются именованные неизменяемые DTO. ORM-сущности, HTTP request, SQL session, произвольные dict и глобальные переменные не являются межмодульными контрактами.
- Обмен внутри процесса — вызов функции. Фоновая работа — сохранённая запись в PostgreSQL со ссылкой на неизменяемые данные. Общего event bus и брокера нет.
- Даты — UTC RFC 3339, например `2026-09-29T21:41:29Z`; пользовательское представление может учитывать часовой пояс. В pure функции передаётся `now`, внутри них нет чтения часов.
- ID сущностей — UUID. ID профилей, полей, правил и поставщиков — стабильные ASCII ключи. Номер схемы — SemVer, первая схема `1.0.0`; версия пакета и версия схемы различаются.
- Деньги — `Money{currency: RUB, minor: int}`, целые копейки, представимый диапазон 0…9 000 000 000 000 000, в PostgreSQL BIGINT. Отдельный бизнес-предел каждого входного значения цены, сертификата и доставки — 10 000 000 000 копеек (100 млн рублей). Результат сложения не ограничивается пределом одного ввода: gap=100 млн и доставка=100 млн дают total=200 млн рублей. Арифметика целочисленная с проверкой диапазона, overflow → VALIDATION_ERROR без обрезания. Ввод более двух десятичных знаков отклоняется с пояснением; лишние знаки не округляются молча. Физические значения сериализуются десятичной строкой и имеют единицу.
- `null` допустим только в явно nullable поле. Он не равен нулю, false, пустой строке или подтверждённому отсутствию условия. Пустой список означает отсутствие элементов, а не неизвестность.
- Строки нормализуются в Unicode NFC; ключи сериализуемых объектов сортируются при хешировании; JSON — UTF-8, без NaN/Infinity. Массивы с порядком сохраняют его, множества сортируются по стабильному ключу. Хеши — SHA-256 канонического JSON; manifest не содержит собственный hash в хешируемой части.
- Схемы DTO владеет `contracts`. Доменные DTO не знают о БД или MAX. Изменение обязательного поля/смысла enum требует новой версии и миграции сохранённых записей. Неизвестная major-версия отклоняется, не интерпретируется приблизительно.

## 2 Владение, версии и ошибки

| Тип | Поля | Правила |
| --- | --- | --- |
| ActorContext | owner_id: UUID; delivery_target_id: UUID; bot_scope: str; case_mode: demo/pilot; correlation_id: UUID | Создаётся доверенным адаптером после проверки события, не принимается из пользовательского JSON/кнопки |
| CaseGuard | case_id: UUID; expected_revision: int; expected_deletion_epoch: int | Проверка в транзакции под блокировкой кейса; owner приходит отдельно из ActorContext |
| DialogGuard | CaseGuard; expected_dialog_revision: int | Дополнительно защищает вопрос, навигацию и кнопки |
| VersionRef | id: str; version: SemVer | Ссылка на неизменяемый профиль, маршрут, алгоритм или шаблон |
| DomainError | code: enum; field_key: str/null; retryability: none/safe/explicit_user; safe_message_key: str; correlation_id: UUID/null | Pure функция оставляет null; application присваивает correlation_id исходного ActorContext до логирования/показа. Не содержит SQL, payload, токены, имена файлов или ПД |
| Result[T] | ok: true + value:T либо ok:false + error:DomainError | Успех и ошибка взаимоисключающие; ожидаемые ошибки не маскируются исключением |

`case_revision` меняется только при изменении значимых пользовательских сведений, выбранного предложения или ветки. `dialog_revision` меняется при замене кандидата, вопроса или экрана. Создание задания, heartbeat, доставка, навигация и само подтверждение результата не увеличивают case_revision. Это предотвращает отмену manifest только из-за перехода на экран «Готовим файлы».

`InputRevision` имеет собственный неизменяемый UUID и предыдущий UUID. Несколько case_revision могут ссылаться на один InputRevision, например при смене предложения. `deletion_epoch` монотонно увеличивается при удалении. Новые задания сохраняют конкретные revision/epoch; права доступа при выполнении проверяются заново.

Каталог, профиль и RoutePack имеют независимые версии. Данные содержимого неизменяемы; отзыв, срок проверки и активный указатель находятся в отдельном контролируемом lifecycle record. Отзыв не редактирует незаметно ранее посчитанный hash.

Единые коды ожидаемых ошибок: `VALIDATION_ERROR`, `ACCESS_DENIED`, `NOT_FOUND`, `STALE_REVISION`, `STALE_CANDIDATE`, `CASE_DELETED`, `DATA_NOT_READY`, `DATA_REVOKED`, `DATA_EXPIRED`, `UNSUPPORTED_SCHEMA`, `INCOMPATIBLE_PROFILE`, `INVALID_TRANSITION`, `LEASE_LOST`, `RATE_LIMITED`, `TEMPORARY_FAILURE`, `PERMANENT_FAILURE`, `DELIVERY_UNKNOWN`, `ARTIFACT_EXPIRED`. Неизвестный параметр изделия и отсутствие маршрута — нормальные предметные результаты, не ошибки сервера. Для чужого объекта клиент получает одинаковое безопасное «недоступно», без раскрытия существования объекта.

## 3 Значения и происхождение

`TypedValue` — объединение именованных wrapper DTO: `QuantityValue={kind:quantity,value:decimal-string,unit:mm|kg}`, `BooleanValue={kind:boolean,value:bool}`, `CodeValue={kind:code,value:key}`, `TextValue={kind:text,value:string}`, `MoneyValue={kind:money,value:Money}`. `RequirementValue` дополнительно допускает range `{kind:range,minimum,maximum,unit}` и set `{kind:set,values:TypedValue[]}`. Подготовленный JSON и runtime используют одинаковые wrapper DTO, importer не снимает обёртку. Например, цена в known Fact читается как `fact.value.value.minor`, boolean — `fact.value.value`; предпочтительны типизированные accessor helpers. Допустимые варианты и оператор определяются профилем поля, не произвольным выбором пользователя.

`Fact[T]` имеет три взаимоисключающих варианта:

| Состояние | Содержание |
| --- | --- |
| known | value:T; evidence: непустой список Evidence |
| unknown | value:null; reason_code; evidence: список, может быть пустым |
| conflicting | value:null; alternatives: не менее двух known вариантов со своими evidence; reason_code |

`Evidence` — source `{source_id, locator, observed_at}`; user `{input_revision_id, field_key, confirmed_at}`; calculated `{algorithm:VersionRef, input_refs}`. URL есть в реестре источников только для source. Подтверждение ответа пользователем подтверждает ввод, не независимую достоверность государственного реестра.

Новые UUID/непредсказуемые handles получает только application через IdGenerator и передаёт pure factories явно: case_id, candidate_id, input_revision_id, comparison_id, view_id. Pure функции не читают часы, не используют random и не создают correlation ID. IDs уже существующих сущностей приходят во входных DTO.

Схемы подготовленных файлов находятся в `schemas/mvp-data.schema.json`, в `$defs`. Для известного типа файла валидация выбирает конкретное определение, например `#/$defs/CatalogPack`; корневая схема также ограничена объединением восьми видов пакетов. JSON Schema проверяет форму; перечисленные ниже межполевые/ссылочные ограничения проверяет importer. Внутренние runtime DTO описаны в этом документе; их Pydantic модели реализуются задачей C01, готового runtime кода в комплекте нет.

## 4 Каталог и требования пользователя

| DTO | Поля и вложенные модели |
| --- | --- |
| CategoryProfile | schema_version, profile_id/version, category_id, data_kind, review, fields: AttributeRule[] |
| AttributeRule | field_key, label, value_kind, unit/null, operator, required_for_complete_comparison, allowed_codes, tolerance/null, help_text, source_ids |
| InputRevision | input_revision_id, owner_id, case_id, created_at, previous_id/null, category_id, profile_ref, region_code:string/null, role:self/representative, prescribed:map[field,RequirementValue], unspecified_fields[], certificate_amount:Money/null, certificate_applicable_declared:yes/no/unknown, route_answers:map[key,yes/no/unknown], document_fields:map[key,string], confirmations:UserEvidence[] |
| Variant | variant_id, category_id, manufacturer, model, modification, configuration |
| Supplier | supplier_id, display_name, contacts:Fact[TextValue][], source_ids[] |
| OfferSnapshot | snapshot_id, offer_id, supplier_id, seller_sku, Variant, profile_ref, data_kind, observed_at, review, attributes:map[field,Fact[TypedValue]], price:Fact[MoneyValue], price_kind:exact/from/unknown, Delivery, accepts_certificate:Fact[BooleanValue], has_fund_contract:Fact[BooleanValue], availability:Fact[CodeValue], source_ids[] |
| Delivery | mode:included/separate/unknown/conflicting; charge:Fact[MoneyValue]; terms:Fact[TextValue] |
| CatalogQuery | category_id, profile_ref, region_code, cursor/null, limit≤20, include_incomplete:true | 
| OfferPage | items:OfferSnapshot[], next_cursor/null, active_catalog_ref, data_quality_flags[] |
| CaseSnapshot | case_id, owner_id, mode, category_id, profile_ref, case_revision, dialog_revision, deletion_epoch, status:active/completed/deleting/deleted, step:start/input/comparison/selection/branch/route/review/preparing/materials, input_revision_id, selected_snapshot_id/null, branch:null/purchase/support, active_confirmation_id/null, last_activity_at |
| Candidate | candidate_id, owner_id, case_id, field_key, value:RequirementValue либо отдельное unknown, value_hash, CaseGuard, dialog_revision, created_at, expires_at |

Обязательные правила importer: имена полей уникальны; единица и value_kind согласованы; range.minimum≤maximum; элементы set совместимы; operator допустим для типа; tolerance применяется только при утверждённом правиле. `eq` сравнивает нормализованные значения, `gte` означает значение предложения ≥ требуемого, `lte` — ≤, `within_range` — значение предложения внутри назначенного диапазона, `in_set` — одно из назначенных допустимых значений. Точная медицинская применимость оператора утверждается экспертом отдельно.

Свойства конкретной комплектации нельзя заполнять свойствами другой SKU. price=known требует TypedValue.kind=money; два поля условий продавца — boolean; quantity имеет единицу профиля. included означает подтверждённое включение доставки и известный нулевой дополнительный charge; неизвестная доставка требует unknown charge. `price_kind=from` не используется как точная цена комплектации. supplier_id, source_id и profile_ref должны разрешаться внутри публикуемого release. `prescribed` и `unspecified_fields` не пересекаются.

Начало кейса получает выбранную роль и exact CategoryProfile (payload category_ref — VersionRef этого профиля). `initialize_input` создаёт первую ревизию: category_id/profile_ref из профиля, role из подтверждённого выбора, region_code=null, суммы=null, применимость=unknown, maps/lists пустые. Подтверждение выбора роли фиксируется UserEvidence. Application создаёт CaseSnapshot с ссылкой на эту InputRevision и сохраняет обе записи одной транзакцией. Поле, отсутствующее и в prescribed, и в unspecified_fields, ещё не отвечено; unspecified_fields означает явный ответ «не указано». При сравнении оба случая дают unspecified_input, а диалог отличает необходимость вопроса. Перенос DemoProfile также заполняет category_id из проверенного профиля.

CatalogQuery выполняет безопасный отбор по категории и подтверждённым ограничениям. Unknown по характеристике, доставке или региональным условиям не исключается SQL фильтром как доказанное несоответствие. Сравнение применяется после загрузки кандидатов.

## 5 Предметные результаты

| DTO | Поля |
| --- | --- |
| FieldMatch | field_key, status:match/mismatch/unknown_offer/unspecified_input, required_for_complete, required_value/null, offer_fact:Fact, reason_code, evidence_refs[], supplier_question/null |
| ComparisonResult | comparison_id, input_revision_id, snapshot_id, profile_ref, matching_algorithm_ref, computed_at, fields:FieldMatch[], class:complete/incomplete/mismatch, questions:SupplierQuestion[] |
| SupplierQuestion | question_key, field_key/null, text_key, source_refs[], category:parameter/price/delivery/certificate/fund_contract/availability |
| PricingResult | pricing_algorithm_ref, input_revision_id, snapshot_id, price:Money/null, certificate_limit:Money/null, certificate_use:allowed_by_declared_data/conditional/not_accepted/not_applicable/unknown, coverage:Money/null, gap:Money/null, delivery:Delivery, possible_own_total:Money/null, status:calculated/conditional/incomplete, reasons[], assumptions[], provenance:CalculatedEvidence |
| RouteContext | region_code:string/null, category_id, role, applicant_status_declared:yes/no/unknown, certificate_applicable_declared:yes/no/unknown, additional_answers:map[key,yes/no/unknown], supplier_accepts_certificate:Fact[BooleanValue], supplier_has_fund_contract:Fact[BooleanValue], policy:EvaluationPolicy, now |
| ConditionResult | condition_id, status:met/not_met/unknown, evidence_refs[], reason_code |
| RouteEvaluation | route_ref/null, status:preliminary_match/blocked/needs_clarification/not_covered, conditions:ConditionResult[], checklist:ChecklistItem[], addressee:Fact[TextValue], next_steps[], missing_fields[], evaluated_at |
| ChecklistItem | item_id, label, required_when:always/if_available/representative, source_ids[]; в результате applicability:required/optional/not_applicable, user_status:provided/missing/unknown |
| FreshnessDecision | decision:current/historical_only/blocked; reasons[]; checked_versions[]; checked_at |

Порядок ComparisonResult.class: любое известное mismatch → mismatch; иначе unknown/unspecified по обязательному полю → incomplete; иначе complete. Необязательные unknown отображаются, но не изменяют полноту обязательного сравнения. При пустом наборе назначенных обязательных значений результат incomplete. Процент медицинской пригодности отсутствует.

PricingResult не смешивает отсутствие суммы и ноль. Если цена точная и сумма известна, условный расчёт `coverage=min(P,C)`, `gap=max(P-C,0)` допустим только с явно обозначенной применимостью. При certificate_applicable_declared=no ставим not_applicable, coverage=0 и gap=P независимо от суммы и приёма продавцом. При известном отказе продавца принимать сертификат — not_accepted, coverage=0 и gap=P; оба отрицательных основания сохраняются в reasons. Отрицательное условие имеет приоритет над unknown другого условия. При неподтверждённом условии и отсутствии отрицательного выводится conditional, если известны P и C; иначе incomplete. Unknown доставка даёт total=null. Included даёт total=gap. Separate: сумма gap+charge допускается только как явно условный расход при оплате доставки пользователем; это предположение хранится в assumptions, не утверждение о покрытии доставки сертификатом. Для цены «от» price/coverage/gap/total результата null, исходный Fact остаётся видимым.

`EvaluationPolicy={case_mode:demo|pilot,allow_synthetic_draft:bool}` задаётся конфигурацией сервера, не кнопкой/JSON пользователя. `build_route_context(input,snapshot,policy,now)` переносит region_code/category_id/role и certificate_applicable_declared из InputRevision; applicant_status_declared берёт из `input.route_answers["applicant_status_declared"]` либо unknown; оставшиеся подтверждённые ответы — additional_answers. Два условия продавца берутся из OfferSnapshot. При загрузке DemoProfile его отдельное applicant_status_declared преобразуется в тот же ключ route_answers; демонстрационная модель не является отдельным runtime контрактом. Неизвестный регион требует уточнения и не трактуется как другой регион/not_covered; application запрашивает его до поиска регионального пакета.

Предикаты RoutePack — фиксированные handler_id в коде, не исполняемые выражения. Сначала region/category: несовпадение → not_covered. В применимом пакете любое not_met обязательного условия → blocked; иначе unknown обязательного условия или неизвестный адресат → needs_clarification; иначе preliminary_match. Даже preliminary_match не означает одобрение. В pilot draft/revoked/expired дают DATA_NOT_READY/DATA_REVOKED/DATA_EXPIRED. Только synthetic draft при case_mode=demo и allow_synthetic_draft=true может исполняться как явно маркированная модель. Флаг не разрешает revoked пакет, просроченные проверенные реальные сведения или непроверенный public_snapshot. «Неполный черновик» допускается при неизвестностях проверенного маршрута; он не заменяет отсутствующий проверенный маршрут произвольным заявлением.

## 6 Команды и UI

`CommandEnvelope` содержит command_id, inbox_id/null, ActorContext, CaseGuard/null, DialogGuard/null, type и строго типизированный payload. У команды нет произвольного owner или chat_id в payload.

| type | payload | Результат application |
| --- | --- | --- |
| start_case | category_ref, requested_role | CaseSnapshot + следующий ViewModel |
| propose_field | field_key, raw_text либо unknown | Candidate + ViewModel подтверждения |
| confirm_candidate | candidate_id, candidate_hash | Новый InputRevision, CaseSnapshot, ViewModel |
| compare_offers | snapshot_ids: UUID[] от 1 до 3 | ComparisonSet + ViewModel |
| select_offer | snapshot_id, comparison_id | Обновлённый CaseSnapshot |
| choose_branch | purchase/support | CaseSnapshot и вопросы маршрута/проверки |
| answer_route | field_key, yes/no/unknown | Через Candidate и confirm_candidate, без обхода подтверждения значимого ввода |
| confirm_result | preview_id, manifest_hash | Confirmation + DocumentBundle + задания |
| request_material | artifact_id, disposition:current/historical | DeliveryIntent либо ARTIFACT_EXPIRED |
| retry_delivery | outbox_id, acknowledged_possible_duplicate:bool | Новый send_attempt или новая явная DeliveryIntent |
| navigate | back/resume/materials/help | ViewModel, без изменения case_revision |
| delete_case | confirmation_handle | DeleteReceipt после tombstone, затем очистка |

`ComparisonSet={items:ComparisonResult[], quotes:PricingResult[], case_guard}`. `CommandResult={case:CaseSnapshot|null, view:ViewModel, created_bundle_id:UUID|null, delivery_state:null|queued|delivery_unknown}` — это итог для orchestration; отправка ViewModel ставится в outbox внутри той же транзакции.

`ViewDraft={kind,title_key,sections:ViewSection[],actions:ActionIntent[],case_guard|null,dialog_revision|null,flags[]}` — pure результат conversation. `ActionIntent={action_key,label_key,command_type,typed_payload,required_guard}` содержит серверное описание разрешённого действия. Application получает UUID/непредсказуемые handles через IdGenerator, сохраняет CallbackHandle и привязывает их к draft. `ViewModel={view_id, kind, title_key, sections:ViewSection[], actions:ActionSpec[], case_guard|null, dialog_revision|null, flags[]}` — готовый результат привязки. `ViewSection={kind:text|offer|comparison|status|material, text_key, parameters:typed safe map}`. `ActionSpec={label_key, action_handle, expires_at}`. Только MAX presenter превращает ViewModel в платформенную разметку и экранирует. Запись CallbackHandle хранит owner, case, revision, dialog_revision, candidate/preview/artifact IDs, действие и expiry. В пользовательском payload — только непрозрачный handle.

`NormalizedEvent={inbox_id, bot_scope, dedupe_key, owner_id, delivery_target_id, kind:start|text|callback|ignored, occurred_at, received_at, payload:TextEvent|CallbackEvent|StartEvent}`. TextEvent содержит text и reply_to_message_id/null; CallbackEvent — platform_callback_id и action_handle; StartEvent — разрешённый start payload. Неподдерживаемые события очищаются от лишнего payload и помечаются ignored. Сырые платформенные события в предметные модули не передаются.

## 7 Preview, подтверждение и документы

`ResultPreview={preview_id,owner_id,case_guard,dialog_revision,proposed_content:ManifestContent,manifest_hash,freshness,expires_at}`. Preview неизменяемый; manifest_hash вычислен по полному frozen proposed_content и используется в кнопке без отдельного неоднозначного preview_hash. Все показываемые поля извлекаются из proposed_content, включая generator_ref и release_commit; текущий deployment не подмешивается при подтверждении. Новая цена, маршрут, шаблон, алгоритм, генератор или ввод требуют нового preview для нового актуального результата. Если исходная версия генератора после deployment недоступна, выдаётся DATA_NOT_READY и новый preview, без молчаливой замены.

`Confirmation={confirmation_id, owner_id, case_id, case_revision, deletion_epoch, preview_id, manifest_hash, confirmed_at, confirmed_missing_fields[]}`. Она подтверждает показанный набор, а не все будущие данные пользователя.

`ManifestContent` содержит:

1. schema_version; owner_id; case_id; case_revision; deletion_epoch; case_mode.
2. Канонический InputRevision и пользовательские подтверждения, конкретный OfferSnapshot и разрешённые source metadata для документа.
3. ComparisonResult, PricingResult, RouteEvaluation/null и branch.
4. category_profile_ref; matching_algorithm_ref; pricing_algorithm_ref; route_ref/null; template_refs[]; generator_ref; release_commit; missing_fields[]; flags[].

`DocumentManifest={manifest_id,manifest_hash,created_at,confirmation_id,content:ManifestContent}`. Hash вычисляется только по canonical_bytes(ManifestContent); metadata envelope не участвует. Идентификаторы подтверждения/manifest добавляются и связываются в одной транзакции. ManifestContent включает owner/case/revision/epoch, mode, все пользовательские значения и evidence, снимок, результаты и версии. При подтверждении content копируется из preview без пересчёта полей, hash перепроверяется. Computed_at уже зафиксирован preview. Рендер читает manifest.content, не текущий Case и не глобальные версии deployment.

`DocumentBundle={bundle_id, owner_id, case_id, manifest_id, status:preparing|ready|failed|historical|expired|deleted, required_artifacts:ArtifactSpec[], published_artifact_ids[], created_at}`. Для покупки требуется purchase_card.pdf; для поддержки — application.docx, application.pdf, product_card.pdf, checklist.pdf. Bundle становится ready только после успешной публикации всех required_artifacts. Частичный сбой не объявляется полным комплектом; уже готовые части могут отображаться как неполный набор с причиной.

`DocumentModel={schema_version, document_kind, case_mode, sections:DocumentSection[], missing_fields[], sources[], warnings[], manifest_hash}` — неизменяемая модель контента. `DocumentSection` содержит только plain text, именованные поля и таблицы строк; произвольные HTML, Python или шаблонный код не допускаются.

`RenderRequest={job_id, fence_token, manifest_hash, document_kind, format:pdf|docx, template_ref, model:DocumentModel, max_output_bytes}`. Worker передаёт его в отдельный процесс, без SQL session, ключей, токена MAX и mutable Case. Для Python 3.13 явно выбираем spawn через multiprocessing context; render entrypoint не загружает Settings/ORM/секреты при импорте. Это изоляция ресурсов и зависимостей, а не отдельный security sandbox: child работает в том же доверенном контейнере. `RenderedBytes={job_id, fence_token, manifest_hash, format, mime_type, plaintext_sha256, bytes}` — ограниченный по размеру IPC результат; в JSON-документах bytes не сериализуется, base64 наружу не отправляется.

Родительский worker шифрует RenderedBytes и создаёт `StagedArtifact={artifact_id, job_id, fence_token, manifest_id, document_kind, format, plaintext_sha256, encrypted_blob_ref, size_bytes, created_at}`. Рендер-процесс не пишет открытые документы на постоянный диск. `ArtifactRecord` добавляет owner/case/revision/epoch, publication_status, published_at, expires_at; encrypted_blob_ref — приватный серверный путь.

Возможность скачать исторический уже опубликованный файл отличается от генерации: `MaterialPermit={owner_id, case_id, current_case_guard, artifact_id, artifact_original_revision, disposition:current|historical, warning_acknowledged, expires_at}`. Historical разрешается только явной кнопкой с предупреждением, пока файл хранится; не требует генерации старой revision и не объявляется актуальным. Новая генерация — только действующий подтверждённый manifest после freshness check. Для current обязательны совпадение revision и current freshness.

## 8 Работа и доставка

| DTO | Поля |
| --- | --- |
| WorkRef | job_id; kind:process_inbox/render_artifact/deliver_outbox; payload_ref; owner_id; case_id/null; case_revision/null; deletion_epoch; dedupe_key; schema_version |
| ClaimedJob | WorkRef; fence_token:int; lease_owner; lease_until; attempt:int; next_attempt_at; trace_id |
| RenderPayload | manifest_id, bundle_id, artifact_spec; никаких mutable данных кейса |
| DeliveryIntent | outbox_id; owner_id; delivery_target_id; case_guard/null; kind:view/material/callback_answer; view_ref/null; material_permit_ref/null; callback_answer_ref/null; dedupe_key; created_at |
| SendPermit | outbox_id; send_attempt_id; job_id; fence_token; owner_id; delivery_target_id; case_guard/null; approved_at; payload_hash; expires_at |
| TransportResult | confirmed{receipt:MessageReceipt или CallbackReceipt} либо definitely_rejected{error_code,retry_after/null,retryable} либо unknown{reason_code} |
| UploadResult | attachment_token_ref; artifact_id; owner_id; manifest_hash; state:uploaded/processing/ready/unknown; observed_at |
| DeleteReceipt | case_id; deletion_epoch; own_access_revoked_at; cleanup:pending/completed; inflight_delivery_possible:bool |

WorkRef — протокол приложения, не схема MAX. Не кладём персональные данные в payload queue, dedupe_key, имя файла и журнал. В PostgreSQL Job ссылается на inbox/manifest/outbox по FK. Уникальность включает bot_scope/owner/case и тип операции; одинаковый hash между разными владельцами не даёт совместный доступ.

`MessageReceipt={operation:send_message|edit_message,message_id,accepted_at}` применяется только к отправке/изменению сообщения; подтверждённый message_id берётся из проверенного ответа или исходного разрешённого edit. `CallbackReceipt={operation:answer_callback,callback_id,acknowledged_at}` подтверждает отдельную операцию над проверенным callback и не требует нового message_id. Callback ID берётся из исходного проверенного события; success проверяется по актуальной схеме ответа MAX в D10, ID сообщения не выдумывается. Receipt хранится зашифрованно, в журнале только безопасный статус.

Null case_guard допустим только для заранее определённых глобальных сообщений без сведений кейса: начало/общая помощь, технический ACK и квитанция удаления с минимумом служебных данных. Любой экран с вводом, выбором или материалом требует guard. Это не обход tombstone: после удаления можно подтвердить факт удаления, но нельзя отправить содержимое кейса. Список кейсов перечитывается по owner и live-статусу; кнопка открытия проверяет guard выбранного кейса заново.

Автоматы состояний:

| Объект | Разрешённые основные переходы |
| --- | --- |
| Inbox | received → processing → processed/ignored; recoverable failure → received; окончательная ошибка → failed |
| Job | queued → running → succeeded/failed/cancelled; безопасный повтор running → retry_wait → running; takeover увеличивает fence_token |
| Outbox | pending → preparing → sending → confirmed/definitely_rejected/delivery_unknown; определённый временный отказ → retry_wait → preparing; unknown повторяется только отдельным явным действием |
| Bundle | preparing → ready/failed; готовый после изменения case/rules → historical; удалённые бинарные файлы → expired; удаление кейса → deleted |

`processing` Inbox не даёт отдельного необратимого эффекта: применение команды, processed и создание Job/Outbox коммитятся одной транзакцией. Job успех означает успех конкретного вида работы: process_inbox=команда сохранена, render=артефакт опубликован, deliver=определён конечный исход; delivery_unknown не равно успешно доставлено.

Задание рендера захватывается только после получения свободного render slot. Lease не тратится на очередь внутри процесса. Heartbeat, finish, fail и publish — условный UPDATE по текущему fence_token/lease; операции кейса дополнительно проверяют owner/revision/epoch. SQL транзакции короткие; рендер, MAX и файловое шифрование вне них.

Перед HTTP вызовом send стадия sending и SendPermit сохраняются durable. Отдельный upload до send не доказывает доставку пользователю. Повтор upload при неизвестном результате не отправляет новое сообщение и может оставить удалённый orphan token; это ограниченный повтор со своим статусом и учётом. Crash в sending или неоднозначный ответ сети → delivery_unknown; восстановление не возвращает такую отправку в pending. Известный message_id допускает обновление только того же сообщения/владельца; неизвестный ID нельзя выдумывать.

Секретное значение attachment token хранится зашифрованным и извлекается транспортным адаптером; в DTO остальных модулей передаётся ссылка. Prepared network payload создаётся после SendPermit только из привязанных данных; произвольный chat_id не принимается от domain или пользователя.

## 9 Транзакционные сценарии

1. **Приём.** MAX adapter проверяет секрет и схему, identity adapter разрешает actor; UoW атомарно upsert identity при необходимости, insert inbox с уникальным ключом и process_inbox job. Commit → HTTP 200. Дубликат → 200 без нового job; недоступна БД → ошибка доставки.
2. **Команда.** Worker проверяет свой claim; UoW блокирует Case, проверяет owner/epoch/revision, выполняет быстрые доменные функции, сохраняет Case/InputRevision/кандидаты, помечает Inbox processed и создаёт Job/Outbox. Commit один. Для start_case создание кейса и связывание Inbox также атомарны. Pure расчёты ограничены маленькой выборкой; если объём растёт, выполняются вне транзакции с повторной проверкой версий при commit.
3. **Подтверждение.** Под блокировкой Case проверяются preview ID/hash, revision, epoch, freshness и версии; записываются Confirmation, Manifest, Bundle и render jobs по форматам. Job+manifest unique не допускает повторный комплект от дубля callback. Само подтверждение не меняет semantic revision.
4. **Публикация.** Worker заранее записывает зашифрованный blob в приватную область атомарно. UoW проверяет fence/lease/owner/revision/epoch/freshness, публикует ссылку ArtifactRecord и завершает Job. Только DB metadata даёт право чтения. Если это последний обязательный файл, bundle становится ready и в той же транзакции создаётся одно уведомление. При rollback blob остаётся недоступным orphan и удаляется уборщиком; commit не ссылается на ещё не записанный файл.
5. **Отправка.** Claim → подготовка/при необходимости upload вне транзакции → UoW проверяет текущие права и создаёт durable SendPermit/sending → HTTP вызов вне транзакции → CAS результата. Historical permit проверяется отдельно; после tombstone разрешение не выдаётся. Старый worker не получает новое разрешение, но уже начатый внешний вызов отозвать гарантированно нельзя.
6. **Удаление.** UoW блокирует Case, ставит tombstone, увеличивает epoch, аннулирует handles/permits, отменяет ещё не начатые работы и фиксирует cleanup request. После commit чтение/публикация/новый send запрещены. Удаление blobs и ciphertext выполняет maintenance с повторами. Ранее начатая доставка имеет явно описанную границу.

## 10 Хранилища и порты

`UnitOfWork` владеет begin/commit/rollback. Репозитории не вызывают commit самостоятельно. Application-функция получает UoW factory; pure domain не получает репозитории. Worker инфраструктура может вызвать отдельную UoW для claim/heartbeat, но не завершает domain job в обход application publish.

| Port | Обязательные методы | Возвращаемые типы |
| --- | --- | --- |
| CaseRepository | get_owned, lock_owned, insert, save_if_revision, mark_deleted | Result[CaseSnapshot/CaseGuard/DeleteReceipt] |
| InputRepository | append_revision, get_revision, put_candidate, consume_candidate | InputRevision, Candidate, Result[Candidate] |
| CatalogRepository | get_active_release, list_candidates, get_snapshots, read_lifecycle | VersionRef, OfferPage, OfferSnapshot[], Freshness metadata |
| RouteRepository | find_pack, get_pack, read_lifecycle | RoutePack/null, RoutePack, lifecycle |
| DocumentRepository | get_preview, save_confirmation_manifest, create_bundle, publish_artifact, list_owned, get_owned_artifact | ResultPreview, IDs, DocumentBundle, ArtifactRecord[], MaterialPermit inputs |
| WorkRepository | enqueue_unique, claim_next, renew_lease, finish_if_claim, retry_safe, recover_expired | WorkRef, ClaimedJob/null, bool, RecoveryReport |
| OutboxRepository | append_unique, begin_send_if_allowed, record_transport_result, recover_sending | DeliveryIntent, SendPermit, bool, RecoveryReport |
| PrivateStorage | stage_encrypted, read_owned_authorized, remove, purge_orphans | StagedArtifact, byte stream, DeleteReport |
| CryptoPort | encrypt, decrypt | EncryptedBlob{key_id,nonce,ciphertext,tag}, bytes |
| MaxTransport | upload_file, send_view, send_material, edit_known_message, answer_callback, inspect_subscription | UploadResult, TransportResult, SubscriptionHealth |
| Clock / IdGenerator | now / new_id | UTC datetime / UUID |

Параметры репозиториев следуют именованным DTO: owned = ActorContext + ID; save = CaseSnapshot + CaseGuard; claim/CAS = ClaimedJob + now; publication = ClaimedJob + StagedArtifact + CaseGuard + freshness. Эти порты доступны только внутри trusted application/worker; они не предоставляются пользователю напрямую.

## 11 Правила активации подготовленных файлов

Файлы `.example.json` в комплекте — синтетические образцы формы, все имеют draft/синтетические метки. Они не являются проверенными предложениями Ortonica/Medicamarket, адресатом или полным чек-листом фонда. TemplateEntry содержит assets по формату: `{format,renderer_id,path,sha256}`. Для заявления DOCX и PDF имеют отдельные ресурсы, PDF не получается конвертацией DOCX. Renderer выбирается по фиксированному registry, путь не является исполняемым пользовательским кодом. В одной записи format уникален; два renderer используют общую DocumentModel. Шаблоны и шрифт ещё нужно получить/создать; null asset hash не активируется как production-ready.

Импорт: safe read → JSON Schema по модели → типовые и межполевые проверки → разрешение всех ссылок → проверка лицензии/источников/версий → проверка полного состава → staging → атомарная активация release pointer. Dry-run ничего не меняет. Draft можно загрузить в staging; активировать синтетический draft разрешено только отдельным demo-fixtures флагом с обязательной меткой теста. Pilot требует reviewed, неотозванные, непросроченные public_snapshot пакеты и реальные шаблоны с hash. Demo с реальными снимками всё равно маркирует синтетические сведения пользователя.

Активация каталога и профиля совместима по exact VersionRef, а RoutePack и шаблон — по точным ref. Чужая версия профиля не «подходит по похожему имени». Откат переключает указатель на ранее проверенный release, но не отменяет его отзыв/expiry. При неодинаковых версиях схемы требуется явная миграция. Все операции дают ImportReport/ActivationReceipt и запись аудита без пользовательских данных.

Семантическая матрица обязательна для `validate_release`, даже если JSON Schema принимает форму:

| Объект | Проверка сверх формы JSON |
| --- | --- |
| Fact и alternatives | Ожидаемый kind у каждого known/alternative одинаков и соответствует полю; alternatives различны после нормализации; unknown.value=null, конфликт не выбирается автоматически; evidence refs разрешены |
| Денежные и логические поля | price/charge — MoneyValue с входным пределом 100 млн рублей; accepts_certificate/has_fund_contract — BooleanValue; contacts/terms/addressee — TextValue; availability — CodeValue из in_stock/on_order/out_of_stock; неизвестность задаётся Fact, не фиктивным code |
| CategoryProfile и attributes | field_key уникальны; attribute ключи существуют в точном профиле; kind/unit/allowed_codes/operator/tolerance согласованы; для quantity допустимы eq/gte/lte/within_range, для code — eq/in_set, для boolean/text — eq; range и set только для совместимого оператора; физические границы утверждены D01 |
| InputRevision и DemoProfile | prescribed/unspecified не пересекаются; значения совместимы с exact profile, range.min≤max, set непустой и без нормализованных дублей; certificate_amount проходит входной предел; DemoProfile преобразуется в runtime без изменения значения/известности |
| Delivery и price_kind | included → known charge=0 с evidence; separate → known положительный/нулевой charge либо unknown/conflicting с неполным итогом; unknown → unknown charge; conflicting → конфликт mode/charge отражён явно; from не становится exact, unknown price_kind не содержит якобы точную цену |
| IDs и refs | Уникальны ID источников/поставщиков/snapshot/профилей/условий/checklist/templates и пары id+version; одинаковый variant_id не имеет разных комплектаций; все supplier/source/profile/route/template refs разрешены; ссылки на algorithms/renderers/predicates входят в фиксированные реестры |
| Review/lifecycle | Для reviewed обязательны reviewer_id/reviewed_at/review_due_at, reviewed_at≤now<review_due_at; observed/captured_at≤reviewed_at; reviewed_at не в будущем; content review и отдельный lifecycle проверяются вместе; revoked/expired нельзя активировать через rollback |
| Assets и файлы | Пути относительные к разрешённому корню, без абсолютных путей/.., symlink escape или сетевой загрузки; файл существует, лимит размера и sha256 совпадают; лицензия/основание получены; renderer соответствует format; format в TemplateEntry и пары document_kind+format в bundle уникальны; все обязательные ресурсы и шрифты есть |
| GoldenCase | scenario известен; profile/snapshot/route refs разрешены; expected непустой и задаёт именно ожидаемый результат этого scenario, error_code из общего enum; эталон не вычисляется тестируемой функцией |
| Release и режим | Полный согласованный набор файлов; schema version поддержана; exact refs и hashes совпадают; demo flag допускает лишь явно synthetic draft; pilot требует reviewed public_snapshot/реальные assets; отсутствующее доказательство не превращается в reviewed |

Каждый отказ содержит FieldError с путём до поля; validate не исправляет значение молча. Runtime ввод проверяется по тем же правилам профиля и денег, хотя не импортируется из файла.

## 12 Вспомогательные результаты

`ValidationReport={valid, errors:FieldError[], warnings[]}`; `FieldError={path,code,safe_message_key}`; `ImportReport={release_ref,status:valid|rejected|staged,files[],errors[],warnings[]}`; `ActivationReceipt={previous_ref|null,active_ref,activated_at,actor_key}`; `RecoveryReport={requeued_ids[],unknown_delivery_ids[],cancelled_ids[],stale_claims[]}`; `DeleteReport={removed_count,remaining_count,errors[]}`; `HealthSnapshot={status,release_commit,db_ready,worker_heartbeat_age,oldest_job_age,counts}`; `SubscriptionHealth={status:active|missing|unreachable,checked_at,reason_code|null}`; `AuditEvent={event_id,kind,internal_subject_id,actor_key,timestamp,result_code}`.

`ReadContext` — ActorContext плюс текущий now. `DocumentRequest` — branch, manifest_id, required ArtifactSpec[]. `ArtifactSpec` — document_kind, format, template_ref. `CommandBatch` — нормализованный event плюс одна разрешённая команда либо ignored result. `CallbackAnswer` — owner_id, verified platform_callback_id, текстовый ключ и безопасные параметры, исходный inbox_id; DTO хранится зашифрованно и передаётся только MaxTransport.answer_callback. В DeliveryIntent заполнена ровно одна ссылка соответственно kind. `AppPorts` — набор UoW factory, Clock, IdGenerator и инфраструктурных портов, внедряемый в application, не передаваемый domain. Непредвиденные исключения становятся correlation ID и operational alert; безопасное сообщение не раскрывает внутреннее исключение.
