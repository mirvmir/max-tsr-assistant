# Запуск и эксплуатация первой реализации

## Конфигурация

`cp .env.example .env`, затем создайте файлы секретов командой из README. Генератор не перезаписывает существующие ключи. Не удаляйте и не заменяйте ключ шифрования при перезапуске: текущая версия не имеет процедуры ротации/расшифровки старых ключей. Значения секретов не выводятся командами приложения.

| Настройка | Default/пример | Назначение |
| --- | --- | --- |
| `TSR_MODE` | `demo` | Pilot требует reviewed-data/privacy flags и secrets, но текущий synthetic release всё равно не активируется для pilot |
| `TSR_ALLOW_SYNTHETIC_DRAFT` | `true` в demo | Явное разрешение synthetic draft; не переводит пакет в reviewed |
| `TSR_DATABASE_URL` | PostgreSQL URL | БД не публикуется наружу в compose; demo password используется только для локального запуска |
| `TSR_ENCRYPTION_KEY_FILE` | `/run/secrets/encryption_key` | AES-GCM, 32 байта в hex/base64 |
| `TSR_IDENTITY_HMAC_KEY_FILE` | `/run/secrets/identity_hmac_key` | Отдельный ключ scoped identity lookup |
| `TSR_MAX_BOT_TOKEN_FILE` | `/run/secrets/max_bot_token` | Пустой при локальном demo; live transport не имитирует успех |
| `TSR_WEBHOOK_SECRET_FILE` | `/run/secrets/webhook_secret` | Проверка `X-Max-Bot-Api-Secret` |
| `TSR_HEALTH_TOKEN_FILE` | `/run/secrets/health_token` | Проверка `X-Readiness-Secret` |
| `TSR_MAX_API_URL` | `https://platform-api2.max.ru` | Официальный API host |
| `TSR_MAX_CA_BUNDLE` | отсутствует | Проверенный complete PEM CA bundle; TLS verification всегда включена |
| `TSR_PRIVATE_ROOT` | `/var/lib/tsr/blobs` в compose | Приватные ciphertext blobs, общий volume app/worker |
| `TSR_RELEASE_ROOT` | `/app` в compose | Фиксированные данные, схемы, templates/fonts в checkout/image |
| `TSR_RELEASE_COMMIT` | `first-mvp-development` | Для своего image укажите фактическую release revision до первого кейса |
| `TSR_BOT_SCOPE` | `tsr-demo` | Область identity/dedupe/worker; не запускайте разные боты с одинаковой областью |
| `TSR_CANDIDATE_TTL_SECONDS` / `TSR_PREVIEW_TTL_SECONDS` / `TSR_HANDLE_TTL_SECONDS` | 900 | Срок жизни подтверждения и кнопок |
| `TSR_ARTIFACT_TTL_SECONDS` | 604800 | 7 дней: после TTL доступ запрещён, физическое плановое удаление ещё не реализовано |
| `TSR_LEASE_SECONDS` / `TSR_HEARTBEAT_SECONDS` | 60 / 15 | Lease/fence и продление текущей задачи |
| `TSR_RENDER_TIMEOUT_SECONDS` | 45 | Жёсткий предел child render и пересоздание процесса |
| `TSR_MAX_ATTEMPTS` | 5 | Ограниченные безопасные повторы |
| `TSR_MAX_BODY_BYTES` / `TSR_MAX_INPUT_CHARS` | 131072 / 2000 | Ограничение webhook/ввода |
| `TSR_MAX_FILE_BYTES` / `TSR_NETWORK_TIMEOUT_SECONDS` | 10485760 / 15 | Ограничение файлов/сетевых запросов |

Не задавайте одновременно `*_FILE` и значение того же секрета. Конфигурация читается при startup; импорт domain/renderer секреты не читает. `render_slots` фиксирован равным 1.

## Команды

```bash
docker compose up --build -d
docker compose exec app tsr validate-data
docker compose exec app tsr migrate
docker compose exec app tsr cleanup
docker compose logs --tail=100 app worker
docker compose down
```

App и worker при запуске применяют идемпотентные миграции, импортируют один локальный demo release и активируют его, если pointer пуст. Отозванный release не активируется заново; несовпадение active pointer блокирует новые результаты. Generic release selection из БД ещё не реализован.

