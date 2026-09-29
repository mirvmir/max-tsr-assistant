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
    assert any("ИСТОРИЧЕСКИЙ" in section.parameters.get("text","") for section in materials.view.sections)
    delete_prompt=next(a for a in materials.view.actions if a.label_key=="delete")
    prompt=app.execute_action(ctx,delete_prompt.action_handle)
    assert prompt.ok,prompt.error
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
        assert u.inbox.get(inbox_id).status=="processed"
        assert u.work.get(claim.job_id).status=="succeeded"
    return result.value


def test_verified_start_requires_explicit_role_and_errors_offer_recovery(app):
    ctx=actor()
    started=process_event(app,ctx,"start",StartEvent())
    assert started.case is None
    assert {a.label_key for a in started.view.actions}=={"Для себя","Для другого человека"}
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
    assert activate_release(app.release.release_ref,"test","demo",now,db=app.db,allow_synthetic_draft=True).ok
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
    assert changed.value.case.selected_snapshot_id==app.release.offers[1].snapshot_id
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
