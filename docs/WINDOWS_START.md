# Локальный запуск на Windows

Все команды выполняются из корня клонированного репозитория.
Нужны Docker Desktop с Linux containers и Python 3.12+ в `PATH`.
Python 3.13 и PostgreSQL 16 работают внутри Docker; установленный в Windows
PostgreSQL не участвует в запуске. Для полного набора тестов нужен Python 3.12:
тестовая зависимость `pgserver==0.1.4` не выпускается для Python 3.13.

## Подключение бота

1. Инициализируйте проект без подключения MAX:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -Offline
   ```

   Скрипт создаст `.env`, файлы секретов и локальные сервисы. Существующие
   секреты не перезаписываются. Если ключ шифрования уже используется,
   сохраните его: без него ранее сохранённые данные не расшифровать.
2. Откройте `secrets\max_bot_token` и сохраните токен MAX одной строкой,
   без кавычек. Не отправляйте токен в переписку и не добавляйте его в Git.
3. При необходимости подготовьте TLS bundle по разделу ниже, затем выполните:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
   ```

   `ExecutionPolicy Bypass` действует только для этого процесса.
   Скрипт запускает Docker Desktop при необходимости, собирает один общий
   образ, запускает базу, сервер, worker и приём событий MAX.
   Существующие секреты и данные сохраняются. После изменения токена
   повторите эту команду, чтобы процессы перечитали секреты.
4. Проверьте `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 logs`.
   Успешное подключение отмечено строкой
   `MAX local polling connected; waiting for a private bot conversation.`
5. Откройте своего бота в MAX в личном диалоге и нажмите «Начать».
   При каждом принятом событии появится `Durably accepted updates: ...`.

Публичный адрес не нужен для этого локального режима: адаптер получает события
через [GET /updates](https://dev.max.ru/docs-api/methods/GET/updates) и передаёт
их обычному защищённому HTTP inbox. Полученный пакет временно зашифрован на диске;
курсор продвигается после подтверждённой записи всего пакета в БД. Повторная
доставка после перезапуска обрабатывается существующей дедупликацией inbox.
Содержимое событий и токен не попадают в логи.

Если API сообщает `webhook_already_registered`, у этого бота уже есть webhook.
Адаптер не удаляет чужую настройку: используйте прежний HTTPS endpoint или
отдельного тестового бота. На один токен должен работать один получатель событий.

Long Polling предназначен для разработки и тестов и ограничен самим MAX.
При первом запросе без курсора MAX отдаёт только последнее обновление, поэтому
начинайте тестовый диалог после подключения. Для постоянного размещения нужен
HTTPS webhook по [MAX.md](integrations/MAX.md).

## Проверка проекта и управление

```powershell
.\start.ps1 status
.\start.ps1 logs
.\start.ps1 demo
.\start.ps1 stop
.\start.ps1 -Offline
```

Если политика PowerShell блокирует запуск, добавьте перед скриптом
`powershell -NoProfile -ExecutionPolicy Bypass -File`, как в примере выше.
`demo` проходит обе ветки без сообщений в MAX и сохраняет пять документов
в `var\demo-downloads`. `stop` сохраняет данные. `-Offline` останавливает
приём событий MAX и запускает три основных сервиса; уже поставленные в очередь
задачи worker продолжает обрабатывать.

Чтобы выключить и сам Docker Desktop, сначала выполните `start.ps1 stop`,
затем `docker desktop stop`. Последняя команда останавливает Docker целиком,
включая контейнеры других проектов. Тома и секреты сохраняются.
При следующем запуске `start.ps1` снова откроет Docker Desktop.

Другие пользователи могут общаться с ботом через MAX удалённо, пока ноутбук
включён, подключён к интернету и Docker работает. Сон, выключение ноутбука
или остановка Docker делают бота недоступным. Для проверки в любое время
подготовьте постоянно работающий сервер и HTTPS webhook.

Сервер доступен на <http://127.0.0.1:8080/health/live> и возвращает
`{"status":"live"}`. Отдельного сайта в этом проекте нет — интерфейс находится
в диалоге MAX. Корень `/` возвращает 404, это ожидаемо.

`/health/ready` и `tsr health` в локальном polling-режиме продолжают сообщать
`degraded` / `webhook_unconfigured`: существующая проверка готовности требует
production webhook. Состояние локального подключения проверяйте по логам
`polling`, а обработку — по ответам бота. Без токена причина `max_unconfigured`.

## TLS: дополнительный CA для MAX

Для API `platform-api2.max.ru` понадобились CA Минцифры. Официальные PEM скачаны
с `https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt` и
`https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt` по проверенному
HTTPS. Эти адреса также приведены в
[официальной инструкции RuStore](https://www.rustore.ru/help/developers/monetization/payment-callback/Preparing-the-server-for-RuStore%20API).

Для локального запуска они добавляются к штатным Linux CA в
`var\ca\ca-bundle.pem`. Локальный файл
`compose.local-ca.yaml` монтирует bundle только в контейнеры этого проекта;
`start.ps1` подключает его автоматически. Проверка цепочки сертификата и имени
сервера включена. Системное хранилище Windows не меняется. Bundle, локальный
override, токены и `.env` исключены из Git и Docker build context.

Контрольные SHA-256 скачанных PEM:

```text
root: 936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504
sub:  f0ae589f36774f29ef3648f7984b08d42fcce6f1ffeeb6236d773daeb2744ea6
```

После сборки образа (`start.ps1 -Offline`) подготовьте bundle из PowerShell:

```powershell
New-Item -ItemType Directory -Path .\var\ca -Force | Out-Null
Invoke-WebRequest 'https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt' -OutFile .\var\ca\root.pem
Invoke-WebRequest 'https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt' -OutFile .\var\ca\sub.pem
Get-FileHash .\var\ca\root.pem, .\var\ca\sub.pem -Algorithm SHA256
```

Сверьте SHA-256 с указанными выше. При несовпадении проверьте обновление
сертификатов по официальному источнику, прежде чем использовать их.
Затем добавьте штатные корни образа и подключите пример override:

```powershell
$caContainer = docker create max-tsr-assistant:0.1.0
if ($LASTEXITCODE -ne 0) { throw 'Cannot create CA export container' }
try {
    docker cp "${caContainer}:/etc/ssl/certs/ca-certificates.crt" .\var\ca\system.pem
    if ($LASTEXITCODE -ne 0) { throw 'Cannot export system CA bundle' }
} finally {
    docker rm $caContainer | Out-Null
}
$pemParts = @('.\var\ca\system.pem', '.\var\ca\root.pem', '.\var\ca\sub.pem') | ForEach-Object { [IO.File]::ReadAllText((Resolve-Path $_)) }
[IO.File]::WriteAllText((Join-Path (Get-Location) 'var\ca\ca-bundle.pem'), ($pemParts -join [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
Copy-Item .\compose.local-ca.example.yaml .\compose.local-ca.yaml
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

Секреты, полный bundle и локальный override не входят в репозиторий.
В него включён переносимый пример `compose.local-ca.example.yaml`.
Не отключайте проверку TLS при ошибке сертификата; проверьте пути, состав
bundle и доступность официальной цепочки. Подробнее — в
[инструкции TLS](integrations/MAX.md).
