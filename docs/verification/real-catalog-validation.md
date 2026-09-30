# Финальный протокол повторной проверки

30 сентября 2026 года. Проверенный код и данные: local commit
`13dd1190755fc3d23a9e36b909de22a04c3fc946`, tree
`c391fac6fa5e1af02c5c26e5f5347ef489ca66ec`. Последующий commit добавляет только
протоколы и результаты QA. Основание — исходные contracts, implementation plan и
data tasks в `docs/specs`; исходные чекбоксы не переписаны.

Тот же code/data tree выгружен в GitHub commit
[`16ae327`](https://github.com/mirvmir/max-tsr-assistant/commit/16ae327496dc0810d32129b531d6400eadde821a).
Tree SHA сравнен до перемещения ветки и совпал полностью, включая прежние fonts.

Полная матрица 70 задач реализации и 14 задач данных, независимые probes и
остатки: [real-catalog-review.md](real-catalog-review.md). Итог относится к первой
локальной mixed demo реализации, не к принятию всего MVP или пилота.

## Реальные данные

В [catalog.json](../../data/catalog/mvp/1.1.0/catalog.json) — **12 реальных
предложений: 6 Ortonica + 6 Medicamarket**. `normalized_catalog.json` совпадает
побайтно. Варианты не размножаются из общего диапазона ширин или маркетингового
названия. Сохранены exact/from/unknown, конфликтные цены/наличие, исходные единицы,
источники и дата наблюдения. Доставка, приём сертификата для конкретного заказа и
договор с фондом неизвестны; они не превращаются в ноль или true.

Root повторно получил 12 первичных страниц и независимо проверил **151 факт**.
Результат и hashes: [FACTUAL_AUDIT.md](../data/FACTUAL_AUDIT.md),
[factual-audit.json](../data/factual-audit.json). Публичные снимки технически
reviewed этим аудитом; synthetic профиль, пользователь, маршрут и формы остались
draft. Следующая техническая проверка назначена на 7 октября 2026 года.
Автоматическое обновление источников не реализовано.

Обе команды завершились с exit 0:

```bash
tsr validate-data --manifest data/releases/real-1.1.0/manifest.json
tsr validate-data --manifest data/releases/demo-1.0.0/manifest.json
```

Первый результат: `real-catalog-demo 1.1.0`, 12 public offers, synthetic draft
model dependencies. Второй: `demo-release 1.0.0`, 5 synthetic offers. Старые data
пакеты 1.0.0, исходные спецификации и binary fonts не изменены.

## Полный набор тестов

Среда: Python 3.12.14, PostgreSQL 16.2, native TestClient и настоящий spawn child.
Каждый DB test использует собственную временную схему; backup/restore drill —
собственные временные базы. Секреты и реальные пользовательские сведения не
использовались и не выводились.

```bash
TSR_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres \
PYTHONPATH=src ../.venv/bin/python -m pytest -q
```

**162 passed, 1 warning in 41.10s**, exit 0. Единственное предупреждение — upstream
Starlette/AnyIO deprecation, не ошибка приложения. Docker-образ Python 3.13 этим
запуском не проверяется.

Проверены owner/revision/dialog/epoch/fence/TTL, сохранение ввода и pending candidate,
freeze/hash compatibility, stale handles и алгоритмы, точные template refs,
late source/snapshot revocation, current/historical material, ambiguous send,
retention, шифрование и восстановление с replay текущего deletion journal.
Проверки bounded queries считают расшифрованные DTO: страницы cases5/artifacts8,
текущие question/candidate/receipt/token lookup и максимум четыре published IDs.
Rank каталога из 1000 записей сохраняет только три показанных comparison records.

Restore drill проверяет retained ciphertext и физическое удаление кейса/файлов,
удалённых после backup. Missing/stale journal, tamper, wrong blob key и непустая
целевая база отклоняются до открытия пользовательского доступа. Это локальная
проверка процесса; независимое размещение журнала/ключей ещё требует эксплуатации.

## Установленный CLI и документы

После последней правки фактически выполнены оба dataset и обе ветки:

```bash
tsr demo --dataset public --scenario both --show-dialog --download-dir /tmp/tsr-public
tsr demo --dataset synthetic --scenario both --show-dialog --download-dir /tmp/tsr-legacy
```

Команды работали в собственной PostgreSQL области и с локальным transport, без
внешних MAX calls. CLI сам обеспечивает scope dataset; рабочий pointer не меняется.

| Dataset / ветка | Фактический результат | Preview = manifest hash |
| --- | --- | --- |
| Public / purchase | ready; 1 PDF; условная разница 450000 копеек | `efae3b2c55751750ce759a6ff1ce19da06d28df23bc6283618fd705c33b41c70` |
| Public / support | ready; DOCX + 3 PDF; та же условная разница | `56f297bb7dd6fbf864920450676304ceef115df75757d825ffc16dfcf78b3416` |
| Legacy / purchase | ready; 1 PDF; прежняя разница 2000000 копеек | `84cff1a235b6f4922e54e50167735dcd46b1707c52194e9f4887e095038221b6` |
| Legacy / support | ready; DOCX + 3 PDF; прежняя разница | `eeb2aa4d77311d4b406e907fc681fa292ef1874fdd3542d91c03fe1127e6ccd6` |

Root проверил все 10 скачанных файлов: русская demo-метка, embedded PDF fonts,
отсутствие восьми технических reason/action keys. В карточке покупки проверены код
продавца 5048, цена 14 500 ₽, условная разница 4 500 ₽, неизвестная доставка и
вопросы продавцу. Это условная арифметика модельного сертификата, не подтверждение
его приёма. `possible_own_total=null`, `certificate_use=conditional`.

Независимый reviewer отрендерил и лично просмотрел **все 26 свежих страниц**:
public 13 PDF + 3 DOCX, legacy 8 PDF + 2 DOCX. Кириллица, поля и footer читаемы;
clipping/overlap не обнаружены, PDF bbox outside_canvas=0. Root дополнительно
просмотрел purchase PDF2/3 и public DOCX1/3. Minor: последняя страница public
карточки изделия содержит только frozen hash; уплотнение provenance — дальнейшая
правка макета. Она не скрывает содержание и не влияет на расчёт.

UI проверен локально: все 12 предложений доступны на четырёх страницах, complete
стоит перед incomplete/mismatch; неизвестность объясняется, роль выбирается явно,
ввод сохраняется, pending candidate возвращается на подтверждение, старые кейсы
не переводятся молча в новый профиль. Живой mobile/web UX ещё не принят.

## Нагрузка QA05

```bash
TSR_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres \
PYTHONPATH=src ../.venv/bin/python tools/load_check.py --mode dialogs --output docs/qa/load-check.json

TSR_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres \
PYTHONPATH=src ../.venv/bin/python tools/load_check.py --mode documents --output docs/qa/document-load-check.json
```

Оба actual run завершились exit 0, `passed=true`, errors=0, external MAX calls=0.
Показатели взяты из [dialogs JSON](../qa/load-check.json) и
[documents JSON](../qa/document-load-check.json), не из предположений о ёмкости.

| Показатель | 50 диалогов | Отдельные 100 документов |
| --- | --- | --- |
| Synthetic snapshots | 1000 | 1000 |
| События/с target / actual | 5 / **5.0** | Unpaced setup; отдельная очередь |
| Завершённые диалоги | 50/50 | 40/40 setup |
| Ready / local authorized downloads | 125/125 | 100/100 |
| Render jobs до запуска burst | — | **100**, queueing 1.119s |
| Время всего / document phase | 218.790s | 79.844s / **26.197s** |
| p95 queue wait | 3120.652ms | 9384.696ms |
| p95 facade compute | 66.260ms | 57.855ms |
| p95 render compute + poll | 88.181ms | 88.208ms |
| p95 local delivery | 15.566ms | 20.013ms |
| Ошибки / guarded work rejections | 0 / 0 | 0 / 0 |

Один renderer process и один sender thread, timeout360s. Steady cadence использует
deadline независимо от compute; actual arrivals измеряются между первым и
последним steady admission, допустимое отклонение2%. Для documents worker
остановлен после setup, confirmations создают всю очередь, затем worker запускает
burst; latency samples setup сброшены. Итоговые durable counts включают setup:
dialogs1425 succeeded jobs/1300 confirmed outbox; documents1140/1040.

Граница измерения — **local application facade**, не HTTP webhook/proxy/Internet/
MAX. Upload/receipts и authorized download reads локальны. Реальная доставка,
многопроцессная распределённая quota, fairness произвольной нагрузки и ресурсная
ёмкость production хоста этими числами не доказываются.

## Незакрытые требования

R1–R6 независимого corrective review исправлены локально; проверка и матрица —
в отдельном отчёте reviewer. Весь исходный backlog остаётся шире этой реализации.
Программные остатки: indexed catalog candidate ports/полная портовая поверхность,
полный executable golden/document oracle corpus, плотность provenance в макетах,
контейнерный build/restart и комплект сдачи. Внешние/предметные остатки: экспертный
профиль, настоящий региональный маршрут/адресат/формы, pilot privacy approval,
MAX token/HTTPS/subscription/два клиента, независимое размещение backup/journal/
keys, правила сдачи и сравнительный пользовательский тест. См.
[IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md) и точные статусы reviewer.

Docker binary/daemon в среде проверки отсутствует. Ready живого бота без MAX
token остаётся degraded — это ожидаемый отказ, не successful deployment.
