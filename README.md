# max-tsr-assistant

Диалоговый помощник MAX для технического подбора ТСР: карточка покупки и предварительная проверка маршрута с пакетом черновиков. Новый каталог содержит **12 реальных предложений Ortonica и Medicamarket** с источниками и датой наблюдения. Ввод пользователя, правила технической сверки, маршрут и неофициальные шаблоны остаются синтетической demo-моделью. Каждый результат помечен DEMO; медицинская пригодность и пилот ещё требуют предметного утверждения.

Python-монолит, PostgreSQL 16, одна кодовая база и образ для HTTP и worker. PostgreSQL хранит inbox/jobs/outbox, подтверждённый ввод и неизменяемые manifest. Один дочерний процесс `spawn` готовит документы; файлы и чувствительные записи хранятся зашифрованно. UI — русский диалог MAX с подтверждением значений, выбором роли, возвратом, исправлением и материалами.

## Первый запуск

**Windows / локальный бот без домена:** [готовая команда запуска и подключение MAX](docs/WINDOWS_START.md).
Первый запуск: `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -Offline`.
Затем сохраните токен в `secrets/max_bot_token` и повторите команду без `-Offline`.
Если MAX требует дополнительный CA, подготовьте локальный bundle по инструкции.

Нужны Git, Python 3.12+ для создания локальных секретов и Docker Compose v2. Образ использует Python 3.13. Из корня репозитория:

```bash
cp .env.example .env
PYTHONPATH=src python -c "from pathlib import Path; from tsr.bootstrap import generate_demo_secrets; generate_demo_secrets(Path('secrets'))"
docker compose up --build -d
docker compose exec app tsr demo --scenario purchase --download-dir /tmp/tsr-purchase --show-dialog
mkdir -p var/purchase
docker compose cp app:/tmp/tsr-purchase/. var/purchase/
```

По умолчанию команда использует реальный каталог: Ortonica Base 200, SKU 5048, ширина 405 мм, цена **14 500 ₽**. При заявленном сертификате 10 000 ₽ ожидается условная разница **4 500 ₽** (`450 000` копеек), `ready` и один PDF. Приём сертификата для заказа и доставка неизвестны; окончательная сумма с доставкой отсутствует. Команда проводит синтетического пользователя через application, PostgreSQL, очереди, worker, рендер и авторизованную выдачу. Локальный transport имитирует MAX, токен бота не нужен. Demo использует отдельную область кейсов, очередей и active release.

Старый воспроизводимый набор сохранён: `tsr demo --dataset synthetic --scenario both`. Он ожидает прежнюю разницу 20 000 ₽. Для нового набора можно явно указать `--dataset public`.

[Подробная проверка первого сценария и второй ветки](docs/FIRST_SCENARIO.md). [Запуск, секреты и эксплуатация](docs/RUNBOOK.md). [Что реализовано и что осталось](docs/IMPLEMENTATION_STATUS.md). [API MAX и TLS](docs/integrations/MAX.md).

## Проверки

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m pip install pytest==8.4.1 pgserver==0.1.4
TSR_TEST_DATABASE_URL=postgresql://tsr:tsr-local-demo@127.0.0.1:5432/tsr .venv/bin/python -m pytest -q
```

Здесь нужен отдельный локальный PostgreSQL с правом создавать тестовые схемы. Compose не публикует порт БД; для него можно выполнять проверки внутри контейнера с тестовыми зависимостями. Без `TSR_TEST_DATABASE_URL` интеграционные тесты пропускаются — такой запуск не подтверждает сквозной сценарий. Тесты создают и удаляют только свои схемы со случайными именами.

Проверки покрывают реальные SKU и единицы, деньги и неизвестности, весь каталог, старые/чужие кнопки, preview/manifest hash, отзыв отдельных пакетов, owner/epoch/fence, неопределённую доставку, таймаут рендера, retention и backup/restore. Фактические результаты сквозного сценария, независимого ревью и нагрузочного прогона приводятся в отчёте проверки; локальные проверки не заменяют живые клиенты MAX.

Исходные требования и контракты сохранены в [docs/specs](docs/specs). Их первоначальные чекбоксы оставлены без изменений; актуальный статус реализации описан отдельно.

Нормализованный [catalog.json](data/catalog/mvp/1.1.0/catalog.json), [описание источников и неоднозначностей](docs/data/REAL_CATALOG.md), [независимая сверка публичных фактов](docs/data/FACTUAL_AUDIT.md) и [повторный аудит требований](docs/verification/requirements-audit-2026-09-30.md). Review публичных фактов действует семь дней; непроверенный или просроченный public snapshot не активируется общим demo-флагом.
