"""Pure Russian conversation views: no database, clocks or ID generation."""
from __future__ import annotations
from tsr.contracts import (
    ActionIntent, ActionSpec, CaseGuard, ChooseBranchPayload, CompareOffersPayload,
    ConfirmCandidatePayload, ConfirmResultPayload, DeleteCasePayload,
    NavigatePayload, ProposeFieldPayload, RequestMaterialPayload,
    SelectOfferPayload, StartCasePayload, ViewDraft, ViewModel, ViewSection,
)

FIELD_LABELS = {
    "seat_width":"Ширина сиденья", "max_user_mass":"Максимальная масса пользователя", "foldable":"Складная конструкция",
    "certificate_amount":"Сумма сертификата", "certificate_applicable_declared":"Применимость сертификата",
    "region_code":"Регион", "route_answers.applicant_status_declared":"Статус заявителя",
    "applicant_status_declared":"Статус заявителя", "role":"Кто обращается",
    "document_fields.applicant_name":"ФИО заявителя", "applicant_name":"ФИО заявителя",
    "document_fields.address":"Адрес заявителя", "address":"Адрес заявителя",
    "addressee":"Адресат обращения", "route":"Маршрут обращения",
    "supplier_accepts_certificate":"Продавец принимает сертификат", "supplier_has_fund_contract":"Договор продавца с фондом",
    "applicant":"Статус заявителя", "certificate":"Применимость сертификата",
    "certificate_acceptance":"Продавец принимает сертификат", "fund_contract":"Договор продавца с фондом",
}
COMPARISON_LABELS={"complete":"По указанным параметрам совпадает", "incomplete":"Нужны уточнения", "mismatch":"Есть несоответствие"}
ROUTE_LABELS={"preliminary_match":"Предварительно подходит по заявленным условиям", "blocked":"Есть препятствующее условие", "needs_clarification":"Нужны уточнения", "not_covered":"Для этого региона и категории маршрут не подготовлен"}
BUNDLE_LABELS={"preparing":"Файлы готовятся", "ready":"Полный комплект готов", "failed":"Подготовка не завершена", "historical":"Исторический комплект: сведения изменились", "expired":"Срок хранения истёк", "deleted":"Кейс удалён"}
DOCUMENT_LABELS={"purchase_card":"Карточка покупки", "application":"Заявление", "product_card":"Карточка ТСР", "checklist":"Чек-лист документов"}


def label_field(key, profile=None):
    if profile:
        rule=next((r for r in profile.fields if r.field_key==key),None)
        if rule:
            return rule.label
    return FIELD_LABELS.get(key,"Сведения для уточнения")


def value_text(value):
    if value is None or value.kind=="unknown":
        return "Не указано / не знаю"
    if value.kind=="money":
        return money_text(value.value)
    if value.kind=="boolean":
        return "Да" if value.value else "Нет"
    if value.kind=="code":
        return {"yes":"Да","no":"Нет","unknown":"Не знаю","in_stock":"В наличии","on_order":"Под заказ","out_of_stock":"Нет в наличии"}.get(value.value,value.value)
    if value.kind=="quantity":
        return value.value+" "+{"mm":"мм","kg":"кг"}[value.unit]
    if value.kind=="range":
        return value.minimum+"–"+value.maximum+" "+{"mm":"мм","kg":"кг"}[value.unit]
    if value.kind=="set":
        return ", ".join(value_text(v) for v in value.values)
    return str(value.value)


def money_text(money):
    if money is None:
        return "Неизвестно"
    major,minor=divmod(money.minor,100)
    return f"{major:,}".replace(","," ")+f",{minor:02d} ₽"


def summary_text(text,limit=80):
    return text if len(text)<=limit else text[:limit]+"… (сокращено; полный ответ в проверке)"


def page_text(text,page,size=1800):
    pages=tuple(text[start:start+size] for start in range(0,len(text),size)) or ("",)
    if page<0 or page>=len(pages):
        raise ValueError("page unavailable")
    return pages[page],len(pages)


def page_actions(case,screen,resource_id,page,count):
    g=guard_for(case);actions=[]
    for target in (page-1,page+1):
        if 0<=target<count:
            label=("Предыдущая часть" if target<page else "Далее")+f" · {target+1}/{count}"
            actions.append(action(f"{screen}_page_{target}",label,"navigate",NavigatePayload(destination="resume",screen=screen,resource_id=resource_id,page=target),g))
    return tuple(actions)


