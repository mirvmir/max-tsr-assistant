# Каталог кресел-колясок

Версия 1.1.0 содержит 12 конкретных предложений Ortonica и Medicamarket. Дата снимка: **30 сентября 2026 года**.

| Данные | Файл |
| --- | --- |
| Каталог | [catalog.json](../../data/catalog/mvp/1.1.0/catalog.json) |
| Нормализованная выгрузка | [normalized_catalog.json](../../data/catalog/mvp/1.1.0/normalized_catalog.json) |
| Источники | [sources.json](../../data/sources/1.1.0/sources.json) |
| Извлечённые факты | [data/evidence](../../data/evidence) |
| Версии и контрольные суммы | [manifest.json](../../data/releases/real-1.1.0/manifest.json) |

## Предложения

| Поставщик / модель | Публичный идентификатор SKU | Сиденье, мм | Максимальная нагрузка, кг | Опубликованная цена, руб. | Наличие по странице | Источник |
|---|---|---:|---:|---|---|---|
| Ortonica / Base 200 | 5048 | 405 | 130 | 14 500 (exact для выбранного SKU) | заявлено в наличии | [карточка](https://ortonica.ru/catalog/invalidnye_kolyaski/mehanicheskie_kolyaski/base-200/5053/) |
| Ortonica / Base 195 | 386 | 380 | 130 | 29 900 (exact для выбранного SKU) | заявлено в наличии | [карточка](https://ortonica.ru/catalog/invalidnye_kolyaski/mehanicheskie_kolyaski/base-190/385/) |
| Ortonica / Base 250 | 4908 | 405 | 130 | 17 900 (exact для выбранного SKU) | заявлено в наличии | [карточка](https://ortonica.ru/catalog/invalidnye_kolyaski/mehanicheskie_kolyaski/base-250/4907/) |
| Ortonica / Base 145 | 324 | 430 | 130 | 33 500 (exact для выбранного SKU) | под заказ / уже везут | [карточка](https://ortonica.ru/catalog/invalidnye_kolyaski/mehanicheskie_kolyaski/322/) |
| Ortonica / Base 300 | 5055 | 405 | 130 | 22 900 (exact для выбранного SKU) | заявлено в наличии | [карточка](https://ortonica.ru/catalog/invalidnye_kolyaski/mehanicheskie_kolyaski/5059/) |
| Ortonica / Base 350 | 4686 | 430 | 130 | 23 700 (exact для выбранного SKU) | заявлено в наличии | [карточка](https://ortonica.ru/catalog/invalidnye_kolyaski/mehanicheskie_kolyaski/4685/) |
| Медикамаркет / Base 200 | 00-00046407 | 505 | 130 | от 16 724 (без точного расчёта) | заявлено в наличии | [карточка](https://medicamarket.ru/product/kreslo_kolyaska_dlya_invalidov_ortonica_base_200_do_130kg_1) |
| Медикамаркет / Base 300 | 00-00048577 | 455 | 130 | 20 046 (exact для выбранного SKU) | заявлено в наличии | [карточка](https://medicamarket.ru/product/kreslo_kolyaska_dlya_invalidov_ortonica_base_300_do_130kg) |
| Медикамаркет / Base 350 | 00-00048705 | 455 | 130 | от 20 988 (без точного расчёта) | под заказ / уже везут | [карточка](https://medicamarket.ru/product/kreslo_kolyaska_dlya_invalidov_ortonica_base_350_do_130kg) |
| Медикамаркет / Base 400 | 00-00048697 | 405 | 130 | от 23 628 (без точного расчёта) | под заказ / уже везут | [карточка](https://medicamarket.ru/product/kreslo_kolyaska_dlya_invalidov_ortonica_base_400_do_130kg) |
| Медикамаркет / Base Lite 350 | 00-00048067 | 455 | 130 | от 29 986 (без точного расчёта) | нет в наличии / PreOrder; conflicting | [карточка](https://medicamarket.ru/product/kreslo_kolyaska_dlya_invalidov_ortonica_base_lite_350_do_130kg) |
| Медикамаркет / Base Lite 300 | 00-00048055 | 430 | 130 | 33 141 / 31 957; conflicting | под заказ / уже везут | [карточка](https://medicamarket.ru/product/kreslo_kolyaska_dlya_invalidov_ortonica_base_lite_300_do_130kg) |

## Формат

Каждое предложение связано с поставщиком, публичным SKU, комплектацией и снимком. Значимые поля содержат состояние `known`, `unknown` или `conflicting`, единицу и ссылку на источник. Ширина хранится в миллиметрах, нагрузка в килограммах, денежные значения в целых копейках.

У Ortonica данные относятся к выбранному `selectedOfferId` из `OFFERS`, у Medicamarket к опубликованному коду товара. Размер и цена привязаны к конкретной комплектации. Например, 40,5 см у SKU 5048 соответствуют 405 мм.

Виды цены: `exact`, `from`, `unknown`. Конфликт цены Base Lite 300 и конфликт наличия Base Lite 350 сохраняются в данных. Для доставки, приёма сертификата и договора с фондом используются самостоятельные поля.

## Происхождение

В [facts.json](../../data/evidence) сохранены URL, время наблюдения, locator и SHA-256 исходного ответа. [factual-audit.json](factual-audit.json) и [FACTUAL_AUDIT.md](FACTUAL_AUDIT.md) входят в пакет происхождения данных и закреплены контрольными суммами release manifest. Период повторной проверки снимков указан в `review_due_at`.

Набор [1.0.0](../../data/catalog/mvp/1.0.0/catalog.json) содержит пять синтетических предложений для воспроизводимых тестов. [Сценарий демонстрации](../FIRST_SCENARIO.md) описывает входные значения и ожидаемые документы.
