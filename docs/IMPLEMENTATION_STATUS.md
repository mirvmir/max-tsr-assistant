# Статус реализации после повторной проверки

Основание: неизменённые `docs/specs/CONTRACTS_v1.md`, `IMPLEMENTATION_PLAN_v1.md`,
`MVP_DATA_TASKS_v1.md`. Отдельный `ARCHITECTURE_v1.md` в checkout не предоставлен;
архитектурные ограничения взяты из плана. Полная матрица 70 задач реализации и 14
задач данных находится в [независимом ревью](verification/real-catalog-review.md).
Наличие реализации не означает принятие всех внешних критериев MVP.

## Реальная цепочка первого сценария

**Ortonica + Medicamarket → 12 предложений → catalog.json → подбор → условный
расчёт сертификата → документы** реализована в отдельном пакете версии 1.1.0.
Шесть предложений каждого продавца проверены по прямым страницам. Каталог и
`normalized_catalog.json` имеют одинаковое содержание; факты привязаны к SKU,
конфигурации, URL, дате наблюдения и evidence. Независимый повторный запрос
проверил 151 факт. См. [описание каталога](data/REAL_CATALOG.md),
[фактический аудит](data/FACTUAL_AUDIT.md) и [первый сценарий](FIRST_SCENARIO.md).

Публичные снимки имеют технические review-метки этого аудита. Тестовые пользователи,
профиль категории, маршрут обращения и шаблоны остаются явно synthetic draft.
Это mixed demo release; он не допускается в pilot. Старый пакет 1.0.0, его пять
синтетических SKU и hash-проекции сохранены для совместимости и регрессий.

Первый выбор — Ortonica Base 200, код предложения 5048, ширина 405 мм,
грузоподъёмность 130 кг, опубликованная цена 14 500 ₽. Для модельного пользователя
с сертификатом 10 000 ₽ условная разница равна 4 500 ₽. Приём сертификата именно
для заказа и доставка неизвестны: покрытие/разница помечены условными, итоговая
стоимость с доставкой остаётся неизвестной. Баннер продавца не становится
подтверждением приёма сертификата или договора с фондом. Цены «от», несовпадающие
цены и наличие сохранены без выбора удобного значения.

## Реализованные области и границы

| Область | Реализовано | Остаток / граница |
| --- | --- | --- |
| Contracts / CASE | Frozen strict DTO, canonical hashes, owner/revision/dialog/epoch guards, неизменяемые revisions/candidates, tombstone; additive provenance без изменения старых hashes | Медицинский профиль и порядок представительства требуют предметного утверждения |
| DB / release | Encrypted payload, FK/dedupe/leases/fences, типизированные immutable пакеты, scoped active pointer, lifecycle до отдельных source/snapshot/template refs, CLI import/activate/rollback/revoke | Полная каталоговая портовая поверхность и indexed catalog candidate retrieval остаются развитием первой версии; рабочий rank проверяется на 1000 предложениях |
| CAT / MATCH / PRICE | 12 публичных предложений, exact configuration/units/provenance, детерминированный rank, страницы по три, unknown/conflicting/from; целые копейки, отдельные сертификат/доставка/договор | Факты относятся к дате снимка; обновление и право на массовое переиспользование требуют отдельного решения |
| ROUTE | Фиксированные predicates, четыре статуса, список пропусков/действий, проверка refs/hashes/lifecycle | Только synthetic модель; полный реальный региональный маршрут и адресат ещё не подготовлены |
| CONV / APP | Русский диалог, явная роль, подтверждение/правки, сохранение pending candidate, мои кейсы, история/материалы, пагинация, guarded handles, frozen preview→manifest; смена алгоритма инвалидирует current result | Приёмка в настоящих MAX mobile/web и пользовательское исследование отсутствуют |
| DOC / FILE | Покупка PDF; обращение DOCX + 3 PDF; кириллица и DEMO, conditional money, русские причины и вопросы продавцу, exact template registry; private encrypted blobs, current/historical access | Нет экспертно утверждённых реальных форм. Поддерживаются фиксированные engines, а не произвольный исполняемый шаблон |
| MAX / HTTP | Verified minimal ingress/ACK after commit, typed transport/uploads/receipts, safe unknown-send, opaque buttons, shared DB quota, private guidance для доверенного actor; protected readiness | Нет live token/HTTPS/subscription и двух реальных клиентов; fixed-window DB quota не обещает распределённое sliding-window ограничение |
| Worker / observability | Один spawn renderer с hard timeout, fenced renew/retry/recovery, persistent heartbeat, scoped queue counts/ages, safe trace/code logs и шесть агрегатов queue/compute | Нагрузочные измерения относятся к локальному facade/worker; proxy/network/MAX latency не измерены |
| Retention / restore | Scheduled case/manifest90d, blobs7d, processed inbox24h, dedupe30d; claim-safe orphan sweep; encrypted consistent DB+blob backup7d, отдельный журнал, offline restore barrier и deletion replay | Размещение независимого журнала/ключей и операторский cutover нужно проверить в окружении эксплуатации. Автоматического внешнего dual-write нет |
| QA / delivery | PostgreSQL негативные и crash/fence/delete/owner tests, две ветки для public и legacy dataset, scoped независимое ревью, воспроизводимые load modes, runbook/API/сценарий | Полный golden/document oracle corpus, Docker build/clean startup, live deployment, служебный слайд/комплект сдачи и user study не завершены |

## Контрактные дополнения

`NavigatePayload` содержит optional screen/resource_id и bounded page; guard/owner/
revision/epoch/TTL продолжают действовать. `ManifestContent` содержит optional
catalog_ref/data_release_ref/supplier: только эти три новые None поля исключаются
из legacy canonical projection. Заполненные значения участвуют в hash.
`RenderRequest`/`RenderJobContext` передают typed template registry отдельно от
manifest hash. `ClaimedJob.created_at` — optional operational metadata для queue
timing. Persistent metric aggregates не содержат owner/job/trace/payload.

MAX использует plain text, а не интерпретирует ввод пользователя как markup.
Лимиты длины/кнопок проверяются до durable sending. Неопределённая отправка не
повторяется автоматически; historical material требует явного предупреждения.

## Что ещё требуется для пилота

D01/D04/D05: экспертные параметры категории, реальный маршрут/условия/адресат и
пригодные формы. D11/QA03/REL01/REL03: живой MAX, HTTPS, subscription, два клиента,
Docker build/restart и размещение. D12/REL02: правила сдачи, FAQ, служебный слайд и
доступы. D13: владелец процесса, основания, политика и представительство для
реальных чувствительных сведений. QA06: реальные участники и сравнительная
проверка пользы. Эти условия не заменяются synthetic fixtures или флагом demo.

Результаты окончательных команд, нагрузка и визуальная проверка фиксируются в
[финальном протоколе](verification/real-catalog-validation.md). Исходные чекбоксы
backlog не переписаны; статусы и ограничения приведены отдельно для проверки.
