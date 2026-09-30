"""Two real-PostgreSQL flows and ownership/revision/publication invariants."""
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4
import pytest
from tsr.contracts import *
from tsr.application import Application
from tsr.config import Settings
from tsr.adapters.db import Database
from tsr.adapters.crypto import Crypto
from tsr.operations.releases import load_demo_release


@pytest.fixture
def app():
    import psycopg
    from psycopg.conninfo import make_conninfo
    dsn=os.getenv("TSR_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TSR_TEST_DATABASE_URL required")
    schema="test_app_"+uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    scoped=make_conninfo(dsn,options="-c search_path="+schema)
    db=Database(scoped,Crypto(b"a"*32)); db.migrate()
    settings=Settings(database_url=dsn,encryption_key="61"*32,identity_hmac_key="test",release_commit="fixture-commit")
    instance=Application(db,load_demo_release(Path(__file__).parents[1]),settings)
    try:
        yield instance
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


def actor():
    return ActorContext(owner_id=uuid4(),delivery_target_id=uuid4(),bot_scope="tsr-demo",case_mode="demo",correlation_id=uuid4())


def command(app,ctx,type,payload,case=None):
    result=app.execute_command(CommandEnvelope(command_id=uuid4(),actor=ctx,case_guard=case.guard if case else None,type=type,payload=payload))
    assert result.ok,result.error
    return result.value


def confirm_field(app,ctx,case,key,text=None):
    proposed=command(app,ctx,"propose_field",ProposeFieldPayload(field_key=key,raw_text=text,unknown=text is None),case)
    confirmed=app.execute_action(ctx,proposed.view.actions[0].action_handle)
    assert confirmed.ok,confirmed.error
    return confirmed.value


def prepare_flow(app,ctx,branch):
    started=command(app,ctx,"start_case",StartCasePayload(category_ref=app.release.profile.ref,requested_role="self"))
    case=started.case
    for key,text in (("seat_width","420"),("max_user_mass","100"),("foldable","да"),("certificate_amount","100000"),("certificate_applicable_declared","yes")):
        case=confirm_field(app,ctx,case,key,text).case
    compared=command(app,ctx,"compare_offers",CompareOffersPayload(snapshot_ids=tuple(o.snapshot_id for o in app.release.offers[:3])),case)
    from tsr.adapters.max import render_max_view
    presentation=render_max_view(compared.view)
    assert len(presentation.text)<=4000
    assert len({a.label_key for a in compared.view.actions[:3]})==3
    assert "неизвестно у продавца" in presentation.text
    assert "Цена комплектации" in presentation.text and "Покрытие сертификатом" in presentation.text
    assert "Источник: "+app.release.sources.sources[0].title in presentation.text
    assert "synthetic-demo" not in presentation.text
    selected=app.execute_action(ctx,compared.view.actions[0].action_handle)
    assert selected.ok,selected.error
    branched=command(app,ctx,"choose_branch",ChooseBranchPayload(branch=branch),selected.value.case)
    case=branched.case
    if branch=="support":
        for key,text in (("region_code","ru-alt"),("route_answers.applicant_status_declared","yes"),("document_fields.applicant_name","Вымышленный Заявитель"),("document_fields.address",None)):
            branched=confirm_field(app,ctx,case,key,text); case=branched.case
    assert branched.view.kind=="review"
    with app.db.uow() as u:
        handle=u.handles.resolve(ctx,branched.view.actions[0].action_handle,datetime.now(timezone.utc)).value
        preview=u.previews.get(handle.action.typed_payload.preview_id)
    result=app.execute_action(ctx,branched.view.actions[0].action_handle)
    assert result.ok,result.error
    # Direct replay has the same semantic guard and can only return this bundle.
    replay=command(app,ctx,"confirm_result",handle.action.typed_payload,result.value.case)
    assert replay.created_bundle_id==result.value.created_bundle_id
    with app.db.uow() as u:
        manifest=u.manifests.list_by_case(case.case_id)[0]
        assert manifest.content==preview.proposed_content
        assert manifest.manifest_hash==preview.manifest_hash==content_hash(manifest.content)
        assert len(u.bundles.list_by_case(case.case_id))==1
        jobs=[j for j in u.work.list_by_case(case.case_id) if j.work.kind=="render_artifact"]
        assert len(jobs)==(1 if branch=="purchase" else 4)
    return result.value.case,manifest


def publish_all(app):
    published=[]
    while True:
        with app.db.uow() as u:
            claim=u.work.claim_next("render_artifact","test-render",datetime.now(timezone.utc),60)
            u.commit()
        if claim is None:
            break
        context=app.get_render_context(claim)
        assert context.ok,context.error
        spec=context.value.payload.artifact_spec
        staged=StagedArtifact(artifact_id=uuid4(),job_id=claim.job_id,fence_token=claim.fence_token,
            manifest_id=context.value.manifest.manifest_id,document_kind=spec.document_kind,format=spec.format,
            plaintext_sha256="a"*64,encrypted_blob_ref="private-test/"+uuid4().hex,size_bytes=10,created_at=datetime.now(timezone.utc))
        stale=app.publish_render_result(claim.model_copy(update={"fence_token":claim.fence_token+1}),staged)
        assert not stale.ok and stale.error.code==ErrorCode.LEASE_LOST
        result=app.publish_render_result(claim,staged)
        assert result.ok,result.error
        assert result.value.expires_at-result.value.published_at==timedelta(seconds=app.settings.artifact_ttl_seconds)
        published.append(result.value)
    return published


@pytest.mark.parametrize("branch",["purchase","support"])
def test_both_frozen_confirmed_flows_and_required_bundle_readiness(app,branch):
    ctx=actor();case,manifest=prepare_flow(app,ctx,branch)
    artifacts=publish_all(app)
    assert len(artifacts)==(1 if branch=="purchase" else 4)
    with app.db.uow() as u:
        bundle=u.bundles.list_by_case(case.case_id)[0]
        current=u.cases.get(case.case_id)
        assert bundle.status=="ready"
        notices=[o for o in u.outbox.list_by_case(case.case_id) if o.intent.dedupe_key.startswith("bundle-ready:")]
        assert len(notices)==1
    allowed=app.request_material(ctx,current.guard,artifacts[0].artifact_id,"current",datetime.now(timezone.utc))
    assert allowed.ok,allowed.error
    foreign=app.request_material(actor(),current.guard,artifacts[0].artifact_id,"current",datetime.now(timezone.utc))
    assert not foreign.ok and foreign.error.code==ErrorCode.ACCESS_DENIED
    edited=confirm_field(app,ctx,current,"seat_width","430").case
    denied=app.request_material(ctx,edited.guard,artifacts[0].artifact_id,"current",datetime.now(timezone.utc))
    assert not denied.ok and denied.error.code==ErrorCode.STALE_REVISION
    unack=app.request_material(ctx,edited.guard,artifacts[0].artifact_id,"historical",datetime.now(timezone.utc))
    assert not unack.ok
    historical=app.request_material(ctx,edited.guard,artifacts[0].artifact_id,"historical",datetime.now(timezone.utc),warning_acknowledged=True)
    assert historical.ok,historical.error
    materials=command(app,ctx,"navigate",NavigatePayload(destination="materials"),edited)
    assert any("Прежние версии неактуальны" in section.parameters.get("text","")
               and "вы подтверждаете" in section.parameters.get("text","") for section in materials.view.sections)
    delete_prompt=next(a for a in materials.view.actions if a.label_key=="delete")
    prompt=app.execute_action(ctx,delete_prompt.action_handle)
    assert prompt.ok,prompt.error
    assert prompt.value.view.title_key=="delete_confirmation.title"
    deleted=app.execute_action(ctx,prompt.value.view.actions[0].action_handle)
    assert deleted.ok,deleted.error
    denied=app.request_material(ctx,materials.case.guard,artifacts[0].artifact_id,"historical",datetime.now(timezone.utc),True)
    assert not denied.ok and denied.error.code==ErrorCode.CASE_DELETED