Не удаляйте volumes при обычном останове. `docker compose down` сохраняет БД/файлы. Код работает от UID 10001; app/worker используют один приватный volume. Генератор оставляет каталог секретов host приватным (`0700`), файлы доступны для чтения в read-only secret mount контейнера.

Сейчас используйте **один worker** на bot scope: rate limiter in-process, общей квоты для нескольких workers пока нет. HTTP получает вход, сохраняет inbox/job и возвращает ACK после commit. Рендер и исходящие запросы выполняются worker, вне пользовательских SQL транзакций.

## HTTP и живой MAX

Доступны только `POST /webhooks/max`, `GET /health/live`, защищённый `GET /health/ready`. `/health/ready` проверяет доступность БД, но ещё не heartbeat worker, backlog, subscription или полноту approved data. Успешная readiness сейчас не подтверждает работоспособность всего бота.

Нужны токен тестового бота, публичный HTTPS URL и webhook subscription с секретом. Заполните secret file токена, задайте `TSR_WEBHOOK_URL`; добавьте reverse proxy/trusted TLS и зарегистрируйте subscription по [инструкции MAX](integrations/MAX.md). Для Минцифры используйте проверенный complete CA bundle и optional `compose.max-ca.yaml`. Не отключайте проверку TLS и не публикуйте PostgreSQL/private storage.

Локальный `tsr demo` использует отдельный owner и queue scope, не обращается к MAX и не обрабатывает jobs живого бота. В отсутствие токена обычный worker явно отвергает внешнюю отправку; для проверки файлов используйте эмулятор.

## Восстановление и удаление

Expired lease захватывается с новым fence. Старый worker не может завершить/опубликовать задачу. Stuck render завершается и процесс пересоздаётся. Ciphertext сначала сохраняется во временный файл, затем атомарно переименовывается до короткой DB publication.

Жёсткая остановка проверена для render child. Сетевые операции имеют timeout; зависший поток доставки или DB I/O пока не имеет доказанной верхней границы завершения Python. Compose задаёт внешнее `stop_grace_period: 30s`, после которого контейнер может быть принудительно остановлен. До пилота нужны DB/connect/statement timeouts и проверка shutdown/recovery при инфраструктурных сбоях; полного graceful-shutdown SLA здесь нет.

До внешней отправки фиксируется `sending`. После неоднозначного сетевого результата или восстановления оборванной отправки статус становится `delivery_unknown`; автоматический повтор запрещён. Пользователь может явно согласиться с риском дубликата. Upload до отправки и подтверждённый `attachment.not.ready` имеют отдельные ограниченные безопасные повторы, без повторного рендера.

Подтверждённое удаление сначала ставит tombstone/увеличивает epoch, отменяет доступ и jobs. `tsr cleanup` удаляет blobs и ciphertext дочерних записей только для этого epoch, оставляя минимальный tombstone/dedupe. Повторная очистка идемпотентна; сбой удаления файла оставляет cleanup pending. Это ещё не планировщик retention.

Нужно реализовать до пилота: scheduled purge (кейсы/manifest 90 дней, чувствительный inbox 24 часа, dedupe 30 дней, файлы 7 дней), persistent worker heartbeat/метрики и расширенную readiness, orphan janitor, encrypted backup/7-day retention и проверенный restore с replay deletion epochs. Наличие backup volume не заменяет эти операции.

## Локально без Docker

Установите зависимости по README; запустите PostgreSQL 16 отдельно. Создайте secrets командой `tsr init-secrets`. Перед запуском задайте `TSR_DATABASE_URL`, `TSR_ENCRYPTION_KEY_FILE=secrets/encryption_key`, `TSR_IDENTITY_HMAC_KEY_FILE=secrets/identity_hmac_key`, `TSR_WEBHOOK_SECRET_FILE=secrets/webhook_secret`, `TSR_HEALTH_TOKEN_FILE=secrets/health_token`, `TSR_PRIVATE_ROOT=var/private`, `TSR_RELEASE_ROOT=.`. `tsr` не читает `.env` автоматически; env_file загружает Compose.

Запустите `tsr demo --scenario both --show-dialog`, при необходимости `tsr serve` и отдельно `tsr worker`. Установка editable предполагает, что data/templates/fonts остаются в корне checkout; standalone wheel пока не содержит полный release payload.
