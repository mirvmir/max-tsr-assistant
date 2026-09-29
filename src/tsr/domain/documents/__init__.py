"""Pure projections of the confirmed immutable manifest into plain document content."""
from tsr.contracts import (
    ArtifactSpec, DocumentField, DocumentModel, DocumentRequest, DocumentSection,
    DocumentTable, FieldError, Result, ValidationReport, content_hash,
)


KINDS = {
    'purchase': (('purchase_card', 'pdf'),),
    'support': (('application', 'docx'), ('application', 'pdf'),
                ('product_card', 'pdf'), ('checklist', 'pdf')),
}
TEMPLATE_IDS = {
    'purchase_card': 'demo-purchase-card', 'application': 'demo-application',
    'product_card': 'demo-product-card', 'checklist': 'demo-checklist',
}
LABELS = {
    'applicant_name': 'ФИО заявителя', 'representative_name': 'ФИО представителя',
    'address': 'Адрес', 'phone': 'Телефон', 'email': 'Электронная почта',
    'region_code': 'Регион', 'certificate_amount': 'Сумма сертификата',
    'seat_width': 'Ширина сиденья', 'seat_depth': 'Глубина сиденья',
    'max_user_weight': 'Максимальная масса пользователя', 'max_user_mass': 'Максимальная масса пользователя',
    'total_width': 'Общая ширина', 'folding': 'Складная конструкция', 'foldable': 'Складная конструкция',
}
STATUS = {
    'match': 'Совпадает с указанным требованием', 'mismatch': 'Не соответствует требованию',
    'unknown_offer': 'Параметр предложения неизвестен', 'unspecified_input': 'Требование не указано',
    'complete': 'Сравнение заполнено', 'incomplete': 'Сравнение неполное',
    'calculated': 'Расчёт по указанным данным', 'conditional': 'Условный расчёт',
    'preliminary_match': 'Предварительное соответствие условиям',
    'blocked': 'Условия не выполнены', 'needs_clarification': 'Необходимо уточнение',
    'not_covered': 'Маршрут не определён', 'provided': 'Есть по данным пользователя',
    'missing': 'Нет по данным пользователя', 'unknown': 'Неизвестно',
    'required': 'Требуется по модельному маршруту', 'optional': 'При наличии',
    'not_applicable': 'Не применимо', 'allowed_by_declared_data': 'По заявленным данным',
    'not_accepted': 'Продавец не принимает сертификат',
}


def _money(value):
    if value is None:
        return 'Неизвестно'
    return f'{value.minor // 100:,}'.replace(',', ' ') + f',{value.minor % 100:02d} ₽'


def _value(value):
    if value is None:
        return 'Не указано'
    kind = getattr(value, 'kind', None)
    if kind == 'money':
        return _money(value.value)
    if kind == 'quantity':
        return f'{value.value} {value.unit}'
    if kind == 'range':
        return f'{value.minimum} - {value.maximum} {value.unit}'
    if kind == 'set':
        return ', '.join(_value(item) for item in value.values)
    if kind == 'boolean':
        return 'Да' if value.value else 'Нет'
    if kind == 'code':
        return {'in_stock':'В наличии', 'on_order':'Под заказ', 'out_of_stock':'Нет в наличии'}.get(value.value,str(value.value))
    if kind == 'text':
        return str(value.value)
    return 'Неизвестно'


def _fact(fact):
    if fact.status == 'known':
        return _value(fact.value)
    if fact.status == 'conflicting':
        return 'Противоречивые сведения'
    return 'Неизвестно'


def _field(label, value):
    return DocumentField(label=label, value=str(value))


def build_document_request(manifest):
    content = manifest.content
    specs = []
    for kind, fmt in KINDS[content.branch]:
        template = next((ref for ref in content.template_refs if ref.id == TEMPLATE_IDS[kind]), None)
        if template is None:
            raise ValueError('Frozen template reference unavailable')
        specs.append(ArtifactSpec(document_kind=kind, format=fmt, template_ref=template))
    return DocumentRequest(branch=content.branch, manifest_id=manifest.manifest_id,
                           required_artifacts=tuple(specs))