def guard_for(case):
    return CaseGuard(case_id=case.case_id,expected_revision=case.case_revision,expected_deletion_epoch=case.deletion_epoch)


def section(text,kind="text"):
    return ViewSection(kind=kind,text_key="text",parameters={"text":text})


def action(key,label,command_type,payload,guard=None):
    return ActionIntent(action_key=key,label_key=label,command_type=command_type,typed_payload=payload,required_guard=guard)


def navigation(guard):
    return tuple(action(v,v,"navigate",NavigatePayload(destination=v),guard) for v in ("back","resume","materials","help")) + (
        action("cases","Мои кейсы","navigate",NavigatePayload(destination="cases",screen="cases"),guard),
        action("new_case","Новый кейс с актуальным каталогом","navigate",NavigatePayload(destination="new_case"),guard),
        action("delete_prompt","delete","navigate",NavigatePayload(destination="help"),guard),)


def draft(case,kind,text,actions=(),flags=()):
    return ViewDraft(kind=kind,title_key=f"{kind}.title",sections=(section(text),),actions=tuple(actions),
                     case_guard=guard_for(case) if case else None,dialog_revision=case.dialog_revision if case else None,flags=tuple(flags))


def start_view(profile_ref,help_text=None,public_catalog=False,case_mode="demo"):
    actions=(action("role_self","Для себя","start_case",StartCasePayload(category_ref=profile_ref,requested_role="self")),
             action("role_representative","Для другого человека","start_case",StartCasePayload(category_ref=profile_ref,requested_role="representative")))
    provenance=("ДЕМОНСТРАЦИЯ · вымышленные сведения пользователя; каталог — публичные снимки продавцов." if public_catalog else "ДЕМОНСТРАЦИЯ · предложения и сведения синтетические.") if case_mode=="demo" else "Техническое сравнение по указанным сведениям; не заменяет назначения."
    return draft(None,"start",help_text or provenance+"\nСравним точные комплектации и подготовим материалы. Кто обращается?",actions+(action("cases","Мои кейсы","navigate",NavigatePayload(destination="cases",screen="cases")),),("synthetic_demo",) if case_mode=="demo" else ())


def input_view(case,rule,current=None):
    text=rule.label+"\n"+rule.help_text
    if rule.unit:
        text += "\nВведите значение в "+{"mm":"мм","kg":"кг"}[rule.unit]+"."
    if current is not None:
        text += f"\nСохранено: {current}. Новый ответ потребуется подтвердить."
    g=guard_for(case)
    actions=(action("unknown","unknown","propose_field",ProposeFieldPayload(field_key=rule.field_key,unknown=True),g),)
    if rule.value_kind in ("boolean","code"):
        actions=tuple(action("answer_"+answer,"Да" if answer=="yes" else "Нет","propose_field",ProposeFieldPayload(field_key=rule.field_key,raw_text=answer),g) for answer in ("yes","no"))+actions
    elif rule.field_key=="region_code":
        actions=(action("demo_region","Учебный регион","propose_field",ProposeFieldPayload(field_key=rule.field_key,raw_text="ru-alt"),g),)+actions
    return draft(case,"input",text,actions+navigation(g),("synthetic_demo",))


def candidate_view(case,candidate,label,page=0):
    g=guard_for(case)
    value="Учебный регион" if candidate.field_key=="region_code" and getattr(candidate.value,"value",None)=="ru-alt" else value_text(candidate.value)
    text,count=page_text(label+": "+value,page,1600)
    actions=page_actions(case,"candidate",candidate.candidate_id,page,count)
    if page==count-1:
        actions += (action("confirm_candidate","confirm","confirm_candidate",ConfirmCandidatePayload(candidate_id=candidate.candidate_id,candidate_hash=candidate.value_hash),g),)
    actions += (action("edit","edit","navigate",NavigatePayload(destination="back"),g),)
    detail=f"Часть {page+1}/{count}. Полный ответ сохраняется без сокращений после подтверждения." if count>1 else "Подтверждение сохранит этот ответ."
    return draft(case,"candidate",text+"\n"+detail,actions,("synthetic_demo",))