def test_candidate_is_not_saved_before_confirmation_and_stale_handles_fail(app):
    ctx=actor()
    started=command(app,ctx,"start_case",StartCasePayload(category_ref=app.release.profile.ref,requested_role="representative"))
    first=command(app,ctx,"propose_field",ProposeFieldPayload(field_key="seat_width",raw_text="420"),started.case)
    second=command(app,ctx,"propose_field",ProposeFieldPayload(field_key="seat_width",raw_text="430"),first.case)
    with app.db.uow() as u:
        revision=u.inputs.get(second.case.input_revision_id)
        assert "seat_width" not in revision.prescribed
        assert len(u.inputs.list_by_case(second.case.case_id))==1
    stale=app.execute_action(ctx,first.view.actions[0].action_handle)
    assert not stale.ok and stale.error.code in (ErrorCode.STALE_CANDIDATE,ErrorCode.STALE_REVISION)
    foreign=app.execute_action(actor(),second.view.actions[0].action_handle)
    assert not foreign.ok and foreign.error.code==ErrorCode.ACCESS_DENIED
    result=app.execute_action(ctx,second.view.actions[0].action_handle)
    assert result.ok,result.error
    with app.db.uow() as u:
        assert u.inputs.get(result.value.case.input_revision_id).prescribed["seat_width"].value=="430"
    help_result=command(app,ctx,"navigate",NavigatePayload(destination="help"),result.value.case)
    assert help_result.case.case_revision==result.value.case.case_revision


def process_event(app,ctx,kind,payload):
    now=datetime.now(timezone.utc);inbox_id=uuid4()
    event=NormalizedEvent(inbox_id=inbox_id,bot_scope=ctx.bot_scope,dedupe_key=uuid4().hex,owner_id=ctx.owner_id,
                          delivery_target_id=ctx.delivery_target_id,kind=kind,payload=payload,occurred_at=now,received_at=now)
    with app.db.uow() as u:
        u.inbox.insert(InboxRecord(inbox_id=inbox_id,owner_id=ctx.owner_id,bot_scope=ctx.bot_scope,event_key=event.dedupe_key,event=event,received_at=now))
        u.work.enqueue_unique(WorkRef(job_id=uuid4(),kind="process_inbox",payload_ref=inbox_id,owner_id=ctx.owner_id,dedupe_key=event.dedupe_key))
        u.commit()
    with app.db.uow() as u:
        claim=u.work.claim_next("process_inbox","test-inbox",datetime.now(timezone.utc),60)
        u.commit()
    result=app.handle_inbox(claim)
    assert result.ok,result.error
    with app.db.uow() as u:
        assert u.inbox.get(inbox_id).status==("ignored" if kind=="ignored" else "processed")
        assert u.work.get(claim.job_id).status=="succeeded"
    return result.value


def test_verified_start_requires_explicit_role_and_errors_offer_recovery(app):
    ctx=actor()
    started=process_event(app,ctx,"start",StartEvent())
    assert started.case is None
    assert {a.label_key for a in started.view.actions[:2]}=={"Для себя","Для другого человека"}
    with app.db.uow() as u:
        assert not u.cases.list_by_owner(ctx.owner_id)
    foreign=app.execute_action(actor(),started.view.actions[1].action_handle)
    assert not foreign.ok
    chosen=app.execute_action(ctx,started.view.actions[1].action_handle)
    assert chosen.ok,chosen.error
    with app.db.uow() as u:
        revision=u.inputs.get(chosen.value.case.input_revision_id)
        assert revision.role=="representative"
        assert revision.confirmations[0].field_key=="role"
    stale=app.execute_action(ctx,started.view.actions[0].action_handle)
    assert not stale.ok
    resumed=process_event(app,ctx,"start",StartEvent())
    assert resumed.case.case_id==chosen.value.case.case_id
    rejected=process_event(app,ctx,"callback",CallbackEvent(platform_callback_id="verified-callback",action_handle="unavailable-handle"))
    assert rejected.view.kind=="error"
    assert {"resume","help"} <= {a.label_key for a in rejected.view.actions}
    with app.db.uow() as u:
        assert len(u.cases.list_by_owner(ctx.owner_id))==1
        acknowledgements=[o for o in u.outbox.list_by_owner(ctx.owner_id) if o.intent.kind=="callback_answer"]
        assert len(acknowledgements)==1


def test_current_release_revocation_blocks_publication_and_current_material(app):
    from tsr.operations.releases import import_release,activate_release,revoke_package
    now=datetime.now(timezone.utc)
    assert import_release(Path(__file__).parents[1],app.release.manifest,"test",db=app.db,now=now).status=="staged"
    assert activate_release(app.release.release_ref,"test","demo",now,db=app.db,allow_synthetic_draft=True,bot_scope=app.settings.bot_scope).ok
    ctx=actor();case,_=prepare_flow(app,ctx,"purchase")
    artifact=publish_all(app)[0]
    with app.db.uow() as u:
        current=u.cases.get(case.case_id)
    second_ctx=actor();second_case,_=prepare_flow(app,second_ctx,"purchase")
    with app.db.uow() as u:
        claim=u.work.claim_next("render_artifact","test-render",datetime.now(timezone.utc),60);u.commit()
    context=app.get_render_context(claim)
    assert context.ok
    spec=context.value.payload.artifact_spec
    staged=StagedArtifact(artifact_id=uuid4(),job_id=claim.job_id,fence_token=claim.fence_token,manifest_id=context.value.manifest.manifest_id,
            document_kind=spec.document_kind,format=spec.format,plaintext_sha256="b"*64,encrypted_blob_ref="private-test/"+uuid4().hex,size_bytes=10,created_at=datetime.now(timezone.utc))
    assert revoke_package(app.release.release_ref,"demo-revocation","test",datetime.now(timezone.utc),db=app.db).ok
    published=app.publish_render_result(claim,staged)
    assert not published.ok and published.error.code==ErrorCode.DATA_REVOKED
    denied=app.request_material(ctx,current.guard,artifact.artifact_id,"current",datetime.now(timezone.utc))
    assert not denied.ok and denied.error.code==ErrorCode.DATA_REVOKED
    history=app.request_material(ctx,current.guard,artifact.artifact_id,"historical",datetime.now(timezone.utc),True)
    assert history.ok,history.error
    materials=command(app,ctx,"navigate",NavigatePayload(destination="materials"),current)
    assert any(a.label_key.startswith("Исторический") for a in materials.view.actions)


