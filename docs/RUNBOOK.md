# Запуск и эксплуатация

## Конфигурация

Скопируйте `.env.example` в `.env` и создайте файлы секретов командой из README.
Генератор не перезаписывает существующие ключи. Храните ключ шифрования и ключ
identity отдельно от резервных копий; замена ключа без процедуры ротации делает
старые данные недоступными. Приложение не выводит значения секретов.

| Настройка | Default или пример | Назначение |
| --- | --- | --- |
| `TSR_MODE` | `demo` | Mixed public catalog с synthetic draft моделью; pilot требует всех reviewed runtime dependencies и privacy approval |
| `TSR_ALLOW_SYNTHETIC_DRAFT` | `true` | Разрешает только synthetic draft в demo; public draft/expired/revoked остаются заблокированными |
| `TSR_DATABASE_URL` | PostgreSQL URL | В Compose БД доступна только внутри сети; локальные тесты используют отдельные схемы |
| `TSR_ENCRYPTION_KEY_FILE` | `/run/secrets/encryption_key` | AES-GCM, 32 байта в hex/base64 |
| `TSR_IDENTITY_HMAC_KEY_FILE` | `/run/secrets/identity_hmac_key` | Отдельный ключ scoped identity lookup |
| `TSR_MAX_BOT_TOKEN_FILE` | `/run/secrets/max_bot_token` | Пустой для локальной проверки; реальная отправка с пустым токеном отвергается |
| `TSR_WEBHOOK_SECRET_FILE` | `/run/secrets/webhook_secret` | Проверка `X-Max-Bot-Api-Secret` |
| `TSR_HEALTH_TOKEN_FILE` | `/run/secrets/health_token` | Проверка `X-Readiness-Secret` |
| `TSR_MAX_API_URL` | `https://platform-api2.max.ru` | API MAX |
| `TSR_WEBHOOK_URL` | HTTPS endpoint тестового бота | Проверяемая subscription; отсутствует для offline demo |
| `TSR_MAX_CA_BUNDLE` | отсутствует | Дополнительный проверенный PEM CA bundle; TLS verification включена |
| `TSR_RELEASE_ROOT` | `/app` | Фиксированные data/templates/fonts в checkout/image |
| `TSR_RELEASE_MANIFEST` | `data/releases/real-1.1.0/manifest.json` в `.env.example` | Initial release только для пустого scoped pointer; далее читается фактический active record из DB |
| `TSR_RELEASE_COMMIT` | фактическая revision образа | Кодовая версия manifest/heartbeat; задайте перед созданием кейсов |
| `TSR_BOT_SCOPE` | `tsr-demo` | Область identity, очередей, квоты и active pointer; разные боты используют разные области |
| `TSR_PRIVATE_ROOT` | `/var/lib/tsr/blobs` | Приватные ciphertext blobs на общем volume app/worker |
| `TSR_BACKUP_ROOT` | `/var/lib/tsr/backups` | Шифрованные архивы `.tsrb`, retention семь дней |
| `TSR_DELETION_JOURNAL_ROOT` | `/var/lib/tsr/deletion-journal` | Отдельный шифрованный журнал удалений; не удаляется семидневной очисткой backup |
| `TSR_MAINTENANCE_INTERVAL_SECONDS` | 300 | Scheduled retention и безопасная очистка |
| `TSR_RETENTION_CASE_DAYS` / `TSR_RETENTION_ARTIFACT_DAYS` | 90 / 7 | Кейсы/manifest по last_activity; физическая очистка файлов |
| `TSR_RETENTION_INBOX_HOURS` / `TSR_RETENTION_DEDUPE_DAYS` | 24 / 30 | Sensitive processed inbox payload и минимальные dedupe-записи; pending события не стираются |
| `TSR_WORKER_HEARTBEAT_STALE_SECONDS` | 60 | Предел свежести heartbeat для readiness |
| `TSR_CANDIDATE_TTL_SECONDS` / `TSR_PREVIEW_TTL_SECONDS` / `TSR_HANDLE_TTL_SECONDS` | 900 | Срок жизни кандидатов, подтверждений и кнопок |
| `TSR_ARTIFACT_TTL_SECONDS` | 604800 | Семь дней разрешённого доступа к файлу |
| `TSR_LEASE_SECONDS` / `TSR_HEARTBEAT_SECONDS` | 60 / 15 | Claim lease/fence и его продление |
| `TSR_RENDER_TIMEOUT_SECONDS` / `TSR_NETWORK_TIMEOUT_SECONDS` | 45 / 15 | Жёсткое завершение дочернего render; сетевой timeout |
| `TSR_DB_CONNECT_TIMEOUT_SECONDS` | 5 | Ограничение подключения к PG |
| `TSR_DB_STATEMENT_TIMEOUT_MS` / `TSR_DB_LOCK_TIMEOUT_MS` | 3000 / 1000 | Запросы и ожидание блокировок в рабочих UoW |
| `TSR_DB_IDLE_TRANSACTION_TIMEOUT_MS` / `TSR_DB_BACKUP_SNAPSHOT_TIMEOUT_MS` | 5000 / 300000 | Простой рабочей транзакции; отдельный предел backup snapshot |
| `TSR_MAX_ATTEMPTS` | 5 | Ограниченные безопасные повторы |
| `TSR_MAX_BODY_BYTES` / `TSR_MAX_INPUT_CHARS` | 131072 / 2000 | Ограничение webhook/ввода |
| `TSR_MAX_FILE_BYTES` / `TSR_MAX_BACKUP_BYTES` | 10485760 / 536870912 | Ограничение файла и backup archive |

