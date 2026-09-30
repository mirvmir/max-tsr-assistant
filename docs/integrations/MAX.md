# Подключение MAX

## HTTPS webhook

1. Сохраните токен бота в `secrets/max_bot_token` одной строкой.
2. Разместите приложение за HTTPS reverse proxy. В `.env` задайте `TSR_WEBHOOK_URL=https://your-host.example/webhooks/max`.
3. Зарегистрируйте подписку через [POST /subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions). Укажите URL webhook, события `message_created`, `message_callback`, `bot_started` и значение из `secrets/webhook_secret`.
4. Перезапустите сервисы и проверьте состояние:

   ```bash
   docker compose up --build -d
   docker compose exec app tsr health
   ```

5. Откройте личный диалог с ботом и выполните [демонстрационные сценарии](../FIRST_SCENARIO.md).

Адрес API в конфигурации: `https://platform-api2.max.ru`. Адаптер передаёт токен заголовком `Authorization`. HTTP-порт контейнера привязан к `127.0.0.1:8080`; внешний TLS завершает reverse proxy.

## Локальный polling

Для локального бота без публичного webhook используйте [Windows startup](../WINDOWS_START.md) или override Compose:

```bash
docker compose -f compose.yaml -f compose.polling.yaml up --build -d
```

Для polling оставьте `TSR_WEBHOOK_URL` пустым и удалите ранее зарегистрированную webhook-подписку согласно [API MAX](https://dev.max.ru/docs-api/methods/DELETE/subscriptions). Bridge проверяет подписки перед получением обновлений.

`tools/poll_max.py` передаёт обновления в тот же защищённый HTTP-вход. Курсор и ожидающие события сохраняются зашифрованно. Курсор продвигается после подтверждения записи или сохранения постоянного отказа. Временная ошибка сохраняет событие для повтора. Постоянные отказы учитываются ограниченными счётчиками причин.

## TLS

Проверка сертификата и имени узла включена. Для дополнительной доверенной цепочки задайте `TSR_MAX_CA_BUNDLE` и подключите подготовленный оператором PEM bundle:

```bash
docker compose -f compose.yaml -f compose.max-ca.yaml up --build -d
```

`TSR_MAX_CA_BUNDLE_HOST` задаёт локальный путь к bundle. Включите в него корни, необходимые API MAX и узлам загрузки файлов. Подробные команды Windows находятся в [WINDOWS_START.md](../WINDOWS_START.md). Пример локальных путей: [compose.local-ca.example.yaml](../../compose.local-ca.example.yaml).

## HTTP-вход и доставка

| Операция | Поведение | Контракт MAX |
| --- | --- | --- |
| Webhook | Проверка `X-Max-Bot-Api-Secret`, запись inbox/job до ответа 200 | [Subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions) |
| События | Личный старт, текст и callback, минимизация входных данных | [Update](https://dev.max.ru/docs-api/objects/Update) |
| Сообщения | Текст и кнопки, получатель из сохранённой identity | [Messages](https://dev.max.ru/docs-api/methods/POST/messages) |
| Callback | Ответ по связанному callback ID | [Answers](https://dev.max.ru/docs-api/methods/POST/answers) |
| Файлы | Загрузка, сохранение attachment reference, отправка владельцу | [Uploads](https://dev.max.ru/docs-api/methods/POST/uploads) |

Дубликат входного события не создаёт второе задание. Worker сохраняет состояние отправки до внешнего запроса. HTTP 429 и `attachment.not.ready` допускают ограниченный повтор. При неопределённом исходе отправки пользователь явно запрашивает повтор. Постоянный отказ не запускает бесконечные попытки.

Секреты, тексты сообщений и содержимое кейсов не попадают в диагностические ответы. Доступ к документу проверяется по владельцу, версии кейса и сроку действия.

## Состояние сервиса

`GET /health/live` проверяет HTTP-процесс. `GET /health/ready` требует `X-Readiness-Secret` и проверяет БД, worker, активный пакет, секреты, HTTPS-подписку и состояние восстановления. Локальная демонстрация работает без токена, а readiness подключения MAX при этом возвращает 503. Polling не заменяет условие HTTPS-подписки в readiness.

Операторская команда `tsr health` выводит коды состояния и агрегированные метрики очередей. Форматы HTTP описаны в [OpenAPI](../api/openapi.json) и [DATA-API.yaml](../api/DATA-API.yaml). Резервное копирование, очистка и восстановление описаны в [RUNBOOK.md](../RUNBOOK.md).