def claim_material(app,outbox_id):
    while True:
        with app.db.uow() as u:
            claim=u.work.claim_next("deliver_outbox","test-delivery",datetime.now(timezone.utc),60);u.commit()
        assert claim is not None
        if claim.payload_ref==outbox_id:
            return claim
        assert app.fail_work(claim,DomainError(code="STALE_REVISION",safe_message_key="error.stale_revision")).ok


def test_durable_material_send_unknown_requires_explicit_retry_and_new_permit(app):
    ctx=actor();case,_=prepare_flow(app,ctx,"purchase");artifact=publish_all(app)[0]
    with app.db.uow() as u:
        current=u.cases.get(case.case_id)
    requested=app.request_material(ctx,current.guard,artifact.artifact_id,"current",datetime.now(timezone.utc))
    assert requested.ok
    claim=claim_material(app,requested.value.outbox_id)
    context=app.get_material_context(claim)
    assert context.ok,context.error
    authorized=app.authorize_delivery(claim)
    assert authorized.ok,authorized.error
    assert authorized.value.payload_hash==content_hash(context.value.permit)
    with app.db.uow() as u:
        assert u.outbox.get(requested.value.outbox_id).status=="sending"
    outcome=app.record_delivery_result(claim,TransportResult(status="unknown",reason_code="network_timeout"))
    assert outcome.ok and outcome.value.status=="delivery_unknown"
    with app.db.uow() as u:
        assert u.work.claim_next("deliver_outbox","automatic-retry",datetime.now(timezone.utc),60) is None
    refused=app.execute_command(CommandEnvelope(command_id=uuid4(),actor=ctx,case_guard=current.guard,type="retry_delivery",payload=RetryDeliveryPayload(outbox_id=requested.value.outbox_id,acknowledged_possible_duplicate=False)))
    assert not refused.ok and refused.error.correlation_id==ctx.correlation_id
    materials=command(app,ctx,"navigate",NavigatePayload(destination="materials"),current)
    assert any("могли уже прийти" in section.parameters.get("text","") for section in materials.view.sections)
    retry=next(a for a in materials.view.actions if a.label_key.startswith("Повторить доставку"))
    repeated=app.execute_action(ctx,retry.action_handle)
    assert repeated.ok,repeated.error
    with app.db.uow() as u:
        new=[o for o in u.outbox.list_by_case(case.case_id) if o.intent.kind=="material" and o.outbox_id!=requested.value.outbox_id]
        assert len(new)==1 and new[0].status=="pending"
    renewed_claim=claim_material(app,new[0].outbox_id)
    deletion_prompt=next(a for a in repeated.value.view.actions if a.label_key=="delete")
    prompt=app.execute_action(ctx,deletion_prompt.action_handle)
    assert prompt.ok
    assert app.execute_action(ctx,prompt.value.view.actions[0].action_handle).ok
    denied=app.authorize_delivery(renewed_claim)
    assert not denied.ok and denied.error.code in (ErrorCode.CASE_DELETED,ErrorCode.LEASE_LOST)


def test_change_offer_and_branch_preserves_confirmed_input_and_invalidates_result(app):
    ctx=actor();case,_=prepare_flow(app,ctx,"purchase")
    old_revision=case.input_revision_id
    menu=command(app,ctx,"navigate",NavigatePayload(destination="back"),case)
    change=next(a for a in menu.view.actions if a.label_key=="Сменить предложение")
    comparison=app.execute_action(ctx,change.action_handle)
    assert comparison.ok
    same=app.execute_action(ctx,comparison.value.view.actions[0].action_handle)
    assert same.ok
    assert same.value.case.case_revision==case.case_revision
    assert same.value.case.active_confirmation_id==case.active_confirmation_id
    with app.db.uow() as u:
        assert u.bundles.list_by_case(case.case_id)[0].status=="preparing"
    same_branch=app.execute_action(ctx,same.value.view.actions[0].action_handle)
    assert same_branch.ok and same_branch.value.case.case_revision==case.case_revision
    assert same_branch.value.view.kind=="preparing"
    menu=command(app,ctx,"navigate",NavigatePayload(destination="back"),same_branch.value.case)
    comparison=app.execute_action(ctx,next(a for a in menu.view.actions if a.label_key=="Сменить предложение").action_handle)
    changed=app.execute_action(ctx,comparison.value.view.actions[1].action_handle)
    assert changed.ok
    assert changed.value.case.selected_snapshot_id!=case.selected_snapshot_id
    assert changed.value.case.input_revision_id==old_revision
    assert changed.value.case.case_revision==case.case_revision+1
    assert changed.value.case.active_confirmation_id is None
    with app.db.uow() as u:
        assert u.bundles.list_by_case(case.case_id)[0].status=="historical"
        assert all(j.status=="cancelled" for j in u.work.list_by_case(case.case_id) if j.work.kind=="render_artifact")
    purchase=app.execute_action(ctx,changed.value.view.actions[0].action_handle)
    assert purchase.ok
    menu=command(app,ctx,"navigate",NavigatePayload(destination="back"),purchase.value.case)
    choices=app.execute_action(ctx,next(a for a in menu.view.actions if a.label_key=="Сменить результат").action_handle)
    assert choices.ok
    support=app.execute_action(ctx,choices.value.view.actions[1].action_handle)
    assert support.ok
    assert support.value.case.branch=="support"
    assert support.value.case.input_revision_id==old_revision
    assert support.value.case.case_revision==changed.value.case.case_revision+1
    assert support.value.view.kind=="input"


def finish_paged_view(app,ctx,result,confirm_label):
    from tsr.adapters.max import render_max_view
    while True:
        presentation=render_max_view(result.view)
        assert len(presentation.text)<=4000
        confirm=next((a for a in result.view.actions if a.label_key==confirm_label),None)
        if confirm:
            return app.execute_action(ctx,confirm.action_handle)
        next_action=next(a for a in result.view.actions if a.label_key.startswith("Далее"))
        moved=app.execute_action(ctx,next_action.action_handle)
        assert moved.ok,moved.error
        assert moved.value.case.case_revision==result.case.case_revision
        result=moved.value


