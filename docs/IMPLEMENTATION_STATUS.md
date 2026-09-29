# Статус первой реализации

Основание: `docs/specs/CONTRACTS_v1.md`, `IMPLEMENTATION_PLAN_v1.md`, `MVP_DATA_TASKS_v1.md`. ARCHITECTURE_v1.md отдельно не приложен; использовано архитектурное решение, зафиксированное в предоставленном плане. Исходные документы не переписаны. Ниже статус первой **synthetic demo** реализации, а не закрытие всех задач исходного backlog.

| Область | Реализовано в первой версии | Осталось / границы проверки |
| --- | --- | --- |
| C01–C03 / CASE | Frozen typed DTO, strict major/extra fields, NFC/canonical hash, Result, owner/semantic-dialog revisions, candidates, tombstone | Полный набор полей для реальных категорий/представительства определяется approved data |
| DB01–DB05 | PostgreSQL migrations/UoW, encrypted payload, scoped dedupe, FK, leases/fences, durable sending/receipts, release audit/pointer | Репозитории часто читают полную небольшую историю; bounded DB queries/pooling до масштаба |
| CAT / MATCH / PRICE | Exact SKU snapshots, match/incomplete/mismatch, integer kopeks, unknown≠0, refusal/conditional certificate/shipping | Реальные reviewed medical profiles/suppliers/prices отсутствуют |
| ROUTE / D | Фиксированный predicate registry, четыре статуса, источники/schema/hash validation, staging/activation/revocation | Только один synthetic model route; per-package lifecycle и generic active-release loading не завершены |
| CONV / APP | Русский диалог, явная роль, подтверждение/правки, смена предложения/ветки, frozen preview/manifest, старые/чужие guards, страницы длинного текста/материалов | UX живого MAX mobile/web ещё не проверен |
| DOC / FILE | Покупка PDF; обращение DOCX + 3 PDF, кириллица, DEMO, encrypted private blobs, authorized current/historical access и expiry | Нет официальных утверждённых региональных форм; scheduled physical expiry/janitor ещё нет |
| MAX / HTTP | Проверенный webhook, HMAC identity, minimal ingress, ACK after commit, typed transport/upload, limits/opaque buttons, receipt binding, optional trusted CA, inspect_subscription adapter | Нет live token/HTTPS/subscription; groups/channels игнорируются, безопасная подсказка группе ещё не реализована; inspection не подключён к readiness/планировщику |
| W | Inbox/render/outbox worker, один spawn renderer, hard render timeout/recycle, safe bounded retry до отправки, unknown-send recovery | Общая rate quota нескольких workers, persistent heartbeat/queue metrics и нагрузка ещё не реализованы; graceful shutdown доставки/DB ограничен внешним Compose timeout |
| OPS / QA | Явный cleanup tombstoned case, schema validation, owner/epoch/fence/freshness tests, две local demo ветки, визуальная QA документов | Scheduled retention, backup encryption/restore, deletion replay, полная readiness не завершены |
| REL | Dockerfile/Compose, pinned runtime dependencies, CLI/runbook/first-scenario, проверяемая ветка и независимое ревью | Docker daemon/build/run, live MAX и end-to-end latency/load здесь не проверены |

## Данные и допускаемый режим

Пять синтетических SKU и два вымышленных поставщика покрывают точную цену, price-from, неизвестность и несовпадение. Никакие вымышленные условия не выдаются за факт о реальном продавце, медицинском изделии или фонде. Первый расчёт: 120 000 ₽ цена с включённой доставкой − 100 000 ₽ применимого принятого сертификата = 20 000 ₽ расчётная разница. Модель маршрута не обещает выплаты/приём заявления.

Pilot fail-closed: flags не превращают synthetic draft в reviewed package. До пилота нужны экспертно проверенные параметры категории, реальные snapshots и источники/сроки, маршрут/условия/адресат/шаблоны, политика обработки данных, а также перечисленные выше эксплуатационные доработки. D01–D06 и все внешние acceptance gates не объявляются выполненными.

## Изменения контрактов для первой UI реализации

Исходный контракт сохранён в `docs/specs`. Исполняемый `NavigatePayload` дополнен optional `screen`, `resource_id` и bounded `page` с default `0`. Они служат только guarded пагинации frozen candidate/preview и истории материалов. Candidate/review требуют привязанный resource; materials не принимает произвольный resource. Условия owner/revision/epoch/TTL сохранены. Подтверждение не пересчитывает manifest и не сокращает подтверждённые значения.

Сообщения MAX теперь plain text: пользовательские значения не требуют HTML-escaping и не интерпретируются как markup. Ограничения длины/кнопок проверяются до `sending`; локальная ошибка форматирования не становится неопределённой внешней доставкой.

## Проверка и независимая оценка

Команды, фактический итог тестов и оценка независимого агента фиксируются в `docs/verification/implementation-review.md` после финального прогона. Предварительный reviewer выявил восемь важных дефектов: startup, supervision render, retry upload, привязка rendered hash, смена выбора, MAX size limits, receipt binding и CA configuration. Для исправлений добавлены точечные регрессии; итоговая оценка относится к demo-границе, указанной выше.

Визуальные проверки PDF/DOCX описаны в [documents-qa.md](verification/documents-qa.md). Локальные сценарии не заменяют проверку download/UX в настоящем MAX и не доказывают production capacity.