def pricing_lines(quote):
    conditional=quote.certificate_use=="conditional"
    coverage_label="Условное покрытие сертификатом" if conditional else "Покрытие сертификатом"
    gap_label="Условная разница без доставки" if conditional else "Доплата за изделие"
    lines=["Цена комплектации: "+money_text(quote.price),"Лимит сертификата: "+money_text(quote.certificate_limit),
           coverage_label+": "+money_text(quote.coverage),gap_label+": "+money_text(quote.gap)]
    cert={"allowed_by_declared_data":"Применимость по заявленным сведениям; условия нужно проверить у продавца",
          "conditional":"Расчёт условный: применимость сертификата требует уточнения", "not_accepted":"Продавец не принимает сертификат", "not_applicable":"Сертификат неприменим", "unknown":"Применимость сертификата неизвестна"}
    lines.append(cert[quote.certificate_use])
    delivery=quote.delivery
    if delivery.mode=="included":
        lines.append("Доставка включена; дополнительный платёж 0 ₽")
    elif delivery.mode=="separate":
        charge=delivery.charge.value.value if delivery.charge.status=="known" else None
        lines.append("Доставка оплачивается отдельно: "+money_text(charge))
    elif delivery.mode=="conflicting":
        lines.append("Условия доставки противоречат друг другу; уточните у продавца")
    else:
        lines.append("Условия и стоимость доставки неизвестны")
    lines.append(("Условные собственные расходы: " if conditional else "Возможные собственные расходы: ")+money_text(quote.possible_own_total))
    if "delivery.paid_by_user" in quote.assumptions:
        lines.append("Отдельная доставка включена в расчёт условно, если вы оплачиваете её самостоятельно.")
    return lines


def supplier_question_text(question,profile=None):
    texts={"parameter":"Уточните у продавца: "+label_field(question.field_key,profile),
           "price":"Уточните точную цену этой комплектации", "delivery":"Уточните условия и стоимость доставки",
           "certificate":"Уточните, принимает ли продавец сертификат", "fund_contract":"Уточните наличие договора с фондом",
           "availability":"Уточните наличие этой комплектации"}
    return texts[question.category]


def build_comparison_view(comparison_set,case=None,offers=(),suppliers=(),profile=None,sources=(),catalog_page=None,catalog_count=1,catalog_version=None):
    sections=[];actions=[];g=comparison_set.case_guard
    for number,(result,quote) in enumerate(zip(comparison_set.items,comparison_set.quotes),1):
        offer=next((o for o in offers if o.snapshot_id==result.snapshot_id),None)
        heading=f"Вариант {number}"
        lines=[]
        if offer:
            heading=f"{number}. {offer.variant.model} · {offer.variant.configuration}"
            supplier=next((s for s in suppliers if s.supplier_id==offer.supplier_id),None)
            lines.extend((summary_text(heading,100),"Продавец: "+summary_text(supplier.display_name if supplier else "продавец не указан",80)))
            if offer.price_kind=="from":
                lines.append("Наблюдаемая цена: от "+money_text(offer.price.value.value if offer.price.status=="known" else None)+"; нижняя граница, не точная цена комплектации")
        else:
            lines.append(heading)
        lines.append(COMPARISON_LABELS[result.classification])
        labels={"match":"совпадает","mismatch":"не совпадает","unknown_offer":"неизвестно у продавца","unspecified_input":"не указано пользователем"}
        for field in result.fields:
            actual=summary_text(value_text(field.offer_fact.value)) if field.offer_fact.status=="known" else "Неизвестно" if field.offer_fact.status=="unknown" else "Противоречивые сведения"
            lines.append(f"{label_field(field.field_key,profile)}: нужно {summary_text(value_text(field.required_value))}; у продавца {actual} — {labels[field.status]}")
        lines.extend(pricing_lines(quote))
        from tsr.domain.matching import supplier_questions
        lines.extend(supplier_question_text(q,profile) for q in supplier_questions(result,quote))
        if offer:
            resolved=tuple(source for source in sources if source.source_id in offer.source_ids)
            titles=tuple(source.title if len(source.title)<=72 else source.title[:72]+"… (название сокращено)" for source in resolved[:2])
            lines.append("Источник: "+(", ".join(titles)+("; есть дополнительные источники" if len(resolved)>2 else "") if titles else "не указан; уточните у продавца"))
            lines.append(("Дата публичного снимка: " if offer.data_kind=="public_snapshot" else "Дата синтетического снимка: ")+offer.observed_at.strftime("%d.%m.%Y"))
            if offer.data_kind=="public_snapshot":
                lines.append("Публичные сведения продавца. Техническое сравнение не заменяет назначения.")
        sections.append(section("\n".join(lines),"comparison"))
        actions.append(action(f"select_{result.snapshot_id}",f"Выбрать {number}: {summary_text(offer.variant.model,60) if offer else 'вариант'}","select_offer",
                              SelectOfferPayload(snapshot_id=result.snapshot_id,comparison_id=result.comparison_id),g))
    if catalog_page is not None:
        provenance="ДЕМОНСТРАЦИЯ: данные пользователя вымышленные; происхождение предложений указано отдельно." if case is None or case.mode=="demo" else "Техническое сравнение не заменяет назначения."
        version=f" Версия каталога: {catalog_version}." if catalog_version else ""
        sections.insert(0,section(f"Каталог · страница {catalog_page+1}/{catalog_count}."+version+" "+provenance+" Сначала совпадения по указанным параметрам, затем варианты для уточнения и несоответствия; точная цена сравнивается внутри группы."))
        for target in (catalog_page-1,catalog_page+1):
            if 0<=target<catalog_count:
                actions.append(action(f"catalog_page_{target}",("Следующие предложения" if target>catalog_page else "Предыдущие предложения")+f" · {target+1}/{catalog_count}","navigate",NavigatePayload(destination="resume",screen="catalog",page=target),g))
    return ViewDraft(kind="comparison",title_key="comparison.title",sections=tuple(sections),actions=tuple(actions)+navigation(g),
                     case_guard=g,dialog_revision=case.dialog_revision if case else None,flags=("synthetic_demo",) if case is None or case.mode=="demo" else ())