def test_long_answers_review_in_full_pages_and_material_pages_stay_bounded(app):
    from tsr.adapters.max import render_max_view
    from tsr.domain.conversation import materials_view,bind_action_handles
    ctx=actor();case,_=prepare_flow(app,ctx,"support")
    # Editing accepts the complete text and requires review of every candidate part.
    for key,text in (("document_fields.applicant_name","&"*2000),("document_fields.address","А"*2000)):
        proposed=command(app,ctx,"propose_field",ProposeFieldPayload(field_key=key,raw_text=text),case)
        assert not any(a.label_key=="confirm" for a in proposed.view.actions)
        confirmed=finish_paged_view(app,ctx,proposed,"confirm")
        assert confirmed.ok,confirmed.error
        case=confirmed.value.case
        with app.db.uow() as u:
            assert u.inputs.get(case.input_revision_id).document_fields[key.partition(".")[2]]==text
    preview=confirmed.value
    assert preview.view.kind=="review"
    assert not any(a.label_key=="prepare" for a in preview.view.actions)
    first_next=next(a for a in preview.view.actions if a.label_key.startswith("Далее"))
    with app.db.uow() as u:
        handle=u.handles.resolve(ctx,first_next.action_handle,datetime.now(timezone.utc)).value
        frozen=u.previews.get(handle.action.typed_payload.resource_id).proposed_content
    generated=finish_paged_view(app,ctx,preview,"prepare")
    assert generated.ok,generated.error
    with app.db.uow() as u:
        manifest=next(m for m in u.manifests.list_by_case(case.case_id) if m.confirmation_id==generated.value.case.active_confirmation_id)
        assert manifest.content==frozen
        assert manifest.manifest_hash==content_hash(frozen)
    # A long historical file list is a pure presentation fixture, without new jobs.
    now=datetime.now(timezone.utc)
    artifact=ArtifactRecord(artifact_id=uuid4(),job_id=uuid4(),fence_token=1,manifest_id=manifest.manifest_id,manifest_hash=manifest.manifest_hash,
        document_kind="application",format="pdf",plaintext_sha256="a"*64,encrypted_blob_ref="test/private",size_bytes=1,created_at=now,
        owner_id=ctx.owner_id,case_id=case.case_id,case_revision=max(0,case.case_revision-1),deletion_epoch=case.deletion_epoch,published_at=now,expires_at=now+timedelta(days=1))
    artifacts=tuple(artifact.model_copy(update={"artifact_id":uuid4()}) for _ in range(36))
    seen=set()
    for page in range(5):
        vd=materials_view(case,(),artifacts,now,page=page)
        handles=tuple(HandleRecord(handle_id=uuid4(),handle=uuid4().hex,owner_id=ctx.owner_id,case_guard=case.guard,dialog_revision=case.dialog_revision,action=intent,expires_at=now+timedelta(minutes=5)) for intent in vd.actions)
        vm=bind_action_handles(uuid4(),vd,handles,now+timedelta(minutes=5))
        payload=render_max_view(vm)
        assert len(payload.text)<=4000 and len(payload.buttons)<=30
        seen.update(intent.typed_payload.artifact_id for intent in vd.actions if isinstance(intent.typed_payload,RequestMaterialPayload))
    assert seen=={a.artifact_id for a in artifacts}


def test_terminal_render_failure_restores_review_instead_of_permanent_preparing(app):
    ctx=actor();case,_=prepare_flow(app,ctx,"purchase")
    with app.db.uow() as u:
        claim=u.work.claim_next("render_artifact","failed-render",datetime.now(timezone.utc),60);u.commit()
    result=app.fail_work(claim,DomainError(code="PERMANENT_FAILURE",safe_message_key="error.permanent_failure"))
    assert result.ok
    with app.db.uow() as u:
        current=u.cases.get(case.case_id)
        assert current.step=="review" and current.active_confirmation_id is None
        assert u.bundles.list_by_case(case.case_id)[0].status=="failed"
        failures=[o for o in u.outbox.list_by_case(case.case_id) if o.intent.dedupe_key.startswith("bundle-failed:")]
        assert len(failures)==1
    resumed=command(app,ctx,"navigate",NavigatePayload(destination="resume"),current)
    assert resumed.view.kind=="review"


def test_crash_recovery_exhaustion_is_atomic_and_continue_prepares_fresh_preview(app,monkeypatch):
    ctx=actor()
    with app.db.uow() as u:
        u.identities.insert(IdentityRecord(identity_id=uuid4(),owner_id=ctx.owner_id,delivery_target_id=ctx.delivery_target_id,
                                          bot_scope=ctx.bot_scope,lookup_key=uuid4().hex,platform_user_id="synthetic-test-user"))
        u.commit()
    case,manifest=prepare_flow(app,ctx,"purchase")
    now=datetime.now(timezone.utc)
    with app.db.uow() as u:
        first=u.work.claim_next("render_artifact","crashed-first",now,1,bot_scope=ctx.bot_scope);u.commit()
    context=app.get_render_context(first,now=now)
    assert context.ok,context.error
    spec=context.value.payload.artifact_spec
    staged=StagedArtifact(artifact_id=uuid4(),job_id=first.job_id,fence_token=first.fence_token,manifest_id=manifest.manifest_id,
        document_kind=spec.document_kind,format=spec.format,plaintext_sha256="a"*64,encrypted_blob_ref="private-test/"+uuid4().hex,size_bytes=10,created_at=now)
    later=now+timedelta(seconds=2)
    recovered=app.recover_work(later,max_attempts=2,bot_scope=ctx.bot_scope)
    assert recovered.ok and recovered.value.requeued_ids==(first.job_id,)
    with app.db.uow() as u:
        second=u.work.claim_next("render_artifact","crashed-second",later,1,bot_scope=ctx.bot_scope);u.commit()
    assert second.job_id==first.job_id and second.fence_token>first.fence_token
    exhausted_at=later+timedelta(seconds=2)
    original_outbox=app._outbox
    def injected_failure(*args,**kwargs):
        raise RuntimeError("injected failure while creating recovery notice")
    monkeypatch.setattr(app,"_outbox",injected_failure)
    with pytest.raises(RuntimeError,match="injected failure"):
        app.recover_work(exhausted_at,max_attempts=2,bot_scope=ctx.bot_scope)
    with app.db.uow() as u:
        assert u.work.get(first.job_id).status=="running"
        assert u.bundles.list_by_case(case.case_id)[0].status=="preparing"
        assert u.cases.get(case.case_id).active_confirmation_id==case.active_confirmation_id
    monkeypatch.setattr(app,"_outbox",original_outbox)
    final=app.recover_work(exhausted_at,max_attempts=2,bot_scope=ctx.bot_scope)
    assert final.ok and final.value.exhausted_ids==(first.job_id,)
    with app.db.uow() as u:
        assert u.work.get(first.job_id).status=="failed"
        assert u.work.claim_next("render_artifact","automatic-third",exhausted_at+timedelta(seconds=100),60,bot_scope=ctx.bot_scope) is None
        current=u.cases.get(case.case_id)
        assert current.step=="review" and current.active_confirmation_id is None
        assert u.bundles.list_by_case(case.case_id)[0].status=="failed"
        notices=[o for o in u.outbox.list_by_case(case.case_id) if o.intent.dedupe_key.startswith("bundle-failed:")]
        assert len(notices)==1
    again=app.recover_work(exhausted_at+timedelta(seconds=100),max_attempts=2,bot_scope=ctx.bot_scope)
    assert again.ok and not again.value.exhausted_ids
    class FixedClock:
        def now(self): return exhausted_at
    app.clock=FixedClock()
    stale=app.publish_render_result(first,staged)
    assert not stale.ok and stale.error.code==ErrorCode.LEASE_LOST
    resumed=command(app,ctx,"navigate",NavigatePayload(destination="resume"),current)
    assert resumed.view.kind=="review"
    with app.db.uow() as u:
        confirm=next(a for a in resumed.view.actions if a.label_key=="prepare")
        handle=u.handles.resolve(ctx,confirm.action_handle,exhausted_at).value
        preview=u.previews.get(handle.action.typed_payload.preview_id)
        assert preview.proposed_content.input_revision==manifest.content.input_revision
        assert preview.proposed_content.comparison.computed_at==exhausted_at
        assert preview.manifest_hash!=manifest.manifest_hash
        assert len([o for o in u.outbox.list_by_case(case.case_id) if o.intent.dedupe_key.startswith("bundle-failed:")])==1


