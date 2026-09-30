"""The single semantic transaction boundary used by MAX and the CLI."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from functools import wraps
from tsr.contracts import *
from tsr.domain.cases import initialize_input, create_case, propose_value, confirm_value, select_snapshot, choose_branch, authorize_owned
from tsr.domain.matching import compare_offer, rank_comparisons
from tsr.domain.pricing import quote_purchase
from tsr.domain.routes import build_route_context, evaluate_route
from tsr.domain.conversation import (
    action, branch_view, build_comparison_view, build_error_view, candidate_view,
    draft, guard_for, input_view, materials_view, navigation, preview_view,
    bind_action_handles, start_view, edit_view, value_text,
)


def correlated(method):
    """Pure errors acquire the trusted request trace only at this boundary."""
    @wraps(method)
    def wrapped(self,*args,**kwargs):
        first=args[0] if args else kwargs.get(method.__code__.co_varnames[1])
        result=method(self,*args,**kwargs)
        subject=first.actor if isinstance(first,CommandEnvelope) else first
        trace=getattr(subject,"correlation_id",getattr(subject,"trace_id",None))
        if isinstance(result,Result) and not result.ok and trace is not None:
            return result.model_copy(update={"error":result.error.model_copy(update={"correlation_id":trace})})
        return result
    return wrapped


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)


class SystemIds:
    def new_id(self):
        return uuid4()


class Application:
    def __init__(self, db, release, settings, clock=None, ids=None):
        self.db, self.release, self.settings = db, release, settings
        self.clock, self.ids = clock or SystemClock(), ids or SystemIds()
        self.matching_ref = VersionRef(id="matching-v1", version="1.0.0")
        self.pricing_ref = VersionRef(id="pricing-v1", version="1.0.0")
        self.generator_ref = VersionRef(id="demo-generator", version="1.0.0")

    def _id(self):
        return self.ids.new_id()

    def _failure(self, code, ctx=None, field=None):
        result = Result.failure(code, field_key=field)
        if ctx:
            result = result.model_copy(update={"error": result.error.model_copy(update={"correlation_id": ctx.correlation_id})})
        return result

    def _owned_case(self, uow, ctx, guard, dialog_guard=None):
        if guard is None:
            return self._failure("INVALID_TRANSITION", ctx)
        found = uow.cases.lock_owned(ctx, guard.case_id)
        if not found.ok:
            return found
        case = found.value
        allowed = authorize_owned(ctx, case.owner_id, guard, case)
        if not allowed.ok:
            return allowed
        if dialog_guard and dialog_guard.expected_dialog_revision != case.dialog_revision:
            return self._failure("STALE_CANDIDATE", ctx)
        return Result.success(case)

    def _check_claim(self, uow, claim, now):
        return uow.work.check_claim(claim, now)

    def _policy(self, ctx):
        return EvaluationPolicy(case_mode=ctx.case_mode,
                                allow_synthetic_draft=bool(getattr(self.settings, "allow_synthetic_draft", False)))

    def _rule(self, key):
        for rule in self.release.profile.fields:
            if rule.field_key == key:
                return rule
        if key == "certificate_amount":
            return AttributeRule(field_key=key,label="Сумма электронного сертификата",value_kind="money",unit=None,operator="eq",required_for_complete_comparison=False,allowed_codes=(),tolerance=None,help_text="Введите рубли, не более двух знаков после запятой. Если сумма неизвестна — выберите «Не знаю».",source_ids=())
        if key == "certificate_applicable_declared" or key.startswith("route_answers."):
            return AttributeRule(field_key=key,label="Сертификат применим к выбранному ТСР?" if key=="certificate_applicable_declared" else "Статус заявителя подтверждён?",value_kind="code",unit=None,operator="eq",required_for_complete_comparison=False,allowed_codes=("yes","no","unknown"),tolerance=None,help_text="Выберите «Да», «Нет» или «Не знаю». Это подтверждение ваших сведений, а не решение фонда.",source_ids=())
        if key == "region_code" or key.startswith("document_fields."):
            labels = {"region_code":"Регион", "document_fields.applicant_name":"ФИО заявителя", "document_fields.address":"Адрес заявителя"}
            return AttributeRule(field_key=key,label=labels.get(key,key),value_kind="text",unit=None,operator="eq",required_for_complete_comparison=False,allowed_codes=(),tolerance=None,help_text="Выберите учебный регион. Для других регионов эта модель маршрута не подготовлена." if key=="region_code" else "Для демонстрации используйте вымышленные сведения; неизвестное можно оставить явно незаполненным.",source_ids=())
        return None

    def _answered(self, revision, key):
        return any(item.field_key == key for item in revision.confirmations)

    def _next_field(self, case, revision):
        keys = [f.field_key for f in self.release.profile.fields]
        keys += ["certificate_amount", "certificate_applicable_declared"]
        if case.branch == "support":
            keys += ["region_code", "route_answers.applicant_status_declared", "document_fields.applicant_name", "document_fields.address"]
        return next((key for key in keys if not self._answered(revision,key)), None)

    def _latest_question(self, uow, case, now):
        record=uow.outbox.latest_question(case.owner_id,case.case_id,case.dialog_revision)
        if record is None:
            return None
        view=record.payload
        for spec in view.actions:
            handle=uow.handles.find(case.owner_id,case.case_id,spec.action_handle)
            if handle and handle.action.command_type=="propose_field" and not handle.action.action_key.startswith("edit_field_"):
                return handle.action.typed_payload.field_key
        if view.kind=="candidate":
            candidate=uow.candidates.latest_for_guard(case.owner_id,guard_for(case),now,dialog_revision=case.dialog_revision)
            if candidate:
                return candidate.field_key
        return None

    def _save_case(self,uow,new_case,old_case):
        return uow.cases.save_if_revision(new_case,guard_for(old_case))

    def _invalidate(self,uow,case,now):
        uow.bundles.mark_historical(case.case_id,case.owner_id)
        uow.work.cancel_old_for_case(case.case_id,case.owner_id,case.case_revision)

    def _bind_view(self,uow,ctx,view_draft,now):
        expires = now+timedelta(seconds=self.settings.handle_ttl_seconds)
        for intent in view_draft.actions:
            payload=intent.typed_payload
            if isinstance(payload,ConfirmCandidatePayload):
                record=uow.candidates.get(payload.candidate_id)
                expires=min(expires,record.expires_at)
            elif isinstance(payload,ConfirmResultPayload):
                record=uow.previews.get(payload.preview_id)
                expires=min(expires,record.expires_at)
            elif isinstance(payload,NavigatePayload) and payload.resource_id:
                record=uow.candidates.get(payload.resource_id) if payload.screen=="candidate" else uow.previews.get(payload.resource_id)
                if record:
                    expires=min(expires,record.expires_at)
        handles=[]
        for intent in view_draft.actions:
            opaque=self._id().hex
            if intent.action_key=="confirm_delete":
                intent=intent.model_copy(update={"typed_payload":DeleteCasePayload(confirmation_handle=opaque)})
            intent_guard=intent.required_guard
            target_case=uow.cases.get(intent_guard.case_id) if intent_guard and intent_guard!=view_draft.case_guard else None
            handle = HandleRecord(handle_id=self._id(),handle=opaque,owner_id=ctx.owner_id,
                                  case_guard=intent_guard,dialog_revision=target_case.dialog_revision if target_case else view_draft.dialog_revision,
                                  action=intent,expires_at=expires)
            uow.handles.insert(handle)
            handles.append(handle)
        return bind_action_handles(self._id(),view_draft,handles,expires)

    def _outbox(self,uow,ctx,payload,guard,now,dedupe,kind="view"):
        outbox_id=self._id()
        # Payloads stay encrypted; the queue stores only an opaque outbox reference.
        kwargs={"view_ref":None,"material_permit_ref":None,"callback_answer_ref":None}
        kwargs[{"view":"view_ref","material":"material_permit_ref","callback_answer":"callback_answer_ref"}[kind]]=getattr(payload,"view_id",None) or outbox_id
        intent=DeliveryIntent(outbox_id=outbox_id,owner_id=ctx.owner_id,delivery_target_id=ctx.delivery_target_id,
                              case_guard=guard,kind=kind,dedupe_key=dedupe,created_at=now,**kwargs)
        record=OutboxRecord(outbox_id=outbox_id,owner_id=ctx.owner_id,intent=intent,status="pending",payload=payload)
        stored=uow.outbox.append_unique(record)
        work=WorkRef(job_id=self._id(),kind="deliver_outbox",payload_ref=stored.outbox_id,owner_id=ctx.owner_id,
                     case_id=guard.case_id if guard else None,case_revision=guard.expected_revision if guard else None,
                     deletion_epoch=guard.expected_deletion_epoch if guard else 0,dedupe_key="outbox:"+str(stored.outbox_id))
        uow.work.enqueue_unique(work)
        return stored.intent

    def _result(self,uow,ctx,case,view_draft,now,command_id,bundle_id=None):
        view=self._bind_view(uow,ctx,view_draft,now)
        self._outbox(uow,ctx,view,view.case_guard,now,"command:"+str(command_id))
        return Result.success(CommandResult(case=case,view=view,created_bundle_id=bundle_id,delivery_state="queued"))

    def _materials_view(self,uow,case,now,page=0):
        total=uow.artifacts.count_live_by_case(case.case_id)
        if page>=max(1,(total+7)//8):
            raise ValueError("material page unavailable")
        artifacts=uow.artifacts.page_live_by_case(case.case_id,8,page*8)
        historical=[]
        for artifact in artifacts:
            manifest=uow.manifests.get(artifact.manifest_id)
            if manifest and not self._freshness(manifest.content,now,uow).ok:
                historical.append(artifact.artifact_id)
        view=materials_view(case,uow.bundles.latest_by_case(case.case_id,3),artifacts,now,tuple(historical),page,total)
        unknown=uow.outbox.latest_unknown_by_case(case.case_id,3)
        if unknown:
            from tsr.domain.conversation import section
            warning=section("Доставка не подтверждена: сообщение или файл могли уже прийти. При повторе возможен дубликат.","status")
            retries=tuple(action("retry_"+str(record.outbox_id),f"Повторить доставку {number} · возможен дубликат","retry_delivery",RetryDeliveryPayload(outbox_id=record.outbox_id,acknowledged_possible_duplicate=True),guard_for(case))
                          for number,record in enumerate(sorted(unknown,key=lambda o:o.intent.created_at,reverse=True)[:3],1))
            view=view.model_copy(update={"sections":view.sections+(warning,),"actions":retries+view.actions})
        return view

    def _normal_view(self,uow,ctx,case,now):
        if case.profile_ref!=self.release.profile.ref:
            return Result.success(self._legacy_view(case))
        revision=uow.inputs.get(case.input_revision_id)
        candidate=uow.candidates.latest_for_guard(ctx.owner_id,guard_for(case),now)
        if candidate:
            if candidate.dialog_revision!=case.dialog_revision:
                candidate=candidate.model_copy(update={"candidate_id":self._id(),"dialog_revision":case.dialog_revision,"created_at":now,"value_hash":""})
                candidate=candidate.model_copy(update={"value_hash":content_hash(candidate)})
                uow.candidates.insert(candidate)
            return Result.success(candidate_view(case,candidate,self._rule(candidate.field_key).label))
        key=self._next_field(case,revision)
        if case.step=="preparing":
            return Result.success(draft(case,"preparing","Комплект появится в «Моих материалах». Можно вернуться позже.",navigation(guard_for(case)),("synthetic_demo",)))
        if case.step=="materials":
            return Result.success(self._materials_view(uow,case,now))
        if key:
            return Result.success(input_view(case,self._rule(key)))
        if key is None and case.active_confirmation_id:
            manifest=uow.manifests.find_by_confirmation(case.active_confirmation_id,owner_id=case.owner_id,case_id=case.case_id)
            bundle=uow.bundles.find_by_manifest(manifest.manifest_id) if manifest else None
            if bundle and bundle.status=="ready":
                return Result.success(self._materials_view(uow,case,now))
            if bundle and bundle.status=="preparing":
                return Result.success(draft(case,"preparing","Комплект появится в «Моих материалах». Можно вернуться позже.",navigation(guard_for(case)),("synthetic_demo",)))
        if case.selected_snapshot_id is None:
            g=guard_for(case)
            compare=action("compare","compare","navigate",NavigatePayload(destination="resume",screen="catalog"),g)
            return Result.success(draft(case,"comparison","Параметры сохранены. Откройте каталог и выберите предложение.",(compare,)+navigation(g),("synthetic_demo",) if case.mode=="demo" else ()))
        if case.branch is None:
            return Result.success(branch_view(case))
        preview=self._prepare_preview(uow,ctx,guard_for(case),now)
        if not preview.ok:
            return preview
        return Result.success(preview_view(case,preview.value,self.release.profile))

    def _start_view(self,text=None):
        return start_view(self.release.profile.ref,text,any(o.data_kind=="public_snapshot" for o in self.release.offers),self.settings.mode)

    def _legacy_view(self,case):
        g=guard_for(case)
        return draft(case,"help","Этот подбор создан по прежнему каталогу. Ответы и файлы сохранены.\n\nДля актуальных предложений начните новый подбор. Прежние файлы доступны с отметкой о неактуальных сведениях.",(
            action("new_case","Новый кейс с актуальным каталогом","navigate",NavigatePayload(destination="new_case"),g),
            action("materials","Мои материалы","navigate",NavigatePayload(destination="materials"),g),
            action("cases","Мои кейсы","navigate",NavigatePayload(destination="cases",screen="cases"),g)))

    def _cases_view(self,uow,ctx,page):
        from tsr.domain.conversation import section
        total=uow.cases.count_live_by_owner(ctx.owner_id)
        count=max(1,(total+4)//5)
        if page>=count:
            return self._failure("VALIDATION_ERROR",ctx)
        cases=uow.cases.page_live_by_owner(ctx.owner_id,5,page*5)
        sections=[section(f"Всего подборов: {total}"+(f" · страница {page+1}/{count}" if count>1 else ""))];actions=[]
        for n,case in enumerate(cases,page*5+1):
            label=f"Подбор {n} · {case.last_activity_at.strftime('%d.%m.%Y')}\n"+("Карточка покупки" if case.branch=="purchase" else "Обращение" if case.branch=="support" else "Заполнение параметров")
            sections.append(section(label))
            actions.extend((action(f"case_resume_{case.case_id}",f"Открыть подбор {n}","navigate",NavigatePayload(destination="resume"),guard_for(case)),action(f"case_materials_{case.case_id}",f"Файлы подбора {n}","navigate",NavigatePayload(destination="materials"),guard_for(case))))
        for target in (page-1,page+1):
            if 0<=target<count:
                actions.append(action(f"cases_page_{target}","Следующие кейсы" if target>page else "Предыдущие кейсы","navigate",NavigatePayload(destination="cases",screen="cases",page=target)))
        actions.append(action("new_case","Новый кейс с актуальным каталогом","navigate",NavigatePayload(destination="new_case")))
        return Result.success(ViewDraft(kind="help",title_key="cases.title",sections=tuple(sections),actions=tuple(actions)))

    def _comparisons(self,uow,ctx,case,ids,now):
        ready=self._release_ready(uow,case.mode,now)
        if not ready.ok:return ready
        revision=uow.inputs.get(case.input_revision_id);records=[]
        existing=uow.comparisons.find_for_guard(ctx.owner_id,guard_for(case),revision.input_revision_id,ids)
        for snapshot_id in ids:
            cached=next((r for r in existing if r.case_guard==guard_for(case) and r.result.input_revision_id==revision.input_revision_id and r.result.snapshot_id==snapshot_id),None)
            if cached:
                records.append(cached);continue
            offer=next((o for o in self.release.offers if o.snapshot_id==snapshot_id),None)
            if offer is None:return self._failure("NOT_FOUND",ctx)
            compared=compare_offer(self._id(),revision,offer,self.release.profile,self.matching_ref,now)
            quoted=quote_purchase(revision,offer,self.pricing_ref)
            if not compared.ok:return compared
            if not quoted.ok:return quoted
            record=ComparisonRecord(comparison_id=compared.value.comparison_id,owner_id=ctx.owner_id,case_id=case.case_id,case_guard=guard_for(case),result=compared.value,quote=quoted.value)
            uow.comparisons.insert(record);records.append(record)
        return Result.success(tuple(records))

    def _catalog_view(self,uow,ctx,case,now,page):
        count=max(1,(len(self.release.offers)+2)//3)
        if page>=count or not self.release.offers:return self._failure("VALIDATION_ERROR",ctx)
        ready=self._release_ready(uow,case.mode,now)
        if not ready.ok:return ready
        revision=uow.inputs.get(case.input_revision_id);results=[];quotes=[]
        # Ranking is pure over immutable inputs. Only the visible three acquire
        # persisted comparison IDs; snapshot IDs identify temporary rank entries.
        for offer in self.release.offers:
            compared=compare_offer(offer.snapshot_id,revision,offer,self.release.profile,self.matching_ref,now)
            quoted=quote_purchase(revision,offer,self.pricing_ref)
            if not compared.ok:return compared
            if not quoted.ok:return quoted
            results.append(compared.value);quotes.append(quoted.value)
        ranked=rank_comparisons(results,quotes)
        by_id={item.comparison_id:item for item in results}
        shown=tuple(by_id[id_].snapshot_id for id_ in ranked.comparison_ids[page*3:(page+1)*3])
        records=self._comparisons(uow,ctx,case,shown,now)
        if not records.ok:return records
        page_set=ComparisonSet(items=tuple(record.result for record in records.value),quotes=tuple(record.quote for record in records.value),case_guard=guard_for(case))
        return Result.success(build_comparison_view(page_set,case,self.release.offers,self.release.catalog.suppliers,self.release.profile,self.release.sources.sources,page,count,self.release.catalog.version))

    def _release_ready(self,uow,mode,now):
        record=uow.releases.get(self.release.release_ref)
        if record is None:
            # Direct pure-demo fixtures have no imported lifecycle. This exemption
            # never extends to a public snapshot or a pilot deployment.
            subjects=(self.release.manifest,self.release.profile,self.release.catalog,self.release.route,self.release.sources,self.release.templates,*self.release.offers,*self.release.sources.sources)
            if mode=="demo" and self.settings.allow_synthetic_draft and all(subject.data_kind=="synthetic" and subject.review.status=="draft" for subject in subjects):
                return Result.success(True)
            return self._failure("DATA_NOT_READY")
        if not record.packages:
            packages=tuple(ReleasePackage(kind=kind,ref=subject.ref,content=subject) for kind,subject in (
                ("CategoryProfile",self.release.profile),("CatalogPack",self.release.catalog),("RoutePack",self.release.route),
                ("SourcesRegistry",self.release.sources),("TemplateRegistry",self.release.templates)))
            record=record.model_copy(update={"packages":packages})
        lifecycles=tuple(state for ref in runtime_dependency_refs(record) if (state:=uow.releases.read_lifecycle(ref,lock=True)) is not None)
        active=uow.releases.get_active(mode,lock=True,bot_scope=self.settings.bot_scope)
        if active is None or active.ref!=self.release.release_ref:
            return self._failure("DATA_NOT_READY")
        return ensure_active_release_ready(record,lifecycles,mode,now,allow_synthetic_draft=self.settings.allow_synthetic_draft)

    def _pilot_ready(self,uow,now):
        return bool(getattr(self.settings,"reviewed_data_ready",False) and getattr(self.settings,"privacy_ready",False) and self._release_ready(uow,"pilot",now).ok)

    @correlated
    def execute_command(self,envelope):
        now=self.clock.now()
        with self.db.uow() as uow:
            result=self._execute(uow,envelope,now)
            if result.ok:
                uow.commit()
            return result

    def _execute(self,uow,envelope,now,historical_ack=False):
        if not uow.restore_ready():
            return self._failure("DATA_NOT_READY")
        ctx=envelope.actor
        p=envelope.payload
        if envelope.type=="start_case":
            exact=VersionRef(id=self.release.profile.profile_id,version=self.release.profile.version)
            if p.category_ref != exact:
                return self._failure("INCOMPATIBLE_PROFILE",ctx)
            if ctx.case_mode=="pilot" and not self._pilot_ready(uow,now):
                return self._failure("DATA_NOT_READY",ctx)
            case_id=self._id()
            revision=initialize_input(self._id(),ctx,case_id,self.release.profile,p.requested_role,now)
            case=create_case(ctx,case_id,exact,revision,now)
            uow.inputs.insert(revision)
            uow.cases.insert(case)
            uow.handles.invalidate_owner(ctx.owner_id)
            view=self._normal_view(uow,ctx,case,now)
            return self._result(uow,ctx,case,view.value,now,envelope.command_id)
        if envelope.type=="navigate" and p.destination in ("new_case","cases"):
            if envelope.case_guard:
                checked=self._owned_case(uow,ctx,envelope.case_guard,envelope.dialog_guard)
                if not checked.ok:return checked
            if p.destination=="new_case":
                return self._result(uow,ctx,None,self._start_view(),now,envelope.command_id)
            listing=self._cases_view(uow,ctx,p.page)
            if not listing.ok:return listing
            return self._result(uow,ctx,None,listing.value,now,envelope.command_id)
        if envelope.type=="navigate" and envelope.case_guard is None and p.destination=="help":
            return self._result(uow,ctx,None,self._start_view(),now,envelope.command_id)
        owned=self._owned_case(uow,ctx,envelope.case_guard,envelope.dialog_guard)
        if not owned.ok:
            return owned
        case=owned.value
        revision=uow.inputs.get(case.input_revision_id)
        if case.profile_ref!=self.release.profile.ref and not (envelope.type=="navigate" and p.destination in ("materials","resume","help")) and envelope.type not in ("request_material","delete_case"):
            return self._failure("INCOMPATIBLE_PROFILE",ctx)
        if envelope.type in ("propose_field","answer_route"):
            field_key=p.field_key
            if envelope.type=="answer_route" and not field_key.startswith("route_answers."):
                field_key="route_answers."+field_key
            rule=self._rule(field_key)
            if rule is None:
                return self._failure("VALIDATION_ERROR",ctx,field_key)
            raw="unknown" if getattr(p,"unknown",False) else getattr(p,"raw_text",None)
            if envelope.type=="answer_route":
                raw=p.answer
            if raw is None:
                return self._failure("VALIDATION_ERROR",ctx,field_key)
            # Russian responses map to stable declared codes before profile validation.
            if rule.value_kind=="code":
                raw={"да":"yes","нет":"no","не знаю":"unknown"}.get(raw.strip().lower(),raw)
            proposal=propose_value(case,self._id(),rule,UnknownValue() if getattr(p,"unknown",False) else raw,now)
            if not proposal.ok:
                return proposal
            candidate=proposal.value.model_copy(update={"expires_at":now+timedelta(seconds=self.settings.candidate_ttl_seconds)})
            updated=case.model_copy(update={"dialog_revision":candidate.dialog_revision,"step":"input","last_activity_at":now})
            saved=self._save_case(uow,updated,case)
            if not saved.ok:
                return saved
            uow.candidates.insert(candidate)
            return self._result(uow,ctx,updated,candidate_view(updated,candidate,rule.label),now,envelope.command_id)
        if envelope.type=="confirm_candidate":
            candidate=uow.candidates.get(p.candidate_id)
            if candidate is None or candidate.owner_id!=ctx.owner_id or candidate.case_id!=case.case_id:
                return self._failure("ACCESS_DENIED",ctx)
            mutation=confirm_value(case,self._id(),candidate,p.candidate_hash,revision,now)
            if not mutation.ok:
                return mutation
            updated=mutation.value.new_case
            if updated.case_revision!=case.case_revision and updated.status=="completed":
                updated=updated.model_copy(update={"status":"active"})
            uow.inputs.insert(mutation.value.new_input_revision)
            saved=self._save_case(uow,updated,case)
            if not saved.ok:
                return saved
            if updated.case_revision!=case.case_revision:
                self._invalidate(uow,updated,now)
            view=self._normal_view(uow,ctx,updated,now)
            if not view.ok:
                return view
            return self._result(uow,ctx,updated,view.value,now,envelope.command_id)
        if envelope.type=="compare_offers":
            ids=p.snapshot_ids
            if not 1<=len(ids)<=3 or len(set(ids))!=len(ids):
                return self._failure("VALIDATION_ERROR",ctx)
            compared=self._comparisons(uow,ctx,case,ids,now)
            if not compared.ok:return compared
            updated=case.model_copy(update={"step":"comparison","dialog_revision":case.dialog_revision+1,"last_activity_at":now})
            saved=self._save_case(uow,updated,case)
            if not saved.ok:return saved
            set_=ComparisonSet(items=tuple(record.result for record in compared.value),quotes=tuple(record.quote for record in compared.value),case_guard=guard_for(updated))
            return self._result(uow,ctx,updated,build_comparison_view(set_,updated,self.release.offers,self.release.catalog.suppliers,self.release.profile,self.release.sources.sources),now,envelope.command_id)
        if envelope.type=="select_offer":
            ready=self._release_ready(uow,case.mode,now)
            if not ready.ok:return ready
            record=uow.comparisons.get(p.comparison_id)
            if record is None or record.owner_id!=ctx.owner_id or record.case_id!=case.case_id:
                return self._failure("ACCESS_DENIED",ctx)
            if record.case_guard!=guard_for(case) or record.result.input_revision_id!=case.input_revision_id:
                return self._failure("STALE_REVISION",ctx)
            mutation=select_snapshot(case,record.result,record.quote,p.snapshot_id)
            if not mutation.ok:
                return mutation
            updated=mutation.value.new_case
            if updated.case_revision!=case.case_revision and updated.status=="completed":
                updated=updated.model_copy(update={"status":"active"})
            saved=self._save_case(uow,updated,case)
            if not saved.ok:
                return saved
            if updated.case_revision!=case.case_revision:
                self._invalidate(uow,updated,now)
            return self._result(uow,ctx,updated,branch_view(updated),now,envelope.command_id)
        if envelope.type=="choose_branch":
            if case.selected_snapshot_id is None:
                return self._failure("INVALID_TRANSITION",ctx)
            mutation=choose_branch(case,p.branch)
            if not mutation.ok:
                return mutation
            updated=mutation.value.new_case
            if updated.case_revision!=case.case_revision and updated.status=="completed":
                updated=updated.model_copy(update={"status":"active"})
            saved=self._save_case(uow,updated,case)
            if not saved.ok:
                return saved
            if updated.case_revision!=case.case_revision:
                self._invalidate(uow,updated,now)
            view=self._normal_view(uow,ctx,updated,now)
            if not view.ok:
                return view
            return self._result(uow,ctx,updated,view.value,now,envelope.command_id)
        if envelope.type=="confirm_result":
            confirmed=self._confirm_preview(uow,ctx,case,p,now)
            if not confirmed.ok:
                return confirmed
            updated,bundle=confirmed.value
            view=draft(updated,"preparing","Сведения подтверждены. Комплект появится в «Моих материалах».",navigation(guard_for(updated)),("synthetic_demo",))
            return self._result(uow,ctx,updated,view,now,envelope.command_id,bundle.bundle_id)
        if envelope.type=="request_material":
            intent=self._request_material(uow,ctx,guard_for(case),p.artifact_id,p.disposition,now,
                                          warning_acknowledged=historical_ack)
            if not intent.ok:
                return intent
            view=self._materials_view(uow,case,now)
            return self._result(uow,ctx,case,view,now,envelope.command_id)
        if envelope.type=="retry_delivery":
            previous=uow.outbox.get(p.outbox_id)
            if previous is None or previous.owner_id!=ctx.owner_id or previous.intent.case_guard is None or previous.intent.case_guard.case_id!=case.case_id:
                return self._failure("ACCESS_DENIED",ctx)
            if previous.status!="delivery_unknown" or not p.acknowledged_possible_duplicate:
                return self._failure("INVALID_TRANSITION",ctx)
            if previous.intent.kind=="material":
                permit=previous.payload
                if not isinstance(permit,MaterialPermit):
                    return self._failure("ACCESS_DENIED",ctx)
                resent=self._request_material(uow,ctx,guard_for(case),permit.artifact_id,permit.disposition,now,permit.warning_acknowledged)
                if not resent.ok:
                    return resent
                view=self._materials_view(uow,case,now)
            else:
                if previous.intent.case_guard!=guard_for(case):
                    return self._failure("STALE_REVISION",ctx)
                normal=self._normal_view(uow,ctx,case,now)
                if not normal.ok:
                    return normal
                view=normal.value
            return self._result(uow,ctx,case,view,now,envelope.command_id)
        if envelope.type=="navigate":
            if p.screen=="catalog":
                updated=case.model_copy(update={"dialog_revision":case.dialog_revision+1,"last_activity_at":now,"step":"comparison"})
                view=self._catalog_view(uow,ctx,updated,now,p.page)
                if not view.ok:return view
                saved=self._save_case(uow,updated,case)
                if not saved.ok:return saved
                return self._result(uow,ctx,updated,view.value,now,envelope.command_id)
            if p.screen in ("candidate","review"):
                if p.resource_id is None:
                    return self._failure("VALIDATION_ERROR",ctx)
                updated=case.model_copy(update={"dialog_revision":case.dialog_revision+1,"last_activity_at":now})
                if p.screen=="candidate":
                    original=uow.candidates.get(p.resource_id)
                    if original is None or original.owner_id!=ctx.owner_id or original.case_id!=case.case_id:
                        return self._failure("ACCESS_DENIED",ctx)
                    if original.case_guard!=guard_for(case) or original.dialog_revision!=case.dialog_revision or original.expires_at<=now:
                        return self._failure("STALE_CANDIDATE",ctx)
                    current=original.model_copy(update={"candidate_id":self._id(),"dialog_revision":updated.dialog_revision,"created_at":now,"value_hash":""})
                    current=current.model_copy(update={"value_hash":content_hash(current)})
                    try:
                        view=candidate_view(updated,current,self._rule(current.field_key).label,p.page)
                    except ValueError:
                        return self._failure("VALIDATION_ERROR",ctx)
                    uow.candidates.insert(current)
                else:
                    original=uow.previews.get(p.resource_id)
                    if original is None or original.owner_id!=ctx.owner_id:
                        return self._failure("ACCESS_DENIED",ctx)
                    if original.case_guard!=guard_for(case) or original.dialog_revision!=case.dialog_revision or original.expires_at<=now:
                        return self._failure("STALE_REVISION",ctx)
                    fresh=self._freshness(original.proposed_content,now,uow)
                    if not fresh.ok:
                        return fresh
                    current=original.model_copy(update={"preview_id":self._id(),"dialog_revision":updated.dialog_revision})
                    try:
                        view=preview_view(updated,current,self.release.profile,p.page)
                    except ValueError:
                        return self._failure("VALIDATION_ERROR",ctx)
                    uow.previews.insert(current)
                saved=self._save_case(uow,updated,case)
                if not saved.ok:
                    return saved
                return self._result(uow,ctx,updated,view,now,envelope.command_id)
            updated=case.model_copy(update={"dialog_revision":case.dialog_revision+1,"last_activity_at":now})
            if p.destination=="help":
                view=draft(updated,"help","1. Укажите параметры и подтвердите ответы.\n2. Выберите предложение из каталога.\n3. Проверьте сведения и получите файлы.\n\n«Назад» — исправить ответ.\n«Продолжить» — вернуться к текущему шагу.\n«Не знаю» — оставить значение неизвестным.\n\nДемо: используйте вымышленные сведения. Сравнение не заменяет назначения специалиста; решение о помощи принимает фонд.",navigation(guard_for(updated)))
            elif p.destination=="materials":
                updated=updated.model_copy(update={"step":"materials"})
                try:
                    view=self._materials_view(uow,updated,now,p.page)
                except ValueError:
                    return self._failure("VALIDATION_ERROR",ctx)
            elif p.destination=="back":
                key=self._latest_question(uow,case,now)
                updated=updated.model_copy(update={"step":"input"})
                view=input_view(updated,self._rule(key)) if key else edit_view(updated,revision,self.release.profile,self.release.offers)
            else:
                if updated.step=="materials":
                    updated=updated.model_copy(update={"step":"review" if updated.branch else "input"})
                saved=self._save_case(uow,updated,case)
                if not saved.ok:
                    return saved
                next_view=self._normal_view(uow,ctx,updated,now)
                if not next_view.ok:
                    return next_view
                view=next_view.value
            saved=self._save_case(uow,updated,case)
            if not saved.ok:
                return saved
            return self._result(uow,ctx,updated,view,now,envelope.command_id)
        if envelope.type=="delete_case":
            handle=uow.handles.resolve(ctx,p.confirmation_handle,now)
            if not handle.ok:
                return handle
            if handle.value.action.action_key!="confirm_delete" or handle.value.case_guard!=guard_for(case):
                return self._failure("ACCESS_DENIED",ctx)
            deleted=uow.cases.mark_deleted(ctx,guard_for(case),now)
            if not deleted.ok:
                return deleted
            view=draft(None,"deleted","Доступ к материалам закрыт. Файлы, уже отправленные в MAX, могут остаться в переписке.",(
                action("new_case","Новый кейс с актуальным каталогом","navigate",NavigatePayload(destination="new_case")),
                action("cases","Мои кейсы","navigate",NavigatePayload(destination="cases",screen="cases"))))
            return self._result(uow,ctx,None,view,now,envelope.command_id)
        return self._failure("INVALID_TRANSITION",ctx)

    def _freshness(self,content,now,uow=None):
        refs=(content.category_profile_ref,content.matching_algorithm_ref,content.pricing_algorithm_ref,content.generator_ref)+tuple(content.template_refs)
        if content.case_mode=="demo" and "synthetic_demo" not in content.flags:
            return self._failure("DATA_NOT_READY")
        if content.case_mode=="pilot" and not (getattr(self.settings,"reviewed_data_ready",False) and getattr(self.settings,"privacy_ready",False)):
            return self._failure("DATA_NOT_READY")
        if content.generator_ref!=self.generator_ref:
            return self._failure("DATA_NOT_READY")
        if content.matching_algorithm_ref!=self.matching_ref or content.pricing_algorithm_ref!=self.pricing_ref:
            return self._failure("DATA_NOT_READY")
        if content.category_profile_ref!=VersionRef(id=self.release.profile.profile_id,version=self.release.profile.version):
            return self._failure("DATA_NOT_READY")
        if content.catalog_ref is not None and content.catalog_ref!=self.release.catalog.ref:
            return self._failure("DATA_NOT_READY")
        if content.data_release_ref is not None and content.data_release_ref!=self.release.release_ref:
            return self._failure("DATA_NOT_READY")
        offer=next((o for o in self.release.offers if o.snapshot_id==content.offer_snapshot.snapshot_id),None)
        if offer is None or content_hash(offer)!=content_hash(content.offer_snapshot):
            return self._failure("DATA_NOT_READY")
        if uow is not None:
            ready=self._release_ready(uow,content.case_mode,now)
            if not ready.ok:return ready
        if content.route_ref and content.route_ref!=self.release.route.ref:
            return self._failure("DATA_NOT_READY")
        lifecycle=getattr(self.release,"route_lifecycle",None)
        if content.route_ref and lifecycle:
            if lifecycle.status=="revoked":
                return self._failure("DATA_REVOKED")
            if lifecycle.status=="expired":
                return self._failure("DATA_EXPIRED")
        if any(ref not in self.release.template_refs for ref in content.template_refs):
            return self._failure("DATA_NOT_READY")
        return Result.success(FreshnessDecision(decision="current",reasons=("synthetic_demo",),checked_versions=refs,checked_at=now))

    def _artifact_specs(self,branch):
        required=(("purchase_card","pdf"),) if branch=="purchase" else (("application","docx"),("application","pdf"),("product_card","pdf"),("checklist","pdf"))
        specs=[]
        for kind,fmt in required:
            entries=tuple(entry for entry in self.release.templates.templates if entry.document_kind==kind and any(asset.format==fmt for asset in entry.assets))
            if len(entries)!=1:
                raise ValueError("required template mapping unavailable or ambiguous")
            specs.append(ArtifactSpec(document_kind=kind,format=fmt,template_ref=entries[0].ref))
        return tuple(specs)

    def _prepare_preview(self,uow,ctx,guard,now):
        if not uow.restore_ready():
            return self._failure("DATA_NOT_READY")
        found=self._owned_case(uow,ctx,guard)
        if not found.ok:
            return found
        case=found.value
        if case.branch is None or case.selected_snapshot_id is None:
            return self._failure("INVALID_TRANSITION",ctx)
        revision=uow.inputs.get(case.input_revision_id)
        if self._next_field(case,revision):
            return self._failure("INVALID_TRANSITION",ctx)
        snapshot=next((o for o in self.release.offers if o.snapshot_id==case.selected_snapshot_id),None)
        if snapshot is None:
            return self._failure("DATA_NOT_READY",ctx)
        comparison=compare_offer(self._id(),revision,snapshot,self.release.profile,self.matching_ref,now)
        pricing=quote_purchase(revision,snapshot,self.pricing_ref)
        if not comparison.ok:
            return comparison
        if not pricing.ok:
            return pricing
        route=None
        if case.branch=="support":
            context=build_route_context(revision,snapshot,self._policy(ctx),now)
            evaluated=evaluate_route(context,self.release.route,self.release.route_lifecycle,now)
            if not evaluated.ok:
                return evaluated
            route=evaluated.value
        missing=list(revision.unspecified_fields)
        if revision.certificate_amount is None:
            missing.append("certificate_amount")
        if case.branch=="support":
            missing.extend(key for key in ("applicant_name","address") if not revision.document_fields.get(key))
            missing.extend(route.missing_fields)
            if route.addressee.status!="known":
                missing.append("addressee")
        try:
            refs=tuple(dict.fromkeys(spec.template_ref for spec in self._artifact_specs(case.branch)))
        except ValueError:
            return self._failure("DATA_NOT_READY",ctx)
        content=ManifestContent(owner_id=ctx.owner_id,case_id=case.case_id,case_revision=case.case_revision,
                                deletion_epoch=case.deletion_epoch,case_mode=case.mode,input_revision=revision,
                                offer_snapshot=snapshot,sources=self.release.sources.sources,comparison=comparison.value,
                                pricing=pricing.value,route=route,branch=case.branch,
                                category_profile_ref=case.profile_ref,matching_algorithm_ref=self.matching_ref,
                                catalog_ref=self.release.catalog.ref,data_release_ref=self.release.release_ref,
                                supplier=next((s for s in self.release.catalog.suppliers if s.supplier_id==snapshot.supplier_id),None),
                                pricing_algorithm_ref=self.pricing_ref,route_ref=route.route_ref if route else None,
                                template_refs=refs,generator_ref=self.generator_ref,
                                release_commit=getattr(self.settings,"release_commit","demo-local"),missing_fields=tuple(sorted(set(missing))),
                                flags=(("synthetic_demo","synthetic_user","public_catalog") if snapshot.data_kind=="public_snapshot" and case.mode=="demo" else ("synthetic_demo",) if case.mode=="demo" else ())+( ("synthetic_route_model",) if route and self.release.route.data_kind=="synthetic" else ()))
        freshness=self._freshness(content,now,uow)
        if not freshness.ok:
            return freshness
        preview=ResultPreview(preview_id=self._id(),owner_id=ctx.owner_id,case_guard=guard,
                              dialog_revision=case.dialog_revision,proposed_content=content,manifest_hash=content_hash(content),
                              freshness=freshness.value,expires_at=now+timedelta(seconds=self.settings.preview_ttl_seconds))
        uow.previews.insert(preview)
        return Result.success(preview)

    @correlated
    def prepare_result_preview(self,ctx,guard,now):
        with self.db.uow() as uow:
            result=self._prepare_preview(uow,ctx,guard,now)
            if result.ok:
                uow.commit()
            return result

    def _confirm_preview(self,uow,ctx,case,payload,now):
        preview=uow.previews.get(payload.preview_id)
        if preview is None or preview.owner_id!=ctx.owner_id:
            return self._failure("ACCESS_DENIED",ctx)
        if preview.case_guard!=guard_for(case):
            return self._failure("STALE_REVISION",ctx)
        if payload.manifest_hash!=preview.manifest_hash or content_hash(preview.proposed_content)!=preview.manifest_hash:
            return self._failure("VALIDATION_ERROR",ctx)
        # A duplicate confirmation uses the exact existing bundle. No recalculation.
        existing=uow.confirmations.find_by_preview(preview.preview_id)
        if existing:
            manifest=uow.manifests.find_by_confirmation(existing.confirmation_id,owner_id=ctx.owner_id,case_id=case.case_id)
            if manifest is None:
                return self._failure("DATA_NOT_READY",ctx)
            bundle=uow.bundles.find_by_manifest(manifest.manifest_id)
            return Result.success((case,bundle))
        if preview.expires_at<=now or preview.dialog_revision!=case.dialog_revision:
            return self._failure("STALE_REVISION",ctx)
        fresh=self._freshness(preview.proposed_content,now,uow)
        if not fresh.ok:
            return fresh
        confirmation=Confirmation(confirmation_id=self._id(),owner_id=ctx.owner_id,case_id=case.case_id,
                                  case_revision=case.case_revision,deletion_epoch=case.deletion_epoch,
                                  preview_id=preview.preview_id,manifest_hash=preview.manifest_hash,confirmed_at=now,
                                  confirmed_missing_fields=preview.proposed_content.missing_fields)
        manifest=DocumentManifest(manifest_id=self._id(),manifest_hash=preview.manifest_hash,created_at=now,
                                  confirmation_id=confirmation.confirmation_id,content=preview.proposed_content)
        bundle=DocumentBundle(bundle_id=self._id(),owner_id=ctx.owner_id,case_id=case.case_id,manifest_id=manifest.manifest_id,
                              status="preparing",required_artifacts=self._artifact_specs(case.branch),published_artifact_ids=(),created_at=now)
        uow.confirmations.insert(confirmation); uow.manifests.insert(manifest); uow.bundles.insert(bundle)
        for spec in bundle.required_artifacts:
            work=WorkRef(job_id=self._id(),kind="render_artifact",payload_ref=manifest.manifest_id,owner_id=ctx.owner_id,
                         case_id=case.case_id,case_revision=case.case_revision,deletion_epoch=case.deletion_epoch,
                         dedupe_key="render:"+str(manifest.manifest_id)+":"+spec.document_kind+":"+spec.format)
            uow.work.enqueue_unique(work,render_payload=RenderPayload(manifest_id=manifest.manifest_id,bundle_id=bundle.bundle_id,artifact_spec=spec))
        updated=case.model_copy(update={"active_confirmation_id":confirmation.confirmation_id,"step":"preparing",
                                        "dialog_revision":case.dialog_revision+1,"last_activity_at":now})
        saved=self._save_case(uow,updated,case)
        if not saved.ok:
            return saved
        return Result.success((updated,bundle))

    def _actor_for(self,owner_id,case,trace_id,delivery_target_id=None,uow=None):
        if uow is None:
            raise ValueError("semantic context requires the current transaction")
        target=delivery_target_id
        if target is None and case is not None:
            previous=uow.outbox.latest_for_owner_case(owner_id,case.case_id)
            target=previous.intent.delivery_target_id if previous else None
        if target is None:
            identities=uow.identities.for_owner(owner_id,limit=1)
            target=identities[0].delivery_target_id if identities else owner_id
        return ActorContext(owner_id=owner_id,delivery_target_id=target,bot_scope=getattr(self.settings,"bot_scope","demo"),
                            case_mode=case.mode if case else "demo",correlation_id=trace_id)

    def _render_context(self,uow,claim,now):
        if not uow.restore_ready():
            return self._failure("DATA_NOT_READY")
        if claim.kind!="render_artifact" or not self._check_claim(uow,claim,now):
            return self._failure("LEASE_LOST")
        record=uow.work.get(claim.job_id)
        payload=record.render_payload
        if payload is None:
            return self._failure("DATA_NOT_READY")
        manifest=uow.manifests.get(payload.manifest_id)
        if manifest is None or manifest.content.owner_id!=claim.owner_id:
            return self._failure("ACCESS_DENIED")
        ctx=ActorContext(owner_id=claim.owner_id,delivery_target_id=claim.owner_id,bot_scope=record.bot_scope,
                         case_mode=self.settings.mode,correlation_id=claim.trace_id)
        guard=CaseGuard(case_id=claim.case_id,expected_revision=claim.case_revision,expected_deletion_epoch=claim.deletion_epoch)
        owned=self._owned_case(uow,ctx,guard)
        if not owned.ok:
            return owned
        case=owned.value
        if case.active_confirmation_id!=manifest.confirmation_id:
            return self._failure("STALE_REVISION",ctx)
        bundle=uow.bundles.get(payload.bundle_id)
        if bundle is None or bundle.owner_id!=claim.owner_id or bundle.manifest_id!=manifest.manifest_id or bundle.case_id!=claim.case_id:
            return self._failure("ACCESS_DENIED",ctx)
        if bundle.status!="preparing":
            return self._failure("INVALID_TRANSITION",ctx)
        if (manifest.content.case_id,manifest.content.case_revision,manifest.content.deletion_epoch)!=(claim.case_id,claim.case_revision,claim.deletion_epoch):
            return self._failure("STALE_REVISION",ctx)
        if content_hash(manifest.content)!=manifest.manifest_hash:
            return self._failure("VALIDATION_ERROR",ctx)
        fresh=self._freshness(manifest.content,now,uow)
        if not fresh.ok:
            return fresh
        if payload.artifact_spec not in bundle.required_artifacts or payload.artifact_spec.template_ref not in manifest.content.template_refs:
            return self._failure("DATA_NOT_READY",ctx)
        return Result.success(RenderJobContext(manifest=manifest,payload=payload,template_registry=self.release.templates))

    @correlated
    def get_render_context(self,claim,now=None):
        with self.db.uow() as uow:
            return self._render_context(uow,claim,now or self.clock.now())

    @correlated
    def publish_render_result(self,claim,staged):
        now=self.clock.now()
        with self.db.uow() as uow:
            checked=self._render_context(uow,claim,now)
            if not checked.ok:
                return checked
            manifest,payload=checked.value.manifest,checked.value.payload
            spec=payload.artifact_spec
            if staged.job_id!=claim.job_id or staged.fence_token!=claim.fence_token:
                return self._failure("LEASE_LOST")
            if staged.manifest_id!=manifest.manifest_id or staged.document_kind!=spec.document_kind or staged.format!=spec.format:
                return self._failure("VALIDATION_ERROR")
            if staged.size_bytes<=0 or staged.size_bytes>self.settings.max_file_bytes:
                return self._failure("VALIDATION_ERROR")
            record=ArtifactRecord(**staged.model_dump(),manifest_hash=manifest.manifest_hash,owner_id=claim.owner_id,case_id=claim.case_id,
                                  case_revision=claim.case_revision,deletion_epoch=claim.deletion_epoch,
                                  publication_status="published",published_at=now,expires_at=now+timedelta(seconds=self.settings.artifact_ttl_seconds))
            uow.artifacts.insert(record)
            bundle=uow.bundles.get(payload.bundle_id)
            published=tuple(dict.fromkeys(bundle.published_artifact_ids+(record.artifact_id,)))
            all_artifacts=uow.artifacts.get_many(published,owner_id=claim.owner_id,case_id=claim.case_id)
            all_artifacts=tuple(a for a in all_artifacts if a.manifest_id==manifest.manifest_id and a.case_revision==claim.case_revision and a.deletion_epoch==claim.deletion_epoch and a.publication_status=="published")
            complete=all(any(a.artifact_id in published and a.document_kind==required.document_kind and a.format==required.format
                             for a in all_artifacts) for required in bundle.required_artifacts)
            uow.bundles.save(bundle.model_copy(update={"published_artifact_ids":published,"status":"ready" if complete else "preparing"}))
            if not uow.work.finish_if_claim(claim,now):
                return self._failure("LEASE_LOST")
            if complete:
                case=uow.cases.get(claim.case_id)
                updated=case.model_copy(update={"step":"materials","dialog_revision":case.dialog_revision+1,"status":"completed"})
                self._save_case(uow,updated,case)
                ctx=self._actor_for(claim.owner_id,case,claim.trace_id,uow=uow)
                view=self._bind_view(uow,ctx,self._materials_view(uow,updated,now),now)
                self._outbox(uow,ctx,view,guard_for(updated),now,"bundle-ready:"+str(bundle.bundle_id))
            uow.commit()
            return Result.success(record)

    def _request_material(self,uow,ctx,guard,artifact_id,disposition,now,warning_acknowledged=False):
        if not uow.restore_ready():
            return self._failure("DATA_NOT_READY")
        owned=self._owned_case(uow,ctx,guard)
        if not owned.ok:
            return owned
        case=owned.value
        artifact=uow.artifacts.get(artifact_id)
        if artifact is None or artifact.owner_id!=ctx.owner_id or artifact.case_id!=case.case_id:
            return self._failure("ACCESS_DENIED",ctx)
        if artifact.publication_status!="published" or artifact.expires_at<=now:
            return self._failure("ARTIFACT_EXPIRED",ctx)
        if artifact.deletion_epoch!=case.deletion_epoch:
            return self._failure("CASE_DELETED",ctx)
        if disposition=="historical":
            if not warning_acknowledged:
                return self._failure("INVALID_TRANSITION",ctx)
        elif disposition=="current":
            if artifact.case_revision!=case.case_revision:
                return self._failure("STALE_REVISION",ctx)
            manifest=uow.manifests.get(artifact.manifest_id)
            fresh=self._freshness(manifest.content,now,uow)
            if not fresh.ok:
                return fresh
        else:
            return self._failure("VALIDATION_ERROR",ctx)
        permit=MaterialPermit(owner_id=ctx.owner_id,case_id=case.case_id,current_case_guard=guard,
                              artifact_id=artifact.artifact_id,artifact_original_revision=artifact.case_revision,
                              disposition=disposition,warning_acknowledged=warning_acknowledged,
                              expires_at=min(artifact.expires_at,now+timedelta(minutes=5)))
        intent=self._outbox(uow,ctx,permit,guard,now,"material:"+str(self._id()),"material")
        return Result.success(intent)

    @correlated
    def request_material(self,ctx,guard,artifact_id,disposition,now,warning_acknowledged=False):
        with self.db.uow() as uow:
            result=self._request_material(uow,ctx,guard,artifact_id,disposition,now,warning_acknowledged)
            if result.ok:
                uow.commit()
            return result

    def _delivery_record(self,uow,claim,now):
        if not uow.restore_ready():
            return self._failure("DATA_NOT_READY")
        if claim.kind!="deliver_outbox" or not self._check_claim(uow,claim,now):
            return self._failure("LEASE_LOST")
        record=uow.outbox.get(claim.payload_ref)
        if record is None or record.owner_id!=claim.owner_id:
            return self._failure("ACCESS_DENIED")
        if record.status=="delivery_unknown":
            return self._failure("DELIVERY_UNKNOWN")
        if record.intent.case_guard:
            ctx=ActorContext(owner_id=record.owner_id,delivery_target_id=record.intent.delivery_target_id,
                             bot_scope=getattr(self.settings,"bot_scope","demo"),case_mode=self.settings.mode,correlation_id=claim.trace_id)
            owned=self._owned_case(uow,ctx,record.intent.case_guard)
            if not owned.ok:
                return owned
            case=owned.value
            if isinstance(record.payload,ViewModel) and record.payload.dialog_revision!=case.dialog_revision:
                return self._failure("STALE_REVISION",ctx)
        elif isinstance(record.payload,ViewModel) and record.payload.case_guard is not None:
            return self._failure("ACCESS_DENIED")
        return Result.success(record)

    @correlated
    def get_delivery_payload(self,claim,now=None):
        with self.db.uow() as uow:
            return self._delivery_record(uow,claim,now or self.clock.now())

    def _material_context(self,uow,claim,now):
        found=self._delivery_record(uow,claim,now)
        if not found.ok:
            return found
        record=found.value
        permit=record.payload
        if not isinstance(permit,MaterialPermit) or record.intent.kind!="material":
            return self._failure("INVALID_TRANSITION")
        artifact=uow.artifacts.get(permit.artifact_id)
        if artifact is None or artifact.owner_id!=permit.owner_id or artifact.owner_id!=claim.owner_id or artifact.case_id!=permit.case_id:
            return self._failure("ACCESS_DENIED")
        if permit.expires_at<=now or artifact.expires_at<=now or artifact.publication_status!="published":
            return self._failure("ARTIFACT_EXPIRED")
        case=uow.cases.get(permit.case_id)
        if case is None or case.status in ("deleted","deleting") or case.deletion_epoch!=artifact.deletion_epoch:
            return self._failure("CASE_DELETED")
        if permit.current_case_guard!=guard_for(case):
            return self._failure("STALE_REVISION")
        if permit.disposition=="current":
            if artifact.case_revision!=case.case_revision:
                return self._failure("STALE_REVISION")
            manifest=uow.manifests.get(artifact.manifest_id)
            fresh=self._freshness(manifest.content,now,uow)
            if not fresh.ok:
                return fresh
        elif not permit.warning_acknowledged:
            return self._failure("ACCESS_DENIED")
        upload=UploadPermit(owner_id=permit.owner_id,artifact_id=artifact.artifact_id,
                            manifest_hash=artifact.manifest_hash,approved_at=now,
                            expires_at=permit.expires_at)
        return Result.success(MaterialDeliveryContext(permit=permit,artifact=artifact,upload_permit=upload))

    @correlated
    def get_material_context(self,claim,now=None):
        with self.db.uow() as uow:
            return self._material_context(uow,claim,now or self.clock.now())

    @correlated
    def authorize_delivery(self,claim,now=None):
        now=now or self.clock.now()
        with self.db.uow() as uow:
            found=self._delivery_record(uow,claim,now)
            if not found.ok:
                return found
            record=found.value
            if record.intent.kind=="material":
                material=self._material_context(uow,claim,now)
                if not material.ok:
                    return material
            permit=SendPermit(outbox_id=record.outbox_id,send_attempt_id=self._id(),job_id=claim.job_id,
                              fence_token=claim.fence_token,owner_id=record.owner_id,
                              delivery_target_id=record.intent.delivery_target_id,case_guard=record.intent.case_guard,
                              approved_at=now,payload_hash=content_hash(record.payload),expires_at=now+timedelta(seconds=30))
            allowed=uow.outbox.begin_send_if_allowed(claim,permit,now)
            if allowed.ok:
                uow.commit()
            return allowed

    @correlated
    def record_delivery_result(self,claim,result,now=None):
        now=now or self.clock.now()
        with self.db.uow() as uow:
            if not uow.outbox.record_transport_result(claim,result,now):
                return self._failure("LEASE_LOST")
            if result.status=="definitely_rejected" and result.retryable and claim.attempt<getattr(self.settings,"max_attempts",5):
                delay=max(1,int(result.retry_after or 2**claim.attempt))
                if not uow.work.retry_safe(claim,now,now+timedelta(seconds=delay)):
                    return self._failure("LEASE_LOST")
            elif not uow.work.finish_if_claim(claim,now):
                return self._failure("LEASE_LOST")
            record=uow.outbox.get(claim.payload_ref)
            uow.commit()
            return Result.success(record)

    def _handle_command(self,uow,ctx,opaque_handle,now,inbox_id=None):
        if not uow.restore_ready():
            return self._failure("DATA_NOT_READY")
        resolved=uow.handles.resolve(ctx,opaque_handle,now)
        if not resolved.ok:
            return resolved
        handle=resolved.value
        if handle.action.action_key=="change_branch":
            owned=self._owned_case(uow,ctx,handle.case_guard)
            if not owned.ok:
                return owned
            case=owned.value
            updated=case.model_copy(update={"dialog_revision":case.dialog_revision+1,"step":"branch","last_activity_at":now})
            saved=self._save_case(uow,updated,case)
            if not saved.ok:
                return saved
            return self._result(uow,ctx,updated,branch_view(updated),now,self._id())
        if handle.action.action_key.startswith("edit_field_"):
            owned=self._owned_case(uow,ctx,handle.case_guard)
            if not owned.ok:
                return owned
            case=owned.value
            field=handle.action.typed_payload.field_key
            rule=self._rule(field)
            if rule is None:
                return self._failure("VALIDATION_ERROR",ctx)
            updated=case.model_copy(update={"dialog_revision":case.dialog_revision+1,"step":"input","last_activity_at":now})
            saved=self._save_case(uow,updated,case)
            if not saved.ok:
                return saved
            return self._result(uow,ctx,updated,input_view(updated,rule),now,self._id())
        if handle.action.action_key=="delete_prompt":
            owned=self._owned_case(uow,ctx,handle.case_guard)
            if not owned.ok:
                return owned
            case=owned.value
            updated=case.model_copy(update={"dialog_revision":case.dialog_revision+1})
            self._save_case(uow,updated,case)
            g=guard_for(updated)
            confirm=action("confirm_delete","confirm_delete","delete_case",DeleteCasePayload(confirmation_handle="pending"),g)
            view=draft(updated,"review","Закрыть доступ к этому подбору и всем его материалам?\n\nЭто действие нельзя отменить.",(confirm,)+navigation(g)).model_copy(update={"title_key":"delete_confirmation.title"})
            return self._result(uow,ctx,updated,view,now,self._id())
        dialog_guard=None
        if handle.case_guard and handle.dialog_revision is not None:
            dialog_guard=DialogGuard(**handle.case_guard.model_dump(),expected_dialog_revision=handle.dialog_revision)
        envelope=CommandEnvelope(command_id=self._id(),inbox_id=inbox_id,actor=ctx,
                                 case_guard=handle.case_guard,dialog_guard=dialog_guard,
                                 type=handle.action.command_type,payload=handle.action.typed_payload)
        acknowledged=isinstance(envelope.payload,RequestMaterialPayload) and envelope.payload.disposition=="historical" and handle.action.action_key.startswith("material_")
        return self._execute(uow,envelope,now,historical_ack=acknowledged)

    @correlated
    def execute_action(self,ctx,action_handle):
        now=self.clock.now()
        with self.db.uow() as uow:
            result=self._handle_command(uow,ctx,action_handle,now)
            if result.ok:
                uow.commit()
            return result

    def _text_command(self,uow,ctx,text,now,case_id=None,inbox_id=None):
        if not uow.restore_ready():
            return self._failure("DATA_NOT_READY")
        global_destination={"мои кейсы":"cases","новый кейс":"new_case"}.get(text.strip().lower())
        if global_destination:
            return self._execute(uow,CommandEnvelope(command_id=self._id(),inbox_id=inbox_id,actor=ctx,type="navigate",payload=NavigatePayload(destination=global_destination,screen="cases" if global_destination=="cases" else None)),now)
        if case_id is None:
            case=uow.cases.latest_live_by_owner(ctx.owner_id)
        else:
            owned=uow.cases.get_owned(ctx,case_id)
            if not owned.ok:return owned
            case=owned.value
        if case is None:
            return self._failure("NOT_FOUND",ctx)
        lowered=text.strip().lower()
        destinations={"назад":"back","продолжить":"resume","помощь":"help","материалы":"materials","мои материалы":"materials","/help":"help"}
        if lowered in destinations:
            envelope=CommandEnvelope(command_id=self._id(),inbox_id=inbox_id,actor=ctx,case_guard=guard_for(case),
                                     type="navigate",payload=NavigatePayload(destination=destinations[lowered]))
        else:
            revision=uow.inputs.get(case.input_revision_id)
            field=self._latest_question(uow,case,now) or self._next_field(case,revision)
            if field is None:
                return self._failure("INVALID_TRANSITION",ctx)
            unknown=lowered in ("не знаю","не указано","unknown")
            envelope=CommandEnvelope(command_id=self._id(),inbox_id=inbox_id,actor=ctx,case_guard=guard_for(case),
                                     type="propose_field",payload=ProposeFieldPayload(field_key=field,raw_text=None if unknown else text,unknown=unknown))
        return self._execute(uow,envelope,now)

    @correlated
    def execute_text(self,ctx,text,case_id=None):
        now=self.clock.now()
        with self.db.uow() as uow:
            result=self._text_command(uow,ctx,text,now,case_id)
            if result.ok:
                uow.commit()
            return result

    @correlated
    def handle_inbox(self,claim):
        now=self.clock.now()
        with self.db.uow() as uow:
            if not uow.restore_ready():
                return self._failure("DATA_NOT_READY")
            if claim.kind!="process_inbox" or not self._check_claim(uow,claim,now):
                return self._failure("LEASE_LOST")
            inbox=uow.inbox.get(claim.payload_ref)
            if inbox is None or inbox.owner_id!=claim.owner_id:
                return self._failure("ACCESS_DENIED")
            event=inbox.event
            if event.kind=="ignored" and event.payload.reason!="private_chat_required":
                uow.inbox.mark_processed(inbox.inbox_id,"ignored")
                if not uow.work.finish_if_claim(claim,now):
                    return self._failure("LEASE_LOST")
                uow.commit()
                return Result.success(None)
            uow.savepoint("application_command")
            ctx=ActorContext(owner_id=event.owner_id,delivery_target_id=event.delivery_target_id,
                             bot_scope=event.bot_scope,case_mode=getattr(self.settings,"mode","demo"),correlation_id=claim.trace_id)
            if inbox.status in ("processed","ignored"):
                return self._failure("INVALID_TRANSITION",ctx)
            if event.kind=="start":
                case=uow.cases.latest_live_by_owner(ctx.owner_id)
                if case is not None:
                    envelope=CommandEnvelope(command_id=self._id(),inbox_id=inbox.inbox_id,actor=ctx,case_guard=guard_for(case),
                                             type="navigate",payload=NavigatePayload(destination="resume"))
                    result=self._execute(uow,envelope,now)
                else:
                    result=self._result(uow,ctx,None,self._start_view(),now,inbox.inbox_id)
            elif event.kind=="callback":
                result=self._handle_command(uow,ctx,event.payload.action_handle,now,inbox.inbox_id)

            elif event.kind=="text":
                result=self._text_command(uow,ctx,event.payload.text,now,inbox_id=inbox.inbox_id)
            else:
                result=self._result(uow,ctx,None,draft(None,"help","Откройте личный чат с помощником, чтобы начать или продолжить подбор."),now,inbox.inbox_id)
            command_succeeded=result.ok
            if not result.ok:
                error=result.error
                uow.rollback_to_savepoint("application_command")
                case=uow.cases.latest_live_by_owner(ctx.owner_id)
                if case is not None:
                    error_view=build_error_view(error).model_copy(update={"actions":navigation(guard_for(case)),"case_guard":guard_for(case),"dialog_revision":case.dialog_revision})
                else:
                    case=None
                    error_view=self._start_view("Действие недоступно. Начните новый кейс; выберите, кто обращается.")
                result=self._result(uow,ctx,case,error_view,now,inbox.inbox_id)
            uow.release_savepoint("application_command")
            if event.kind=="callback":
                answer=CallbackAnswer(owner_id=ctx.owner_id,platform_callback_id=event.payload.platform_callback_id,
                                      text_key="text",parameters={"text":"Готово" if command_succeeded else "Действие недоступно. Продолжите с текущего экрана."},inbox_id=inbox.inbox_id)
                self._outbox(uow,ctx,answer,None,now,"callback:"+str(inbox.inbox_id),"callback_answer")
            if result.value.case is not None:
                uow.inbox.bind_case(inbox.inbox_id,ctx,result.value.case.case_id)
            uow.inbox.mark_processed(inbox.inbox_id,"ignored" if event.kind=="ignored" else "processed")
            if not uow.work.finish_if_claim(claim,now):
                return self._failure("LEASE_LOST",ctx)
            uow.commit()
            return result

    def _fail_render_bundle(self,uow,record,now):
        """One failure transition shared by claimed work and crash recovery."""
        work=record.work
        if work.kind!="render_artifact" or record.render_payload is None or work.case_id is None:
            return Result.success(None)
        ctx=ActorContext(owner_id=work.owner_id,delivery_target_id=work.owner_id,bot_scope=record.bot_scope,
                         case_mode=self.settings.mode,correlation_id=record.trace_id)
        # Keep lock order compatible with publication: job, case, then bundle.
        owned=uow.cases.lock_owned(ctx,work.case_id)
        if not owned.ok:
            return Result.success(None)
        case=owned.value
        found=uow.bundles.get_owned(ctx,record.render_payload.bundle_id,lock=True)
        if not found.ok or found.value.status!="preparing":
            return Result.success(None)
        bundle=found.value
        uow.bundles.save(bundle.model_copy(update={"status":"failed"}))
        manifest=uow.manifests.get(bundle.manifest_id)
        if manifest is None or case.case_revision!=work.case_revision or case.deletion_epoch!=work.deletion_epoch or case.active_confirmation_id!=manifest.confirmation_id:
            return Result.success(None)
        updated=case.model_copy(update={"step":"review","status":"active","active_confirmation_id":None,
                                        "dialog_revision":case.dialog_revision+1,"last_activity_at":now})
        saved=self._save_case(uow,updated,case)
        if not saved.ok:
            return saved
        ctx=self._actor_for(work.owner_id,updated,record.trace_id,uow=uow)
        view=self._bind_view(uow,ctx,draft(updated,"error","Не удалось подготовить весь комплект. Готовые файлы — в «Моих материалах». Продолжите подбор, чтобы повторить подготовку.",navigation(guard_for(updated))),now)
        self._outbox(uow,ctx,view,guard_for(updated),now,"bundle-failed:"+str(bundle.bundle_id))
        return Result.success(None)

    @correlated
    def fail_work(self,claim,error,now=None):
        now=now or self.clock.now()
        with self.db.uow() as uow:
            if not self._check_claim(uow,claim,now):
                return self._failure("LEASE_LOST")
            record=uow.work.get(claim.job_id)
            failed=self._fail_render_bundle(uow,record,now)
            if not failed.ok:
                return failed
            if not uow.work.finish_if_claim(claim,now,status="failed"):
                return self._failure("LEASE_LOST")
            uow.commit()
            return Result.success(None)

    def recover_work(self,now,max_attempts=5,bot_scope=None):
        """Commit terminal crash recovery and its semantic recovery view together."""
        with self.db.uow() as uow:
            report=uow.work.recover_expired(now,max_attempts=max_attempts,bot_scope=bot_scope)
            for job_id in report.exhausted_ids:
                record=uow.work.get(job_id)
                if record is None:
                    continue
                if record.work.kind=="process_inbox":
                    inbox=uow.inbox.get(record.work.payload_ref)
                    if inbox is not None and inbox.status in ("received","processing"):
                        uow.inbox.mark_processed(inbox.inbox_id,"failed")
                failed=self._fail_render_bundle(uow,record,now)
                if not failed.ok:
                    return failed
            uow.commit()
            return Result.success(report)