Не задавайте одновременно `*_FILE` и значение того же секрета. Конфигурация
читается при startup; domain/renderer при импорте секреты не читают. Render slot
фиксирован равным одному. В образе установлен PostgreSQL client; перед запуском
backup проверьте совместимость `pg_dump`/`pg_restore` с сервером PostgreSQL 16.

## Release и обычные операции

```bash
docker compose up --build -d
docker compose exec app tsr validate-data --manifest data/releases/real-1.1.0/manifest.json
docker compose exec app tsr import-release --manifest data/releases/real-1.1.0/manifest.json --actor-key operator-cli
docker compose exec app tsr activate-release --id real-catalog-demo --version 1.1.0 --mode demo --actor-key operator-cli
docker compose restart app worker
docker compose exec app tsr health
docker compose exec app tsr maintenance
docker compose exec app tsr cleanup
```

App/worker применяют идемпотентные миграции и инициализируют release только когда
pointer `(bot_scope, mode)` пуст. Уже активное содержимое берётся из неизменяемой
записи DB и проверяется по pinned files/hashes и lifecycle каждой зависимости.
Смена `.env` manifest не подменяет active pointer. После явной активации другой
версии перезапустите app/worker. Отозванный пакет не возрождается при startup;
rollback использует те же проверки свежести и policy, что activation.

Примеры остальных операторских команд:

```bash
tsr rollback-release --id demo-release --version 1.0.0 --mode demo --actor-key operator-cli
tsr revoke-package --id public-ort-base200 --version 1.1.0 --reason-code source_invalid --actor-key operator-cli
```

Перед rollback уточните точный ID нужного release в manifest: старый пакет
использует собственный ID. Revoke действует на точную неизменяемую зависимость;
это блокирует новые подтверждения и текущую выдачу затронутых результатов.
Операции activation/rollback относятся к настроенному `TSR_BOT_SCOPE`.

Не удаляйте volumes при обычном останове. `docker compose down` сохраняет DB,
файлы, backups и deletion journal. Код работает от UID 10001; генератор оставляет
host secret directory приватным 0700, secret mounts доступны только для чтения.
HTTP фиксирует inbox/job до ACK. Render, файловый I/O и MAX запросы выполняются
вне пользовательских SQL транзакций. Общая DB-квота исходящих запросов действует
на bot scope и private recipient; дополнительные workers не обходят её.

## HTTP и живой MAX

Доступны только `POST /webhooks/max`, `GET /health/live` и защищённый
`GET /health/ready`. Ready проверяет DB, свежий worker соответствующей кодовой
версии, scoped active release и точные lifecycle/policy dependencies, критические
секреты, свежую HTTPS subscription при настроенном боте и restore barrier.
С пустым токеном локальный demo работает, readiness живого бота возвращает 503.
`tsr health` показывает безопасные reason codes и раздельные ages/counts очередей,
без пользовательских текстов, токенов или SQL/DSN.

Для live проверки нужны токен тестового бота, публичный HTTPS endpoint, reverse
proxy/trusted TLS и webhook subscription с секретом. Настройка описана в
[integrations/MAX.md](integrations/MAX.md). Для дополнительного доверенного CA
используйте проверенный полный bundle и `compose.max-ca.yaml`.