def test_catalog_pages_reuse_frozen_comparisons_and_allow_last_offer(app):
    from tsr.adapters.max import render_max_view
    ctx=actor()
    original=app.release.offers
    app.release=app.release.model_copy(update={'offers':tuple(original[n%3].model_copy(update={
        'snapshot_id':uuid4(),'data_kind':'synthetic',
        'variant':original[n%3].variant.model_copy(update={'model':f'Публичная модель {n+1}'})}) for n in range(15))})
    case=command(app,ctx,'start_case',StartCasePayload(category_ref=app.release.profile.ref,requested_role='self')).case
    for key,text in (('seat_width','420'),('max_user_mass','100'),('foldable','yes'),('certificate_amount','100000'),('certificate_applicable_declared','yes')):
        current=confirm_field(app,ctx,case,key,text);case=current.case
    first=command(app,ctx,'navigate',NavigatePayload(destination='resume',screen='catalog'),case)
    old_handle=first.view.actions[0].action_handle
    with app.db.uow() as u:
        frozen=tuple((r.comparison_id,content_hash(r)) for r in u.comparisons.list_by_case(case.case_id))
    for page in range(1,5):
        result=app.execute_action(ctx,next(a.action_handle for a in first.view.actions if a.label_key.startswith('Следующие предложения')))
        assert result.ok,result.error
        first=result.value
        rendered=render_max_view(first.view)
        assert len(rendered.text)<=4000 and len(rendered.buttons)<=30
        assert 'Дата синтетического снимка' in rendered.text
        assert first.case.input_revision_id==case.input_revision_id and first.case.case_revision==case.case_revision
    denied=app.execute_action(ctx,old_handle)
    assert not denied.ok and denied.error.code==ErrorCode.STALE_REVISION
    back=command(app,ctx,'navigate',NavigatePayload(destination='resume',screen='catalog'),first.case)
    with app.db.uow() as u:
        current_records=u.comparisons.list_by_case(case.case_id)
        assert all((id_,hash_) in tuple((r.comparison_id,content_hash(r)) for r in current_records) for id_,hash_ in frozen)
        assert len(current_records)==15
    last=command(app,ctx,'navigate',NavigatePayload(destination='resume',screen='catalog',page=4),back.case)
    selected=app.execute_action(ctx,last.view.actions[2].action_handle)
    assert selected.ok
    with app.db.uow() as u:
        # Selection advanced semantic revision; frozen last-page comparison remains readable for audit.
        record=u.comparisons.get(next(h.action.typed_payload.comparison_id for h in u.handles.list_by_case(case.case_id) if h.handle==last.view.actions[2].action_handle))
        assert selected.value.case.selected_snapshot_id==record.result.snapshot_id
    assert selected.value.case.input_revision_id==case.input_revision_id
    foreign=app.execute_action(actor(),last.view.actions[2].action_handle)
    assert not foreign.ok and foreign.error.code==ErrorCode.ACCESS_DENIED
    bad=app.execute_command(CommandEnvelope(command_id=uuid4(),actor=ctx,case_guard=selected.value.case.guard,type='navigate',payload=NavigatePayload(destination='resume',screen='catalog',page=5)))
    assert not bad.ok and bad.error.code==ErrorCode.VALIDATION_ERROR


def test_owner_case_pages_legacy_inputs_and_explicit_new_case_role(app):
    from tsr.adapters.max import render_max_view
    ctx=actor();cases=[]
    for _ in range(7):
        cases.append(command(app,ctx,'start_case',StartCasePayload(category_ref=app.release.profile.ref,requested_role='self')).case)
    legacy=confirm_field(app,ctx,cases[0],'seat_width','420').case
    app.release=app.release.model_copy(update={'profile':app.release.profile.model_copy(update={'version':'1.1.0'})})
    resumed=command(app,ctx,'navigate',NavigatePayload(destination='resume'),legacy)
    assert resumed.case.input_revision_id==legacy.input_revision_id
    assert 'сохранены' in render_max_view(resumed.view).text
    newer=app.execute_action(ctx,next(a.action_handle for a in resumed.view.actions if 'Новый кейс' in a.label_key))
    assert newer.ok and newer.value.case is None
    with app.db.uow() as u:
        assert len(u.cases.list_by_owner(ctx.owner_id))==7
    created=app.execute_action(ctx,newer.value.view.actions[1].action_handle)
    assert created.ok and created.value.case.profile_ref==app.release.profile.ref
    with app.db.uow() as u:
        assert u.inputs.get(created.value.case.input_revision_id).role=='representative'
        assert u.inputs.get(legacy.input_revision_id).prescribed['seat_width'].value=='420'
    listing=command(app,ctx,'navigate',NavigatePayload(destination='cases',screen='cases'),created.value.case)
    assert listing.case is None and len(render_max_view(listing.view).buttons)<=30
    second=app.execute_action(ctx,next(a.action_handle for a in listing.view.actions if 'Следующие кейсы' in a.label_key))
    assert second.ok
    target=next(a for a in second.value.view.actions if a.label_key.startswith('Открыть подбор'))
    foreign=app.execute_action(actor(),target.action_handle)
    assert not foreign.ok and foreign.error.code==ErrorCode.ACCESS_DENIED
    opened=app.execute_action(ctx,target.action_handle)
    assert opened.ok and opened.value.case.case_id in {c.case_id for c in cases}


def test_public_provenance_and_lower_bound_price_never_claim_exact_amount(app):
    from tsr.domain.conversation import build_comparison_view
    from tsr.domain.matching import compare_offer
    from tsr.domain.pricing import quote_purchase
    from tsr.adapters.max import render_max_view
    ctx=actor()
    case=command(app,ctx,'start_case',StartCasePayload(category_ref=app.release.profile.ref,requested_role='self')).case
    case=confirm_field(app,ctx,case,'seat_width','420').case
    with app.db.uow() as u: revision=u.inputs.get(case.input_revision_id)
    review=Review(status='reviewed',reviewer_id='test-fixture-factual-review',reviewed_at=datetime.now(timezone.utc)-timedelta(minutes=1),review_due_at=datetime.now(timezone.utc)+timedelta(days=7))
    offer=app.release.offers[0].model_copy(update={'data_kind':'public_snapshot','review':review,'price_kind':'from'})
    source=app.release.sources.sources[0].model_copy(update={'data_kind':'public_snapshot','review':review,'title':'Публичная страница точной комплектации · тестовый снимок'})
    compared=compare_offer(uuid4(),revision,offer,app.release.profile,app.matching_ref,datetime.now(timezone.utc)).value
    quote=quote_purchase(revision,offer,app.pricing_ref).value
    assert quote.price is None and quote.gap is None
    view=build_comparison_view(ComparisonSet(items=(compared,),quotes=(quote,),case_guard=case.guard),case,(offer,),app.release.catalog.suppliers,app.release.profile,(source,))
    with app.db.uow() as u:
        bound=app._bind_view(u,ctx,view,datetime.now(timezone.utc));u.commit()
    rendered=render_max_view(bound)
    assert 'Наблюдаемая цена: от 120 000 ₽' in rendered.text
    assert 'нижняя граница' in rendered.text and 'Цена комплектации: Неизвестно' in rendered.text
    assert source.title in rendered.text and 'Дата публичного снимка' in rendered.text
    assert 'Дата синтетического снимка' not in rendered.text
    assert len(rendered.text)<=4000
    app.release=app.release.model_copy(update={'offers':(offer,)})
    denied=app.execute_command(CommandEnvelope(command_id=uuid4(),actor=ctx,case_guard=case.guard,type='compare_offers',payload=CompareOffersPayload(snapshot_ids=(offer.snapshot_id,))))
    assert not denied.ok and denied.error.code==ErrorCode.DATA_NOT_READY


