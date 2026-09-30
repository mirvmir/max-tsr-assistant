# Помощник по подбору ТСР в MAX

Диалоговый помощник для сравнения кресел-колясок по заданным параметрам, расчёта стоимости с электронным сертификатом и подготовки документов.

Пользователь подтверждает параметры, сравнивает предложения и выбирает один из двух сценариев:

- **Покупка:** карточка выбранного изделия, расчёт и вопросы поставщику в PDF.
- **Обращение:** черновик заявления в DOCX и PDF, карточка ТСР и чек-лист в PDF.

Каталог содержит **12 предложений Ortonica и Medicamarket** со ссылками на конкретные комплектации и датой снимка **30.09.2026**. Демонстрация использует учебные профили, маршрут и шаблоны с маркировкой DEMO.

[Презентация PDF](docs/presentation.pdf) · [Архитектура](docs/ARCHITECTURE.md) · [Сценарий демонстрации](docs/FIRST_SCENARIO.md)

## Быстрый запуск

Нужны Git, Python 3.12+ и Docker Compose v2. В контейнерах работают Python 3.13 и PostgreSQL 16.

```bash
git clone https://github.com/mirvmir/max-tsr-assistant.git
cd max-tsr-assistant
cp .env.example .env
PYTHONPATH=src python -c "from pathlib import Path; from tsr.bootstrap import generate_demo_secrets; generate_demo_secrets(Path('secrets'))"
docker compose up --build -d
```

Генератор сохраняет существующие секреты. Данные PostgreSQL и документы хранятся в отдельных Docker volumes.

Обе демонстрационные ветки и выгрузка документов:

```bash
docker compose exec app tsr demo --dataset public --scenario both --download-dir /tmp/tsr-demo --show-dialog
mkdir -p var/demo
docker compose cp app:/tmp/tsr-demo/. var/demo/
```

Локальная демонстрация использует симулятор MAX. Для подключения бота сохраните его токен в `secrets/max_bot_token` и выполните [инструкцию MAX](docs/integrations/MAX.md).

**Windows:**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -Offline
```

[Подключение локального бота и настройка TLS на Windows](docs/WINDOWS_START.md).

## Проверка

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m pip install pytest==8.4.1 pgserver==0.1.4
.venv/bin/tsr validate-data --manifest data/releases/real-1.1.0/manifest.json
TSR_TEST_DATABASE_URL=postgresql://tsr:tsr-local-demo@127.0.0.1:5432/tsr .venv/bin/python -m pytest -q
```

Для полного прогона укажите отдельный PostgreSQL 16 с правом создавать тестовые схемы. Тесты используют схемы со случайными именами. Compose оставляет БД во внутренней сети. Без `TSR_TEST_DATABASE_URL` интеграционные тесты пропускаются.

## Файлы проекта

| Материал | Расположение |
| --- | --- |
| Исходный код и тесты | [src/tsr](src/tsr), [tests](tests) |
| Конфигурация и контейнеры | [.env.example](.env.example), [Dockerfile](Dockerfile), [compose.yaml](compose.yaml) |
| Миграции PostgreSQL | [migrations](migrations) |
| Архитектура и эксплуатация | [ARCHITECTURE.md](docs/ARCHITECTURE.md), [RUNBOOK.md](docs/RUNBOOK.md) |
| Технический HTTP API | [OpenAPI 3.1](docs/api/openapi.json), [DATA-API.yaml](docs/api/DATA-API.yaml) |
| Каталог и источники | [catalog.json](data/catalog/mvp/1.1.0/catalog.json), [описание данных](docs/data/REAL_CATALOG.md) |
| Демо-профили и эталоны | [data/demo](data/demo), [data/golden](data/golden) |
| Шаблоны документов | [templates](templates) |
| Презентация | [PDF](docs/presentation.pdf), [PPTX](docs/presentation.pptx) |
| Комплект передачи | [docs/submission/checklist.md](docs/submission/checklist.md) |

Версия комплекта: `v0.1.0-hackathon`. Команда `git rev-parse HEAD` показывает точный commit локальной копии.
