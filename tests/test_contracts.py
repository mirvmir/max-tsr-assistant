from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from tsr.contracts import (
    ActorContext, Fact, Money, MoneyValue, SourceEvidence, VersionRef,
    canonical_bytes, content_hash, validate_dto, Result, ErrorCode,
)

ID = UUID('00000000-0000-0000-0000-000000000001')
NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def test_unknown_and_zero_remain_distinct_and_known_needs_evidence():
    zero = Fact[MoneyValue].known(MoneyValue(value=Money(minor=0)), (SourceEvidence(source_id='s', locator='price', observed_at=NOW),))
    unknown = Fact[MoneyValue].unknown()
    assert zero.value.value.minor == 0
    assert unknown.value is None
    assert content_hash(zero) != content_hash(unknown)
    with pytest.raises(ValidationError):
        Fact[MoneyValue](status='known', value=MoneyValue(value=Money(minor=0)), evidence=())
    with pytest.raises(ValidationError):
        Money(minor=True)
    with pytest.raises(ValidationError):
        Money(minor=9_000_000_000_000_001)


def test_schema_extra_fields_and_result_exclusivity():
    error = validate_dto('CategoryProfile', {'schema_version': '2.0.0'})
    assert not error.ok and error.error.code == ErrorCode.UNSUPPORTED_SCHEMA
    with pytest.raises(ValidationError):
        VersionRef(id='x', version='1.0.0', extra='ignored')
    with pytest.raises(ValidationError):
        Result[int](ok=True, value=1, error=error.error)
    ctx = ActorContext(owner_id=ID, delivery_target_id=ID, bot_scope='demo', case_mode='demo', correlation_id=ID)
    with pytest.raises(ValidationError):
        ctx.bot_scope = 'other'


def test_hash_canonicalizes_unicode_and_key_order_but_keeps_ordered_arrays():
    assert canonical_bytes({'b': 'e\u0301', 'a': 0}) == canonical_bytes({'a': 0, 'b': 'é'})
    assert content_hash({'items': [1, 2]}) != content_hash({'items': [2, 1]})
    with pytest.raises(ValueError):
        canonical_bytes({'price': float('nan')})


def test_settings_allow_tokenless_demo_but_reject_unreviewed_pilot_and_hide_secrets():
    from tsr.config import load_settings
    env = {'TSR_DATABASE_URL': 'postgresql://demo:secret@db/demo', 'TSR_ENCRYPTION_KEY': '01' * 32,
           'TSR_IDENTITY_HMAC_KEY': 'identity-secret', 'TSR_MODE': 'demo'}
    result = load_settings(env)
    assert result.ok and result.value.max_token is None
    assert 'identity-secret' not in repr(result.value)
    assert 'demo:secret' not in repr(result.value)
    pilot = load_settings({**env, 'TSR_MODE': 'pilot'})
    assert not pilot.ok and pilot.error.code == ErrorCode.DATA_NOT_READY
    missing = load_settings({'TSR_MODE': 'demo'})
    assert not missing.ok and 'secret' not in missing.error.model_dump_json()


def test_requirement_sets_and_long_decimals_canonicalize_without_precision_loss():
    from tsr.contracts import CodeValue, QuantityValue, SetValue
    left = SetValue(values=(CodeValue(value='b'),CodeValue(value='a')))
    right = SetValue(values=(CodeValue(value='a'),CodeValue(value='b')))
    assert content_hash(left) == content_hash(right)
    raw = '123456789012345678901234567890.1200'
    assert QuantityValue(value=raw,unit='mm').value == '123456789012345678901234567890.12'