def build_document_model(manifest, document_kind):
    content = manifest.content
    if content.schema_version != '1.0.0':
        return Result.failure('UNSUPPORTED_SCHEMA')
    if manifest.manifest_hash != content_hash(content):
        return Result.failure('VALIDATION_ERROR', safe_message_key='manifest_invalid')
    if document_kind not in {kind for kind, _ in KINDS[content.branch]}:
        return Result.failure('VALIDATION_ERROR', safe_message_key='document_kind_invalid')
    inp, offer, comparison, pricing = (content.input_revision, content.offer_snapshot,
                                       content.comparison, content.pricing)
    missing = list(content.missing_fields)
    warnings = ['Сведения подтверждены пользователем и не подтверждают медицинскую пригодность изделия.']
    if content.case_mode == 'demo':
        warnings.append('ДЕМОНСТРАЦИОННЫЙ ЧЕРНОВИК. Все предложения и условия являются синтетическими.')
    if content.branch == 'support':
        warnings.append('Модельный маршрут не является утверждённым порядком обращения. '
                        'Это не официальная форма и не обещание одобрения.')
    sections = []
    if document_kind == 'application':
        route = content.route
        addressee = _fact(route.addressee) if route else 'Неизвестно'
        if not route or route.addressee.status != 'known':
            missing.append('addressee')
        document_fields = tuple(_field(LABELS.get(key, key), value if value.strip() else 'Не указано')
                                for key, value in inp.document_fields.items())
        if not inp.document_fields.get('applicant_name', '').strip():
            missing.append('applicant_name')
        sections.append(DocumentSection(
            title='Черновик обращения по ТСР',
            text='Прошу уточнить возможность предоставления технического средства реабилитации '
                 'по указанным данным. Перед подачей необходимо проверить адресата и актуальную форму.',
            fields=(_field('Адресат', addressee),
                    _field('Роль', 'Заявитель' if inp.role == 'self' else 'Представитель'),
                    _field('Регион', inp.region_code or 'Не указано')) + document_fields,
        ))
    if document_kind in ('application', 'purchase_card', 'product_card'):
        sections.append(DocumentSection(
            title='Выбранная комплектация', fields=(
                _field('Производитель', offer.variant.manufacturer), _field('Модель', offer.variant.model),
                _field('Модификация', offer.variant.modification), _field('Комплектация', offer.variant.configuration),
                _field('Артикул', offer.seller_sku), _field('Поставщик', offer.supplier_id),
                _field('Наличие', _fact(offer.availability)),
                _field('Дата снимка предложения', offer.observed_at.isoformat()),
            ),
        ))
        rows = tuple((LABELS.get(field.field_key, field.field_key), _value(field.required_value),
                      _fact(field.offer_fact), STATUS.get(field.status, field.status))
                     for field in comparison.fields)
        sections.append(DocumentSection(title='Сопоставление с указанными требованиями',
                          text=STATUS.get(comparison.classification, comparison.classification),
                          tables=(DocumentTable(headers=('Параметр', 'Требование', 'Предложение', 'Результат'), rows=rows),)))
    if document_kind in ('purchase_card', 'product_card'):
        sections.append(DocumentSection(title='Цена и расходы', fields=(
            _field('Цена комплектации', _fact(offer.price)),
            _field('Характер цены', {'exact': 'Точная по снимку', 'from': 'От указанной суммы', 'unknown': 'Неизвестно'}[offer.price_kind]),
            _field('Сумма сертификата', _money(pricing.certificate_limit)),
            _field('Применимость сертификата', STATUS.get(pricing.certificate_use, pricing.certificate_use)),
            _field('Покрытие сертификатом', _money(pricing.coverage)),
            _field('Разница без доставки', _money(pricing.gap)),
            _field('Доставка', _fact(pricing.delivery.charge)),
            _field('Условия доставки', _fact(pricing.delivery.terms)),
            _field('Возможные собственные расходы', _money(pricing.possible_own_total)),
            _field('Статус расчёта', {'calculated':'Расчёт по указанным данным', 'conditional':'Условный расчёт',
                                      'incomplete':'Неполный расчёт'}[pricing.status]),
        ), text='; '.join((*pricing.reasons, *pricing.assumptions))))
        sections.append(DocumentSection(title='Вопросы поставщику', text='\n'.join(
            LABELS.get(question.field_key, question.field_key or question.category)
            + ': требуется уточнить ' + question.category for question in comparison.questions
        ) or 'Вопросы из сравнения отсутствуют.'))
    if document_kind in ('checklist', 'application'):
        route = content.route
        if route is None:
            sections.append(DocumentSection(title='Маршрут обращения', text='Маршрут не определён. Необходимо уточнение.'))
            missing.append('route')
        else:
            sections.append(DocumentSection(title='Маршрут обращения',
                text=STATUS.get(route.status, route.status), fields=(_field('Адресат', _fact(route.addressee)),)))
            if document_kind == 'checklist':
                sections.append(DocumentSection(title='Модельный чек лист',
                    tables=(DocumentTable(headers=('Материал', 'Необходимость', 'Наличие'), rows=tuple(
                        (item.label, STATUS.get(item.applicability, item.applicability), STATUS.get(item.user_status, item.user_status))
                        for item in route.checklist)),),
                    text='Перечень синтетический. Проверьте актуальные требования у адресата.'))
            sections.append(DocumentSection(title='Следующие действия', text='\n'.join(route.next_steps) or 'Уточните актуальный порядок у адресата.'))
            missing.extend(route.missing_fields)
    versions = (
        ('Профиль', content.category_profile_ref), ('Сравнение', content.matching_algorithm_ref),
        ('Расчёт', content.pricing_algorithm_ref), ('Генератор', content.generator_ref),
    )
    fields = tuple(_field(label, f'{ref.id} {ref.version}') for label, ref in versions)
    if content.route_ref:
        fields += (_field('Маршрут', f'{content.route_ref.id} {content.route_ref.version}'),)
    fields += tuple(_field('Шаблон', f'{ref.id} {ref.version}') for ref in content.template_refs)
    fields += (_field('Версия выпуска', content.release_commit),
               _field('Ревизия данных', content.case_revision))
    sections.append(DocumentSection(title='Зафиксированные версии', fields=fields))
    return Result.success(DocumentModel(schema_version='1.0.0', document_kind=document_kind,
        case_mode=content.case_mode, sections=tuple(sections), missing_fields=tuple(dict.fromkeys(missing)),
        sources=content.sources, warnings=tuple(warnings), manifest_hash=manifest.manifest_hash))


def validate_document_model(model, template_spec):
    errors = []
    if model.schema_version != '1.0.0' or not model.sections:
        errors.append('invalid_document_content')
    for section in model.sections:
        for table in section.tables:
            if any(len(row) != len(table.headers) for row in table.rows):
                errors.append('invalid_table_shape')
    expected = getattr(template_spec, 'document_kind', model.document_kind)
    if model.document_kind != expected:
        errors.append('template_kind_mismatch')
    return ValidationReport(valid=not errors, errors=tuple(FieldError(path='document', code=code,
                            safe_message_key='document_invalid') for code in errors), warnings=model.warnings)


def inspect_rendered_output(rendered, expected_model):
    from hashlib import sha256
    errors = []
    if rendered.manifest_hash != expected_model.manifest_hash:
        errors.append('manifest_hash_mismatch')
    if sha256(rendered.bytes).hexdigest() != rendered.plaintext_sha256:
        errors.append('plaintext_hash_mismatch')
    if not rendered.bytes.startswith(b'%PDF-' if rendered.format == 'pdf' else b'PK'):
        errors.append('invalid_file_signature')
    return ValidationReport(valid=not errors, errors=tuple(FieldError(path='document', code=code,
                            safe_message_key='document_invalid') for code in errors), warnings=())