def branch_view(case):
    g=guard_for(case)
    actions=tuple(action(branch,branch,"choose_branch",ChooseBranchPayload(branch=branch),g) for branch in ("purchase","support"))
    text="Выберите нужный результат. "+("В демонстрации используйте вымышленные сведения пользователя; происхождение предложения показано отдельно." if case.mode=="demo" else "Техническое сравнение не заменяет назначения.")
    return draft(case,"branch",text,actions+navigation(g),("synthetic_demo",) if case.mode=="demo" else ())


def preview_view(case,preview,profile=None,page=0):
    c=preview.proposed_content;i=c.input_revision
    provenance=("ДЕМОНСТРАЦИЯ · вымышленные сведения пользователя; публичные сведения продавца" if c.offer_snapshot.data_kind=="public_snapshot" else "ДЕМОНСТРАЦИЯ · синтетические сведения") if c.case_mode=="demo" else "Техническое сравнение не заменяет назначения."
    lines=[provenance,c.offer_snapshot.variant.model+" · "+c.offer_snapshot.variant.configuration,
           "Результат: "+("карточка покупки PDF" if c.branch=="purchase" else "заявление DOCX и PDF, карточка ТСР PDF, чек-лист PDF"),
           "Кто обращается: "+("для себя" if i.role=="self" else "представитель")]
    if c.supplier:
        lines.append("Продавец: "+c.supplier.display_name)
        for contact in c.supplier.contacts:
            lines.append("Контакт продавца: "+(value_text(contact.value) if contact.status=="known" else "Неизвестно"))
    if c.catalog_ref:
        lines.append("Версия каталога: "+c.catalog_ref.version)
    for key,value in i.prescribed.items():
        lines.append(label_field(key,profile)+": "+value_text(value))
    for key in i.unspecified_fields:
        lines.append(label_field(key,profile)+": Не указано")
    lines.append(COMPARISON_LABELS[c.comparison.classification])
    if c.offer_snapshot.price_kind=="from":
        lines.append("Наблюдаемая цена: от "+money_text(c.offer_snapshot.price.value.value if c.offer_snapshot.price.status=="known" else None)+"; нижняя граница, не точная цена")
    lines.extend(pricing_lines(c.pricing))
    if c.route:
        lines.extend(("Регион: "+("Учебный регион" if i.region_code=="ru-alt" else i.region_code or "Не указан"),"Маршрут: "+ROUTE_LABELS[c.route.status],
                      "Это модель маршрута. Решение фонда не обещается."))
        for condition in c.route.conditions:
            lines.append(label_field(condition.condition_id)+": "+{"met":"условие выполнено по заявленным данным","not_met":"условие не выполнено","unknown":"неизвестно"}[condition.status])
        lines.append("Адресат: "+(value_text(c.route.addressee.value) if c.route.addressee.status=="known" else "Неизвестно"))
        for item in c.route.checklist:
            lines.append("Документ: "+item.label)
        for key,value in i.document_fields.items():
            lines.append(label_field(key)+": "+value)
    if c.missing_fields:
        lines.append("Не заполнено: "+", ".join(dict.fromkeys(label_field(key,profile) for key in c.missing_fields)))
    for source in c.sources:
        if source.source_id in c.offer_snapshot.source_ids:
            lines.append("Источник: "+source.title+(" · "+source.url if source.url else ""))
    lines.append(("Дата публичного снимка: " if c.offer_snapshot.data_kind=="public_snapshot" else "Дата синтетического снимка: ")+c.offer_snapshot.observed_at.strftime("%d.%m.%Y"))
    lines.append("Подтверждаете именно этот набор сведений, включая явно незаполненные поля?")
    g=guard_for(case)
    text,count=page_text("\n".join(lines),page)
    actions=page_actions(case,"review",preview.preview_id,page,count)
    if page==count-1:
        actions += (action("confirm_result","prepare","confirm_result",ConfirmResultPayload(preview_id=preview.preview_id,manifest_hash=preview.manifest_hash),g),)
    if count>1:
        text=f"Проверка результата · часть {page+1}/{count}. Полные сведения показаны по частям.\n"+text
    return draft(case,"review",text,actions+navigation(g),c.flags)