def test_frozen_manifest_hash_binds_owner_generator_configuration_and_excludes_envelope_ids():
    from pathlib import Path
    from tsr.contracts import (
        CalculatedEvidence, ComparisonResult, DocumentManifest, InputRevision, ManifestContent,
        PricingResult,
    )
    from tsr.operations.releases import load_demo_release
    release = load_demo_release(Path(__file__).parents[1])
    offer = release.offers[0]
    matching = VersionRef(id='matching-v1',version='1.0.0')
    pricing_ref = VersionRef(id='pricing-v1',version='1.0.0')
    inp = InputRevision(input_revision_id=ID,owner_id=ID,case_id=ID,created_at=NOW,
                        category_id=offer.variant.category_id,profile_ref=offer.profile_ref,role='self')
    comparison = ComparisonResult(comparison_id=ID,input_revision_id=ID,snapshot_id=offer.snapshot_id,
        profile_ref=offer.profile_ref,matching_algorithm_ref=matching,computed_at=NOW,fields=(),classification='incomplete')
    pricing = PricingResult(pricing_algorithm_ref=pricing_ref,input_revision_id=ID,snapshot_id=offer.snapshot_id,
        certificate_use='unknown',delivery=offer.delivery,status='incomplete',
        provenance=CalculatedEvidence(algorithm=pricing_ref,input_refs=(str(ID),)))
    content = ManifestContent(owner_id=ID,case_id=ID,case_revision=1,deletion_epoch=0,case_mode='demo',
        input_revision=inp,offer_snapshot=offer,sources=release.sources.sources,
        comparison=comparison,pricing=pricing,branch='purchase',category_profile_ref=offer.profile_ref,
        matching_algorithm_ref=matching,pricing_algorithm_ref=pricing_ref,template_refs=release.template_refs,
        generator_ref=VersionRef(id='generator',version='1.0.0'),release_commit='frozen')
    original = content_hash(content)
    import json
    legacy_payload=content.model_dump(mode='json',by_alias=True)
    for field in ('catalog_ref','data_release_ref','supplier'): legacy_payload.pop(field,None)
    expected_legacy=json.dumps(legacy_payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    assert canonical_bytes(content)==expected_legacy
    enriched=ManifestContent.model_validate({**content.model_dump(mode='python'),
        'catalog_ref':release.catalog_ref,'data_release_ref':release.release_ref,
        'supplier':next(supplier for supplier in release.catalog.suppliers if supplier.supplier_id==offer.supplier_id)})
    assert content_hash(enriched)!=original
    assert content_hash(ManifestContent.model_validate(legacy_payload))==original
    with pytest.raises(ValidationError):
        ManifestContent.model_validate({**enriched.model_dump(mode='python'),
            'supplier':enriched.supplier.model_copy(update={'supplier_id':'foreign-supplier'})})
    manifest = DocumentManifest(manifest_id=ID,manifest_hash=original,confirmation_id=ID,created_at=NOW,content=content)
    assert content_hash(manifest) == original
    from tsr.contracts import ArtifactSpec,RenderPayload,RenderJobContext,TemplateRegistry
    payload=RenderPayload(manifest_id=manifest.manifest_id,bundle_id=ID,
        artifact_spec=ArtifactSpec(document_kind='purchase_card',format='pdf',template_ref=release.templates.templates[0].ref))
    assert RenderJobContext(manifest=manifest,payload=payload).template_registry is None
    context=RenderJobContext(manifest=manifest,payload=payload,template_registry=release.templates)
    restored=RenderJobContext.model_validate_json(context.model_dump_json())
    assert isinstance(restored.template_registry,TemplateRegistry)
    assert content_hash(restored.manifest)==original
    assert content_hash(manifest.model_copy(update={'manifest_id':UUID(int=20),'confirmation_id':UUID(int=21)})) == original
    assert content_hash(content.model_copy(update={'generator_ref':VersionRef(id='generator',version='1.0.1')})) != original
    assert content_hash(content.model_copy(update={'deletion_epoch':1})) != original
    new_owner=UUID(int=22)
    assert content_hash(content.model_copy(update={'owner_id':new_owner,'input_revision':inp.model_copy(update={'owner_id':new_owner})})) != original
    changed_variant=offer.variant.model_copy(update={'configuration':'another configuration'})
    assert content_hash(content.model_copy(update={'offer_snapshot':offer.model_copy(update={'variant':changed_variant})})) != original


def test_pagination_payload_binds_frozen_resource_and_rejects_unscoped_pages():
    from tsr.contracts import NavigatePayload
    payload = NavigatePayload(destination='resume',screen='candidate',resource_id=ID,page=1)
    assert payload.resource_id == ID and payload.page == 1
    assert NavigatePayload(destination='help').page == 0
    with pytest.raises(ValidationError):
        NavigatePayload(destination='resume',screen='review',page=1)
    with pytest.raises(ValidationError):
        NavigatePayload(destination='materials',screen='materials',resource_id=ID,page=1)
    with pytest.raises(ValidationError):
        NavigatePayload(destination='resume',screen='candidate',resource_id=ID,page=-1)


def test_navigation_catalog_cases_and_operational_contracts_are_bounded():
    from tsr.contracts import NavigatePayload, RetentionPolicy, WorkerHeartbeat, ReleasePackage
    assert NavigatePayload(destination='resume',screen='catalog',page=3).resource_id is None
    assert NavigatePayload(destination='cases',screen='cases',page=2).page == 2
    assert NavigatePayload(destination='new_case').page == 0
    with pytest.raises(ValidationError):
        NavigatePayload(destination='cases',screen='cases',resource_id=ID,page=1)
    assert RetentionPolicy().case_days == 90
    with pytest.raises(ValidationError):
        RetentionPolicy(dedupe_days=0)
    heartbeat = WorkerHeartbeat(worker_id='worker-1',bot_scope='demo',release_commit='test',heartbeat_at=NOW)
    assert heartbeat.capacity == {}
    with pytest.raises(TypeError):
        heartbeat.capacity['render'] = 1


def test_settings_release_selector_retention_and_health_defaults():
    from tsr.config import load_settings
    env={'TSR_DATABASE_URL':'postgresql://demo@db/demo','TSR_ENCRYPTION_KEY':'01'*32,'TSR_IDENTITY_HMAC_KEY':'opaque'}
    settings=load_settings(env).value
    assert str(settings.release_manifest)=='data/releases/demo-1.0.0/manifest.json'
    assert settings.retention_policy.case_days==90 and settings.retention_policy.inbox_hours==24
    assert settings.db_statement_timeout_ms==3000 and settings.db_lock_timeout_ms==1000
    assert settings.backup_policy.retention_days==7
    assert str(settings.deletion_journal_root)=='var/private/deletion-journal'
    selected=load_settings({**env,'TSR_RELEASE_MANIFEST':'data/releases/real-1.1.0/manifest.json',
        'TSR_RETENTION_CASE_DAYS':'45','TSR_MAINTENANCE_INTERVAL_SECONDS':'123'}).value
    assert selected.retention_policy.case_days==45 and selected.maintenance_interval_seconds==123
    assert not load_settings({**env,'TSR_WORKER_HEARTBEAT_STALE_SECONDS':'0'}).ok


def test_render_request_resolves_optional_typed_registry_without_manifest_changes():
    from pathlib import Path
    from tsr.contracts import DocumentModel, RenderRequest, TemplateRegistry
    from tsr.operations.releases import load_demo_release
    release=load_demo_release(Path(__file__).parents[1])
    model=DocumentModel(document_kind='purchase_card',case_mode='demo',sections=(),manifest_hash='a'*64)
    values=dict(job_id=ID,fence_token=1,manifest_hash=model.manifest_hash,document_kind='purchase_card',
        format='pdf',template_ref=release.templates.templates[0].ref,model=model,max_output_bytes=10000)
    legacy=RenderRequest(**values)
    assert legacy.template_registry is None
    request=RenderRequest(**values,template_registry=release.templates)
    serialized=request.model_dump_json()
    restored=RenderRequest.model_validate_json(serialized)
    assert isinstance(restored.template_registry,TemplateRegistry)
    assert restored.template_registry==release.templates


def test_operational_metrics_are_nonnegative_finite_and_contain_no_subject_identifiers():
    from tsr.contracts import HealthSnapshot,OperationMetric
    metric=OperationMetric(name='compute.render_artifact',count=2,total_seconds=1.5,
        last_seconds=0.5,max_seconds=1.0,updated_at=NOW)
    health=HealthSnapshot(status='ready',release_commit='test',db_ready=True,operation_metrics=(metric,))
    assert health.operation_metrics[0].count==2
    assert not hasattr(metric,'owner_id') and not hasattr(metric,'job_id')
    with pytest.raises(ValidationError):
        OperationMetric(name='compute.render_artifact',count=1,total_seconds=float('inf'),
            last_seconds=0.5,max_seconds=1.0,updated_at=NOW)
    with pytest.raises(ValidationError):
        OperationMetric(name='queue.process_inbox',count=-1,total_seconds=0,last_seconds=0,max_seconds=0,updated_at=NOW)