Локальный `tsr demo --dataset public` использует отдельную область
`<scope>:demo:public`; старый набор — `<scope>:demo:synthetic`. Оба не обращаются
к MAX, не меняют рабочий active release и не обрабатывают jobs живого бота.
Поддерживаются только личные диалоги; безопасная подсказка о них не сохраняет
групповой кейс и не отправляет пользовательские материалы в группу.

## Удаление и восстановление задач

После подтверждённого удаления tombstone/epoch немедленно отменяют доступ.
`tsr cleanup` и scheduled maintenance удаляют ciphertext/blobs для точного epoch;
сбой удаления сохраняет ссылку для повторной очистки. Retention применяет сроки
из таблицы. Orphan sweep не удаляет DB references и не трогает staged blobs при
живом render claim; просроченные кандидаты и preview также очищаются.

Expired lease захватывается с новым fence. Старый worker не может завершить или
опубликовать задачу. Stuck render завершается, процесс пересоздаётся. До внешней
отправки фиксируется `sending`; после неоднозначного исхода или crash recovery
получается `delivery_unknown`, без автоматического повтора. Явный повтор
пользователя предупреждает о возможном дубликате. Upload и подтверждённый
`attachment.not.ready` имеют отдельные ограниченные безопасные повторы.

## Шифрованный backup и offline restore

Backup хранит согласованный PG snapshot и ciphertext опубликованных файлов,
проверяет hashes/bounds и не включает ключи. Архив зашифрован и создаётся
атомарно; plaintext dump не сохраняется на диск. Нужна maintenance граница:

```bash
docker compose stop app worker
docker compose run --rm app tsr backup --maintenance --destination /var/lib/tsr/backups
```

Сохраните путь `.tsrb` из receipt. Чтобы восстановить старую копию без возвращения
удалённых после неё кейсов, нужен **актуальный отдельный журнал с источника**.
После остановки app/worker экспортируйте его, пока источник остаётся offline:

```bash
docker compose run --rm app tsr export-deletion-journal --source-offline --destination /var/lib/tsr/deletion-journal
```

Сохраните путь `.tsrj` и напечатанный `Cutover`. `--source-offline` — явное
заявление оператора, что новых записей после этого cutover не возникает. Журнал
хранится отдельно от старого backup и дольше всех ещё восстанавливаемых копий;
семидневная очистка архивов его не удаляет. Автоматическая полнота внешнего
журнала при продолжающей работать исходной DB не заявляется.

Подготовьте **пустую отдельную offline DB** с правами создать исходную schema;
настройте `TSR_DATABASE_URL` для этой цели и сохраните исходные crypto keys.
Не запускайте обычные app/worker/migrate на цели перед restore. Непустой target
отвергается. Пример команды после настройки target DSN:

```bash
docker compose run --rm app tsr restore --backup /var/lib/tsr/backups/ARCHIVE.tsrb --journal /var/lib/tsr/deletion-journal/JOURNAL.tsrj --cutover CUTOVER_WITH_TIMEZONE --source-offline --offline --maintenance
```

Пути и timestamp замените точными значениями предыдущих шагов. Архив и журнал
полностью аутентифицируются до первого изменения target DB; stale/missing journal,
чужой source, неверный ключ, повреждённый blob или непустая цель отвергаются.
Persistent restore gate остаётся pending/failed до проверки metadata и replay
максимальных deletion epochs с физической очисткой. Bootstrap, application,
worker, transport bindings и readiness учитывают barrier. Только `ready=true`
разрешает дальнейший startup; после неудачи не снимайте gate вручную, создайте
новую пустую цель и повторите проверенную процедуру.

## Локально без Docker

Установите зависимости по README и запустите PostgreSQL 16 отдельно. Создайте
secrets командой `tsr init-secrets`. Задайте `TSR_DATABASE_URL`, файлы ключей,
webhook/readiness secrets, `TSR_PRIVATE_ROOT=var/private`, `TSR_RELEASE_ROOT=.` и
`TSR_RELEASE_MANIFEST=data/releases/real-1.1.0/manifest.json`. `tsr` не читает `.env`
автоматически; Compose загружает env_file.

Запустите `tsr demo --dataset public --scenario both --show-dialog`. Для живого
бота запускаются отдельно `tsr serve` и `tsr worker`. Editable installation
предполагает наличие data/templates/fonts в checkout; standalone wheel не
содержит весь release payload. Запуск выполняется из корня репозитория с указанными переменными окружения.