def materials_view(case,bundles,artifacts,now,historical_artifact_ids=(),page=0,total_artifacts=None):
    g=guard_for(case);sections=[];actions=[]
    for bundle in sorted(bundles,key=lambda b:b.created_at,reverse=True)[:3]:
        sections.append(section(BUNDLE_LABELS[bundle.status],"status"))
    ordered=tuple(artifacts) if total_artifacts is not None else tuple(sorted(artifacts,key=lambda a:(a.published_at,a.document_kind,a.format,str(a.artifact_id)),reverse=True))
    total=len(ordered) if total_artifacts is None else total_artifacts
    count=max(1,(total+7)//8)
    if page<0 or page>=count:
        raise ValueError("material page unavailable")
    if count>1:
        sections.append(section(f"Файлы · страница {page+1}/{count}. Всего файлов: {total}"))
    shown=ordered if total_artifacts is not None else ordered[page*8:(page+1)*8]
    for artifact in shown:
        historical=artifact.case_revision!=case.case_revision or artifact.artifact_id in historical_artifact_ids
        expired=artifact.expires_at<=now
        label=DOCUMENT_LABELS[artifact.document_kind]+" "+artifact.format.upper()
        description=label
        if expired:
            description += " · срок хранения истёк"
        elif historical:
            description += " · ИСТОРИЧЕСКИЙ: сведения изменились; файл не является актуальным. Нажимая кнопку, вы подтверждаете это предупреждение."
        sections.append(section(description,"material"))
        if not expired:
            disposition="historical" if historical else "current"
            button=("Исторический: " if historical else "Получить: ")+label
            actions.append(action("material_"+str(artifact.artifact_id),button,"request_material",RequestMaterialPayload(artifact_id=artifact.artifact_id,disposition=disposition),g))
    if not sections:
        sections.append(section("Материалов пока нет. Продолжите кейс и подтвердите результат."))
    for target in (page-1,page+1):
        if 0<=target<count:
            actions.append(action(f"materials_page_{target}","Следующие файлы" if target>page else "Предыдущие файлы","navigate",NavigatePayload(destination="materials",screen="materials",page=target),g))
    return ViewDraft(kind="materials",title_key="materials.title",sections=tuple(sections),actions=tuple(actions)+navigation(g),case_guard=g,dialog_revision=case.dialog_revision,flags=("synthetic_demo",))


def build_error_view(error,resume_action=None):
    return ViewDraft(kind="error",title_key="error.title",sections=(ViewSection(kind="text",text_key=error.safe_message_key,parameters={}),),
                     actions=() if resume_action is None else (resume_action,),case_guard=None,dialog_revision=None,flags=())


def bind_action_handles(view_id,draft,handles,expires_at):
    return ViewModel(view_id=view_id,kind=draft.kind,title_key=draft.title_key,sections=draft.sections,
                     actions=tuple(ActionSpec(label_key=a.label_key,action_handle=h.handle,expires_at=expires_at) for a,h in zip(draft.actions,handles)),
                     case_guard=draft.case_guard,dialog_revision=draft.dialog_revision,flags=draft.flags)


def edit_view(case,revision,profile,offers=()):
    """Offer explicit field editing; choosing a field never saves a new value."""
    from tsr.contracts import MoneyValue, CodeValue, TextValue
    g=guard_for(case)
    values={**dict(revision.prescribed),**{key:None for key in revision.unspecified_fields},
            "certificate_amount":MoneyValue(value=revision.certificate_amount) if revision.certificate_amount else None,
            "certificate_applicable_declared":CodeValue(value=revision.certificate_applicable_declared)}
    if case.branch=="support":
        values.update({"region_code":TextValue(value=revision.region_code) if revision.region_code else None})
        values.update({"route_answers."+key:CodeValue(value=value) for key,value in revision.route_answers.items()})
        values.update({"document_fields."+key:TextValue(value=value) for key,value in revision.document_fields.items()})
    actions=[];lines=["Выберите ответ для изменения. Новое значение сохранится после подтверждения."]
    for key,value in values.items():
        label=label_field(key,profile)
        lines.append(label+": "+summary_text(value_text(value)))
        actions.append(action("edit_field_"+key,"Изменить: "+label,"propose_field",ProposeFieldPayload(field_key=key,unknown=True),g))
    if offers:
        actions.append(action("change_offer","Сменить предложение","navigate",NavigatePayload(destination="resume",screen="catalog"),g))
    if case.selected_snapshot_id is not None:
        actions.append(action("change_branch","Сменить результат","navigate",NavigatePayload(destination="back"),g))
    return draft(case,"input","\n".join(lines),tuple(actions)+navigation(g),("synthetic_demo",))


def resolve_intent(event,case,resolved_handle,now):
    from tsr.contracts import CommandDraft, Result
    if event.kind=="callback":
        if resolved_handle is None or resolved_handle.owner_id!=event.owner_id:
            return Result.failure("ACCESS_DENIED")
        if resolved_handle.expires_at<=now:
            return Result.failure("STALE_REVISION")
        if case and (resolved_handle.case_guard!=guard_for(case) or resolved_handle.dialog_revision!=case.dialog_revision):
            return Result.failure("STALE_REVISION")
        return Result.success(CommandDraft(type=resolved_handle.action.command_type,payload=resolved_handle.action.typed_payload,required_guard=resolved_handle.action.required_guard))
    if event.kind=="text" and case:
        directions={"назад":"back","продолжить":"resume","помощь":"help","материалы":"materials"}
        destination=directions.get(event.payload.text.strip().lower())
        if destination:
            return Result.success(CommandDraft(type="navigate",payload=NavigatePayload(destination=destination),required_guard=guard_for(case)))
    return Result.failure("INVALID_TRANSITION")


def next_step(case,completeness,domain_result=None):
    from tsr.contracts import StepDecision
    missing=tuple(completeness)
    if missing:
        step="route" if case.branch=="support" else "input"
    elif case.selected_snapshot_id is None:
        step="comparison"
    elif case.branch is None:
        step="branch"
    elif case.active_confirmation_id:
        step="materials" if case.step=="materials" else "preparing"
    else:
        step="review"
    return StepDecision(step=step,required_fields=missing,available_actions=("back","resume","materials","help"))


def build_view(case,domain_result,locale="ru"):
    """Dispatch typed pure results; application supplies prompt/storage context."""
    from tsr.contracts import Candidate, ComparisonSet, ResultPreview
    if locale!="ru":
        raise ValueError("only Russian copy is available")
    if isinstance(domain_result,Candidate):
        return candidate_view(case,domain_result,label_field(domain_result.field_key))
    if isinstance(domain_result,ComparisonSet):
        return build_comparison_view(domain_result,case)
    if isinstance(domain_result,ResultPreview):
        return preview_view(case,domain_result)
    if case.branch is None and case.selected_snapshot_id is not None:
        return branch_view(case)
    return draft(case,"input","Продолжите с текущего вопроса.",navigation(guard_for(case)))
