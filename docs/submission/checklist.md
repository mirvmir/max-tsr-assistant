# Комплект передачи

Версия: `v0.1.0-hackathon`.

| Материал | Файл |
| --- | --- |
| Описание и запуск | [README.md](../../README.md) |
| Архитектурная схема | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| Docker и конфигурация | [Dockerfile](../../Dockerfile), [compose.yaml](../../compose.yaml), [.env.example](../../.env.example) |
| Зависимости | [pyproject.toml](../../pyproject.toml), [requirements.lock](../../requirements.lock) |
| HTTP API | [openapi.json](../api/openapi.json), [DATA-API.yaml](../api/DATA-API.yaml) |
| Данные и версии | [data](../../data), [schemas](../../schemas), [templates](../../templates) |
| Демонстрационные сценарии | [FIRST_SCENARIO.md](../FIRST_SCENARIO.md) |
| Презентация | [presentation.pdf](../presentation.pdf), [presentation.pptx](../presentation.pptx) |
| Эксплуатация | [RUNBOOK.md](../RUNBOOK.md), [MAX.md](../integrations/MAX.md), [WINDOWS_START.md](../WINDOWS_START.md) |

## Перед отправкой

- Сверить срок, канал и формат передачи с регламентом и FAQ организаторов.
- Передать ссылку на неизменяемый commit. Его SHA показывает команда `git rev-parse HEAD`.
- Выполнить чистый Docker-запуск, обе ветки демонстрации и проверку сохранности данных после перезапуска.
- Проверить обе ветки и скачивание файлов в мобильном и веб-клиенте MAX.
- Передать служебный слайд по закрытому каналу: ссылка на бота, версия, контакт, период доступности и согласованный порядок проверочного доступа.
- Сверить комплект презентации и работающий экземпляр с переданной версией.

Рабочие токены, пароли и ключи хранятся отдельно от публичного репозитория.
