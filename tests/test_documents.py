from hashlib import sha256
from io import BytesIO
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path

from docx import Document

from tsr.adapters.render import render_document
from tsr.contracts import DocumentField, DocumentModel, DocumentSection, RenderRequest, VersionRef


def demo_model():
    return DocumentModel(
        schema_version='1.0.0', document_kind='application', case_mode='demo',
        manifest_hash='a' * 64, missing_fields=('applicant_name',), sources=(),
        warnings=('Модельный маршрут не является утверждённым обращением.',),
        sections=(DocumentSection(title='Черновик обращения', text='Не является официальной формой.',
                  fields=(DocumentField(label='Заявитель', value='Не указано'),
                          DocumentField(label='Пояснение', value='<script>Кириллица</script> ' * 100))),),
    )


def request(model, fmt='pdf', limit=2_000_000):
    return RenderRequest(job_id=uuid4(), fence_token=2, manifest_hash=model.manifest_hash,
                         document_kind=model.document_kind, format=fmt,
                         template_ref=VersionRef(id='demo-application', version='1.0.0'),
                         model=model, max_output_bytes=limit)


def test_render_cyrillic_literal_markup_docx_and_byte_bound():
    model = demo_model()
    result = render_document(request(model, 'docx'))
    assert result.ok, result.error
    rendered = result.value
    assert rendered.plaintext_sha256 == sha256(rendered.bytes).hexdigest()
    paragraphs = '\n'.join(p.text for p in Document(BytesIO(rendered.bytes)).paragraphs)
    assert '<script>Кириллица</script>' in paragraphs
    assert 'Не указано' in paragraphs
    assert 'ДЕМОНСТРАЦИОННЫЙ' in paragraphs
    assert 'ФИО заявителя: Не указано' in paragraphs
    assert document_title_has_no_border(Document(BytesIO(rendered.bytes)))
    assert not render_document(request(model, 'pdf', limit=100)).ok


def document_title_has_no_border(document):
    from docx.oxml.ns import qn
    return not list(document.styles['Title']._element.iter(qn('w:pBdr')))


def test_pdf_has_cyrillic_font_and_binds_manifest_hash():
    model = demo_model()
    result = render_document(request(model))
    assert result.ok, result.error
    assert result.value.bytes.startswith(b'%PDF-')
    assert b'DejaVuSans' in result.value.bytes
    mismatched = request(model).model_copy(update={'manifest_hash': 'b' * 64})
    assert not render_document(mismatched).ok
    pilot = demo_model().model_copy(update={'case_mode':'pilot'})
    assert not render_document(request(pilot)).ok


def test_invalid_table_validation_is_a_safe_typed_report():
    from tsr.domain.documents import validate_document_model
    from tsr.contracts import DocumentTable
    invalid = demo_model().model_copy(update={'sections':(DocumentSection(
        tables=(DocumentTable(headers=('A','B'),rows=(('single',),)),)),)})
    report = validate_document_model(invalid, None)
    assert not report.valid
    assert report.errors[0].code == 'invalid_table_shape'


def frozen_manifest(branch='support'):
    from tsr.contracts import (CalculatedEvidence, ComparisonResult, DocumentManifest, Fact,
        InputRevision, ManifestContent, OfferSnapshot, PricingResult, RouteEvaluation, SourceMetadata,
        content_hash)
    import json
    root = Path(__file__).resolve().parents[1]
    offer = OfferSnapshot.model_validate(json.loads((root / 'data/catalog/mvp/1.0.0/catalog.json').read_text())['offers'][0])
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    owner, case, revision = uuid4(), uuid4(), uuid4()
    inp = InputRevision(input_revision_id=revision, owner_id=owner, case_id=case, created_at=now,
                        category_id=offer.variant.category_id, profile_ref=offer.profile_ref,
                        role='representative', region_code=None, document_fields={'address':'Длинный адрес ' * 30})
    matching, pricing_ref = VersionRef(id='matching-v1',version='1.0.0'),VersionRef(id='pricing-v1',version='1.0.0')
    comparison=ComparisonResult(comparison_id=uuid4(),input_revision_id=revision,snapshot_id=offer.snapshot_id,
        profile_ref=offer.profile_ref,matching_algorithm_ref=matching,computed_at=now,fields=(),classification='incomplete')
    pricing=PricingResult(pricing_algorithm_ref=pricing_ref,input_revision_id=revision,snapshot_id=offer.snapshot_id,
        certificate_use='unknown',delivery=offer.delivery,status='incomplete',
        provenance=CalculatedEvidence(algorithm=pricing_ref,input_refs=(str(revision),)))
    route=RouteEvaluation(status='needs_clarification',addressee=Fact.unknown(),evaluated_at=now,
                         missing_fields=('region_code',)) if branch=='support' else None
    template_ids=['demo-purchase-card'] if branch=='purchase' else ['demo-application','demo-product-card','demo-checklist']
    content=ManifestContent(owner_id=owner,case_id=case,case_revision=2,deletion_epoch=0,case_mode='demo',
        input_revision=inp,offer_snapshot=offer,sources=(SourceMetadata(source_id='synthetic',title='Синтетический источник'),),
        comparison=comparison,pricing=pricing,route=route,branch=branch,category_profile_ref=offer.profile_ref,
        matching_algorithm_ref=matching,pricing_algorithm_ref=pricing_ref,
        template_refs=tuple(VersionRef(id=key,version='1.0.0') for key in template_ids),
        generator_ref=VersionRef(id='generator-v1',version='1.0.0'),release_commit='frozen-release',missing_fields=('region_code',))
    return DocumentManifest(manifest_id=uuid4(),confirmation_id=uuid4(),created_at=now,
                            manifest_hash=content_hash(content),content=content)


def test_bundle_branch_composition_frozen_content_and_unknowns():
    from tsr.domain.documents import build_document_model, build_document_request
    purchase = frozen_manifest('purchase')
    assert [(x.document_kind,x.format) for x in build_document_request(purchase).required_artifacts] == [('purchase_card','pdf')]
    support = frozen_manifest()
    assert [(x.document_kind,x.format) for x in build_document_request(support).required_artifacts] == [
        ('application','docx'),('application','pdf'),('product_card','pdf'),('checklist','pdf')]
    model = build_document_model(support,'application')
    assert model.ok, model.error
    assert 'applicant_name' in model.value.missing_fields
    assert 'addressee' in model.value.missing_fields
    assert 'region_code' in model.value.missing_fields
    text = '\n'.join(p.value for s in model.value.sections for p in s.fields)
    assert 'Представитель' in text
    assert 'Неизвестно' in text
    assert 'frozen-release' in text
    assert any('Модельный маршрут' in warning for warning in model.value.warnings)
    changed = support.model_copy(update={'manifest_hash':'b'*64})
    assert not build_document_model(changed,'application').ok