def test_private_chat_guidance_contains_no_case_or_callbacks(app):
    ctx=actor()
    old=command(app,ctx,'start_case',StartCasePayload(category_ref=app.release.profile.ref,requested_role='self')).case
    result=process_event(app,ctx,'ignored',IgnoredEvent(reason='private_chat_required'))
    assert result.case is None and result.view.case_guard is None and not result.view.actions
    assert 'личный' in result.view.sections[0].parameters['text']
    with app.db.uow() as u:
        assert u.cases.get(old.case_id)==old


def test_pending_candidate_restores_after_help_and_start_without_saving_input(app):
    ctx=actor()
    case=command(app,ctx,'start_case',StartCasePayload(category_ref=app.release.profile.ref,requested_role='self')).case
    proposed=command(app,ctx,'propose_field',ProposeFieldPayload(field_key='seat_width',raw_text='405'),case)
    helped=command(app,ctx,'navigate',NavigatePayload(destination='help'),proposed.case)
    restored=process_event(app,ctx,'start',StartEvent())
    assert restored.view.kind=='candidate' and restored.case.input_revision_id==case.input_revision_id
    with app.db.uow() as u:
        assert len(u.inputs.list_by_case(case.case_id))==1
        candidates=u.candidates.list_by_case(case.case_id)
        assert len(candidates)==2 and candidates[-1].value==candidates[0].value
    stale=app.execute_action(ctx,proposed.view.actions[0].action_handle)
    assert not stale.ok
    foreign=app.execute_action(actor(),restored.view.actions[0].action_handle)
    assert not foreign.ok
    confirmed=app.execute_action(ctx,restored.view.actions[0].action_handle)
    assert confirmed.ok
    resumed=command(app,ctx,'navigate',NavigatePayload(destination='resume'),confirmed.value.case)
    assert resumed.view.kind=='input'
    proposed2=command(app,ctx,'propose_field',ProposeFieldPayload(field_key='max_user_mass',raw_text='100'),resumed.case)
    with app.db.uow() as u: expiry=max(c.expires_at for c in u.candidates.list_by_case(case.case_id))
    app.clock=type('Clock',(),{'now':lambda self:expiry+timedelta(seconds=1)})()
    expired=command(app,ctx,'navigate',NavigatePayload(destination='resume'),proposed2.case)
    assert expired.view.kind=='input' and expired.case.input_revision_id==confirmed.value.case.input_revision_id
    with app.db.uow() as u:
        u.cases.mark_deleted(ctx,expired.case.guard,app.clock.now());u.commit()
    denied=app.execute_command(CommandEnvelope(command_id=uuid4(),actor=ctx,case_guard=expired.case.guard,type='navigate',payload=NavigatePayload(destination='resume')))
    assert not denied.ok and denied.error.code==ErrorCode.CASE_DELETED


@pytest.mark.parametrize('branch',['purchase','support'])
def test_real_catalog_all_twelve_ranked_and_later_medica_selection_is_frozen(app,branch):
    from tsr.operations.releases import load_release,import_release,activate_release,revoke_package
    from tsr.adapters.max import render_max_view
    root=Path(__file__).parents[1];now=datetime.now(timezone.utc)
    app.release=load_release(root,'data/releases/real-1.1.0/manifest.json')
    assert len(app.release.offers)==12
    assert import_release(root,app.release.manifest,'app-test',db=app.db,now=now).status=='staged'
    assert activate_release(app.release.release_ref,'app-test','demo',now,db=app.db,allow_synthetic_draft=True,bot_scope=app.settings.bot_scope).ok
    ctx=actor();case=command(app,ctx,'start_case',StartCasePayload(category_ref=app.release.profile.ref,requested_role='self')).case
    for key,text in (('seat_width','405'),('max_user_mass','100'),('foldable','yes'),('certificate_amount','10000'),('certificate_applicable_declared','yes')):
        outcome=confirm_field(app,ctx,case,key,text);case=outcome.case
    listing=app.execute_action(ctx,next(a.action_handle for a in outcome.view.actions if a.label_key=='compare')).value
    seen=[];classifications=[];target=None;target_page=None;page_hashes={}
    for page in range(4):
        rendered=render_max_view(listing.view)
        assert len(rendered.text)<=4000 and len(rendered.buttons)<=30
        assert 'Дата публичного снимка' in rendered.text and f'Страница {page+1}/4' in rendered.text
        assert 'демонстрационный продавец' not in rendered.text and 'Дата синтетического снимка' not in rendered.text
        with app.db.uow() as u:
            records=[]
            for button in listing.view.actions[:3]:
                handle=u.handles.resolve(ctx,button.action_handle,datetime.now(timezone.utc)).value
                record=u.comparisons.get(handle.action.typed_payload.comparison_id)
                records.append(record);seen.append(record.result.snapshot_id);classifications.append(record.result.classification)
                offer=next(o for o in app.release.offers if o.snapshot_id==record.result.snapshot_id)
                if offer.supplier_id=='medicamarket' and offer.price_kind=='from' and record.result.classification!='complete' and page>0 and target is None:
                    target=offer;target_page=page
                    assert record.quote.price is None and record.quote.gap is None
                    assert record.result.classification!='complete'
                    assert 'Наблюдаемая цена: от ' in rendered.text and 'нижняя граница' in rendered.text
            page_hashes[page]=tuple((r.comparison_id,content_hash(r)) for r in records)
        if page==0:
            assert records[0].result.classification=='complete'
            assert records[0].quote.gap==Money(minor=450000) and records[0].quote.certificate_use=='conditional'
            assert 'Условное покрытие сертификатом: 10 000 ₽' in rendered.text
            assert 'Условная разница без доставки: 4 500 ₽' in rendered.text
        if page<3:
            listing=app.execute_action(ctx,next(a.action_handle for a in listing.view.actions if a.label_key.startswith('Следующие предложения'))).value
    assert len(set(seen))==12 and target is not None
    assert classifications==sorted(classifications,key={"complete":0,"incomplete":1,"mismatch":2}.get)
    with app.db.uow() as u:
        assert len(u.comparisons.list_by_case(case.case_id))==12
    revisited=command(app,ctx,'navigate',NavigatePayload(destination='resume',screen='catalog',page=target_page),listing.case)
    with app.db.uow() as u:
        handles=[u.handles.resolve(ctx,a.action_handle,datetime.now(timezone.utc)).value for a in revisited.view.actions[:3]]
        assert tuple((h.action.typed_payload.comparison_id,content_hash(u.comparisons.get(h.action.typed_payload.comparison_id))) for h in handles)==page_hashes[target_page]
        handle=next(h for h in handles if h.action.typed_payload.snapshot_id==target.snapshot_id)
    selected=app.execute_action(ctx,handle.handle);assert selected.ok
    result=command(app,ctx,'choose_branch',ChooseBranchPayload(branch=branch),selected.value.case)
    if branch=='support':
        for key,text in (('region_code','ru-alt'),('route_answers.applicant_status_declared','yes'),('document_fields.applicant_name','Синтетический заявитель'),('document_fields.address',None)):
            result=confirm_field(app,ctx,result.case,key,text)
    full_text=[]
    while True:
        full_text.append(render_max_view(result.view).text)
        button=next((a for a in result.view.actions if a.label_key=='prepare'),None)
        if button:break
        result=app.execute_action(ctx,next(a.action_handle for a in result.view.actions if a.label_key.startswith('Далее'))).value
    reviewed='\n'.join(full_text)
    assert 'Медикамаркет' in reviewed and 'вымышленные сведения пользователя' in reviewed
    assert 'Наблюдаемая цена: от ' in reviewed and 'Цена комплектации: Неизвестно' in reviewed
    if branch=='support':assert 'Адресат:' in reviewed and 'Документ:' in reviewed and 'модель маршрута' in reviewed
    with app.db.uow() as u:
        handle=u.handles.resolve(ctx,button.action_handle,datetime.now(timezone.utc)).value
        preview=u.previews.get(handle.action.typed_payload.preview_id)
    confirmed=app.execute_action(ctx,button.action_handle);assert confirmed.ok
    with app.db.uow() as u:
        manifest=u.manifests.list_by_case(case.case_id)[0]
        assert manifest.content==preview.proposed_content and manifest.manifest_hash==preview.manifest_hash
        assert manifest.content.supplier.display_name=='Медикамаркет'
        assert manifest.content.catalog_ref==app.release.catalog.ref and manifest.content.data_release_ref==app.release.release_ref
        assert manifest.content.comparison.classification!='complete' and manifest.content.pricing.gap is None
        assert len([j for j in u.work.list_by_case(case.case_id) if j.work.kind=='render_artifact'])==(1 if branch=='purchase' else 4)
    artifacts=publish_all(app)
    with app.db.uow() as u:current=u.cases.get(case.case_id)
    assert revoke_package(VersionRef(id=target.source_ids[0],version=app.release.sources.version),'app-test revoked source','app-test',datetime.now(timezone.utc),db=app.db).ok
    blocked=app.request_material(ctx,current.guard,artifacts[0].artifact_id,'current',datetime.now(timezone.utc))
    assert not blocked.ok and blocked.error.code==ErrorCode.DATA_REVOKED
    historical=app.request_material(ctx,current.guard,artifacts[0].artifact_id,'historical',datetime.now(timezone.utc),warning_acknowledged=True)
    assert historical.ok


def test_restore_barrier_blocks_user_reads_writes_and_send_permit(app,monkeypatch):
    from tsr.adapters.db.database import UnitOfWork
    ctx=actor();case=command(app,ctx,'start_case',StartCasePayload(category_ref=app.release.profile.ref,requested_role='self')).case
    with app.db.uow() as u:
        claim=u.work.claim_next('deliver_outbox','test-restore-barrier',datetime.now(timezone.utc),60);u.commit()
    monkeypatch.setattr(UnitOfWork,'restore_ready',lambda self:False)
    assert not app.execute_text(ctx,'405',case.case_id).ok
    for payload,guard in ((NavigatePayload(destination='cases',screen='cases'),None),(NavigatePayload(destination='resume'),case.guard)):
        result=app.execute_command(CommandEnvelope(command_id=uuid4(),actor=ctx,case_guard=guard,type='navigate',payload=payload))
        assert not result.ok and result.error.code==ErrorCode.DATA_NOT_READY
    assert not app.authorize_delivery(claim).ok and not app.get_delivery_payload(claim).ok
    with app.db.uow() as u:
        assert len(u.cases.list_by_owner(ctx.owner_id))==1 and len(u.inputs.list_by_case(case.case_id))==1
        assert u.outbox.get(claim.payload_ref).status=='pending' and u.work.get(claim.job_id).status=='running'
    monkeypatch.setattr(UnitOfWork,'restore_ready',lambda self:True)
    resumed=command(app,ctx,'navigate',NavigatePayload(destination='resume'),case)
    assert resumed.case.input_revision_id==case.input_revision_id


def test_large_catalog_persists_only_visible_page_and_reuses_selection_refs(app):
    ctx=actor();seed=app.release.offers
    app.release=app.release.model_copy(update={'offers':tuple(seed[n%len(seed)].model_copy(update={'snapshot_id':uuid4()}) for n in range(1000))})
    case=command(app,ctx,'start_case',StartCasePayload(category_ref=app.release.profile.ref,requested_role='self')).case
    for key,text in (('seat_width','420'),('max_user_mass','100'),('foldable','yes'),('certificate_amount','100000'),('certificate_applicable_declared','yes')):
        case=confirm_field(app,ctx,case,key,text).case
    first=command(app,ctx,'navigate',NavigatePayload(destination='resume',screen='catalog'),case)
    with app.db.uow() as u:
        records=u.comparisons.list_by_case(case.case_id)
        assert len(records)==3
        frozen=tuple((r.comparison_id,content_hash(r)) for r in records)
    revisit=command(app,ctx,'navigate',NavigatePayload(destination='resume',screen='catalog'),first.case)
    with app.db.uow() as u:
        assert tuple((r.comparison_id,content_hash(r)) for r in u.comparisons.list_by_case(case.case_id))==frozen
    following=command(app,ctx,'navigate',NavigatePayload(destination='resume',screen='catalog',page=1),revisit.case)
    with app.db.uow() as u:assert len(u.comparisons.list_by_case(case.case_id))==6
    chosen=app.execute_action(ctx,following.view.actions[0].action_handle)
    assert chosen.ok and chosen.value.case.input_revision_id==case.input_revision_id


def test_template_mapping_uses_document_kind_and_format_with_opaque_ids(app):
    entries=tuple(entry.model_copy(update={'template_id':f'opaque-template-{n}'}) for n,entry in enumerate(app.release.templates.templates))
    registry=app.release.templates.model_copy(update={'templates':entries})
    app.release=app.release.model_copy(update={'templates':registry,'template_refs':tuple(entry.ref for entry in entries)})
    ctx=actor();case,manifest=prepare_flow(app,ctx,'support')
    with app.db.uow() as u:
        bundle=u.bundles.list_by_case(case.case_id)[0]
        for spec in bundle.required_artifacts:
            entry=next(e for e in entries if e.ref==spec.template_ref)
            assert entry.document_kind==spec.document_kind and any(a.format==spec.format for a in entry.assets)
        claim=u.work.claim_next('render_artifact','opaque-template-test',datetime.now(timezone.utc),60);u.commit()
    result=app.get_render_context(claim)
    assert result.ok and result.value.template_registry==registry


def test_actorless_unsupported_inbox_finishes_without_outbox(app):
    ctx=actor()
    result=process_event(app,ctx,'ignored',IgnoredEvent(reason='unsupported'))
    assert result is None
    with app.db.uow() as u:
        assert not u.outbox.list_by_owner(ctx.owner_id) and not u.cases.list_by_owner(ctx.owner_id)


@pytest.mark.parametrize('attribute',['matching_ref','pricing_ref'])
def test_current_algorithm_change_requires_new_preview_and_warned_history(app,attribute):
    ctx=actor();case,manifest=prepare_flow(app,ctx,'purchase')
    artifact=publish_all(app)[0]
    with app.db.uow() as u:case=u.cases.get(case.case_id)
    old_ref=getattr(app,attribute)
    setattr(app,attribute,VersionRef(id=old_ref.id,version='2.0.0'))
    result=app.request_material(ctx,case.guard,artifact.artifact_id,'current',datetime.now(timezone.utc))
    assert not result.ok and result.error.code==ErrorCode.DATA_NOT_READY
    warned=app.request_material(ctx,case.guard,artifact.artifact_id,'historical',datetime.now(timezone.utc),warning_acknowledged=True)
    assert warned.ok
    with app.db.uow() as u:assert u.manifests.get(manifest.manifest_id).manifest_hash==manifest.manifest_hash


def test_hot_user_and_publication_paths_never_read_full_history(app,monkeypatch):
    from tsr.adapters.db.database import EncryptedRepository,WorkRepository
    def forbidden(*args,**kwargs):
        raise AssertionError('hot path requested full encrypted history')
    monkeypatch.setattr(EncryptedRepository,'list_by_case',forbidden)
    monkeypatch.setattr(EncryptedRepository,'list_by_owner',forbidden)
    monkeypatch.setattr(WorkRepository,'list_by_case',forbidden)
    ctx=actor()
    menu=process_event(app,ctx,'start',StartEvent())
    result=app.execute_action(ctx,menu.view.actions[0].action_handle);assert result.ok
    case=result.value.case
    proposed=command(app,ctx,'propose_field',ProposeFieldPayload(field_key='seat_width',raw_text='420'),case)
    # Current dialog prompt and pending candidate survive help/start through exact lookups.
    text=app.execute_text(ctx,'420',proposed.case.case_id);assert text.ok
    helped=command(app,ctx,'navigate',NavigatePayload(destination='help'),text.value.case)
    resumed=process_event(app,ctx,'start',StartEvent());assert resumed.view.kind=='candidate'
    confirmed=app.execute_action(ctx,resumed.view.actions[0].action_handle);assert confirmed.ok
    case=confirmed.value.case
    for key,value in (('max_user_mass','100'),('foldable','yes'),('certificate_amount','100000'),('certificate_applicable_declared','yes')):
        current=confirm_field(app,ctx,case,key,value);case=current.case
    compared=command(app,ctx,'compare_offers',CompareOffersPayload(snapshot_ids=(app.release.offers[0].snapshot_id,)),case)
    selected=app.execute_action(ctx,compared.view.actions[0].action_handle);assert selected.ok
    result=command(app,ctx,'choose_branch',ChooseBranchPayload(branch='support'),selected.value.case)
    for key,value in (('region_code','ru-alt'),('route_answers.applicant_status_declared','yes'),('document_fields.applicant_name','Синтетический заявитель'),('document_fields.address',None)):
        result=confirm_field(app,ctx,result.case,key,value)
    while not any(a.label_key=='prepare' for a in result.view.actions):
        result=app.execute_action(ctx,next(a.action_handle for a in result.view.actions if a.label_key.startswith('Далее'))).value
    button=next(a for a in result.view.actions if a.label_key=='prepare')
    with app.db.uow() as u:
        payload=u.handles.resolve(ctx,button.action_handle,datetime.now(timezone.utc)).value.action.typed_payload
    prepared=app.execute_action(ctx,button.action_handle);assert prepared.ok
    replay=command(app,ctx,'confirm_result',payload,prepared.value.case)
    assert replay.created_bundle_id==prepared.value.created_bundle_id
    artifacts=publish_all(app);assert len(artifacts)==4
    with app.db.uow() as u:
        case=u.cases.get(prepared.value.case.case_id)
    current=command(app,ctx,'navigate',NavigatePayload(destination='resume'),case)
    assert current.view.kind=='materials'
    edited=confirm_field(app,ctx,current.case,'seat_width','430')
    assert edited.case.input_revision_id!=current.case.input_revision_id
    # New role selection invalidates only global role choices and keeps earlier cases.
    roles=command(app,ctx,'navigate',NavigatePayload(destination='new_case'),edited.case)
    another=app.execute_action(ctx,roles.view.actions[1].action_handle);assert another.ok
    monkeypatch.undo()
    with app.db.uow() as u:
        assert len(u.cases.list_by_owner(ctx.owner_id))==2
        assert u.bundles.get(prepared.value.created_bundle_id).status=='historical'


def test_bulk_input_invalidation_fences_running_render_and_preserves_unknown_send(app):
    ctx=actor();case,manifest=prepare_flow(app,ctx,'purchase')
    with app.db.uow() as u:
        render=u.work.claim_next('render_artifact','bulk-fence-render',datetime.now(timezone.utc),60)
        # A sending attempt belongs to the old semantic revision but must retain
        # its durable evidence so the late external result can be recorded.
        while True:
            delivery=u.work.claim_next('deliver_outbox','bulk-fence-send',datetime.now(timezone.utc),60)
            if delivery is None:raise AssertionError('current outbox unavailable')
            outbox=u.outbox.get(delivery.payload_ref)
            if outbox.intent.case_guard==case.guard and isinstance(outbox.payload,ViewModel) and outbox.payload.dialog_revision==case.dialog_revision:break
            u.work.finish_if_claim(delivery,datetime.now(timezone.utc))
        u.commit()
    permit=app.authorize_delivery(delivery);assert permit.ok,permit.error
    edited=confirm_field(app,ctx,case,'seat_width','430')
    with app.db.uow() as u:
        assert u.work.get(render.job_id).status=='cancelled'
        assert u.work.get(render.job_id).fence_token>render.fence_token
        assert u.bundles.find_by_manifest(manifest.manifest_id).status=='historical'
        assert u.outbox.get(delivery.payload_ref).status=='sending'
        assert u.work.get(delivery.job_id).status=='running'
    lost=app.get_render_context(render)
    assert not lost.ok and lost.error.code==ErrorCode.LEASE_LOST
    outcome=app.record_delivery_result(delivery,TransportResult(status='unknown',reason_code='late_uncertain_send'))
    assert outcome.ok
    with app.db.uow() as u:
        assert u.outbox.get(delivery.payload_ref).status=='delivery_unknown'
        assert u.work.get(delivery.job_id).status=='succeeded'
