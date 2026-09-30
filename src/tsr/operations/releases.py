"""Fail-closed versioned fixture import; no network loads or executable assets."""
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from tsr.contracts import (CatalogPack, CategoryProfile, DemoCasePack, DemoRelease,
    FieldError, ImportReport, LifecycleRecord, ReleasePackManifest, RoutePack,
    SourcesRegistry, TemplateRegistry, VersionRef, ReleasePackage, ReleaseRecord, ReleaseLifecycle, Result, content_hash, runtime_dependency_refs, check_release_policy, ensure_active_release_ready)
from tsr.domain.catalog import validate_catalog
from tsr.domain.routes import validate_route_pack

MAX_FILE_BYTES=10*1024*1024
PACKAGE_MODELS={'CategoryProfile':CategoryProfile,'CatalogPack':CatalogPack,
    'RoutePack':RoutePack,'SourcesRegistry':SourcesRegistry,'TemplateRegistry':TemplateRegistry,
    'DemoCasePack':DemoCasePack,'ReleasePackManifest':ReleasePackManifest}
REQUIRED_KINDS=frozenset({'CategoryProfile','CatalogPack','RoutePack','SourcesRegistry',
    'TemplateRegistry','DemoCasePack','GoldenCasePack'})


def safe_path(root:Path,relative:str)->Path:
    path=Path(relative)
    if path.is_absolute() or '..' in path.parts or not relative or '\\' in relative:
        raise ValueError('unsafe path')
    resolved=(root/path).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError('unsafe path')
    return resolved


def _read(root,path):
    target=safe_path(root,path)
    if target.stat().st_size>MAX_FILE_BYTES:
        raise ValueError('file size')
    raw=target.read_bytes()
    return raw,json.loads(raw)


def _error(path,code):
    return FieldError(path=path,code=code,safe_message_key='data.invalid')


def _review_errors(pack,path,now):
    errors=[]
    review=pack.review
    if pack.data_kind=='synthetic' and review.status!='draft':
        errors.append(_error(path+'.review','SYNTHETIC_CANNOT_BE_REVIEWED'))
    if review.status=='reviewed':
        if not review.reviewer_id or review.reviewed_at is None or review.review_due_at is None or not review.reviewed_at<=now<review.review_due_at:
            errors.append(_error(path+'.review','INVALID_REVIEW'))
        observed=getattr(pack,'observed_at',getattr(pack,'captured_at',None))
        if observed and review.reviewed_at and observed>review.reviewed_at:
            errors.append(_error(path+'.observed_at','INVALID_REVIEW'))
    return errors


GOLDEN_FIELDS={
    'first_purchase':({'class','coverage_minor','gap_minor','total_minor'},
        {'class','price_minor','certificate_minor','coverage_minor','gap_minor','total_minor','certificate_use','pricing_status','route_status'}),
    'large_total':({'price_minor','delivery_minor','certificate_applicable_declared','total_minor'},
        {'price_minor','delivery_minor','certificate_applicable_declared','total_minor'}),
    'mismatch':({'class'},{'class','field_key','required_mm','offer_mm'}),
    'unknown_offer':({'price_kind','price_minor','coverage_minor','gap_minor','total_minor'},
        {'class','price_kind','price_minor','coverage_minor','gap_minor','total_minor'}),
    'certificate_not_applicable':({'certificate_applicable_declared','certificate_use','coverage_minor','gap_minor','total_minor'},
        {'certificate_applicable_declared','certificate_use','coverage_minor','gap_minor','total_minor','price_minor'}),
    'pilot_draft_block':({'error_code'},{'error_code'}),
}


def _golden_expected_errors(case,path,field_keys):
    """Validate independent expected results by scenario; never compute their oracle."""
    expected=case.get('expected')
    definition=GOLDEN_FIELDS.get(case.get('scenario'))
    invalid=False
    if not isinstance(expected,dict) or definition is None:
        return [_error(path+'.expected','INVALID_GOLDEN_EXPECTED')]
    required,allowed=definition
    if required-expected.keys() or expected.keys()-allowed:
        invalid=True
    money_keys={'price_minor','certificate_minor','coverage_minor','gap_minor','total_minor','delivery_minor'}
    nullable={'price_minor','coverage_minor','gap_minor','total_minor'}
    for key,value in expected.items():
        if key in money_keys:
            if value is None:
                if key not in nullable: invalid=True
            elif type(value) is not int or not 0<=value<=9_000_000_000_000_000:
                invalid=True
            elif key in {'price_minor','certificate_minor','delivery_minor'} and value>10_000_000_000:
                invalid=True
    enums={
        'class':{'complete','incomplete','mismatch'},
        'price_kind':{'exact','from','unknown'},
        'certificate_applicable_declared':{'yes','no','unknown'},
        'certificate_use':{'allowed_by_declared_data','conditional','not_accepted','not_applicable','unknown'},
        'pricing_status':{'calculated','conditional','incomplete'},
        'route_status':{'preliminary_match','blocked','needs_clarification','not_covered'},
    }
    for key,options in enums.items():
        if key in expected and (not isinstance(expected[key],str) or expected[key] not in options): invalid=True
    if 'field_key' in expected and (not isinstance(expected['field_key'],str) or expected['field_key'] not in field_keys): invalid=True
    for key in ('required_mm','offer_mm'):
        if key in expected and (not isinstance(expected[key],str) or not re.fullmatch(r'\d+(?:\.\d+)?',expected[key])): invalid=True
    scenario=case['scenario']
    if scenario=='mismatch' and expected.get('class')!='mismatch': invalid=True
    if scenario=='unknown_offer' and isinstance(expected.get('price_kind'),str) and expected.get('price_kind') in {'from','unknown'} and any(expected.get(key) is not None for key in ('price_minor','coverage_minor','gap_minor','total_minor')): invalid=True
    if scenario=='certificate_not_applicable' and (expected.get('certificate_applicable_declared')!='no' or expected.get('certificate_use')!='not_applicable' or expected.get('coverage_minor')!=0): invalid=True
    if scenario=='pilot_draft_block' and expected.get('error_code')!='DATA_NOT_READY': invalid=True
    return [_error(path+'.expected','INVALID_GOLDEN_EXPECTED')] if invalid else []


def validate_release(root,manifest,now=None):
    now=now or datetime.now(timezone.utc)
    errors=[]
    warnings=['release.synthetic_demo_only'] if manifest.data_kind=='synthetic' else []
    ref=VersionRef(id=manifest.release_id,version=manifest.version)
    loaded={}
    seen_paths=set()
    seen_refs=set()
    try:
        schema=json.loads(safe_path(root,'schemas/mvp-data.schema.json').read_bytes())
    except (OSError,ValueError,json.JSONDecodeError):
        return ImportReport(release_ref=ref,status='rejected',files=(),errors=(_error('schema','SCHEMA_UNAVAILABLE'),),warnings=tuple(warnings))
    for index,entry in enumerate(manifest.files):
        path=f'files[{index}]'
        if entry.path in seen_paths:
            errors.append(_error(path+'.path','DUPLICATE_ID'))
        seen_paths.add(entry.path)
        identity=(entry.ref.id,entry.ref.version)
        if identity in seen_refs:
            errors.append(_error(path+'.ref','DUPLICATE_ID'))
        seen_refs.add(identity)
        try:
            target=safe_path(root,entry.path)
            if target.stat().st_size>MAX_FILE_BYTES: raise ValueError('size')
            raw=target.read_bytes()
        except (OSError,ValueError):
            errors.append(_error(path+'.path','UNSAFE_OR_MISSING_PATH'))
            continue
        if hashlib.sha256(raw).hexdigest()!=entry.sha256:
            errors.append(_error(path+'.sha256','HASH_MISMATCH'))
            continue
        if entry.kind=='asset':
            continue
        try:
            content=json.loads(raw)
            if entry.kind not in schema['$defs']:
                errors.append(_error(path+'.kind','UNSUPPORTED_SCHEMA'))
                continue
            selected={'$schema':schema['$schema'],'$defs':schema['$defs'],'$ref':'#/$defs/'+entry.kind}
            validation=list(Draft202012Validator(selected).iter_errors(content))
            if validation:
                errors.extend(_error(path+'.'+'.'.join(str(value) for value in issue.absolute_path),'SCHEMA_VALIDATION') for issue in validation)
                continue
            pack=PACKAGE_MODELS[entry.kind].model_validate(content) if entry.kind in PACKAGE_MODELS else content
        except (ValidationError,ValueError,json.JSONDecodeError):
            errors.append(_error(path,'SCHEMA_VALIDATION'))
            continue
        if entry.kind in loaded:
            errors.append(_error(path+'.kind','DUPLICATE_ID'))
            continue
        loaded[entry.kind]=pack
        actual_id=next((getattr(pack,key) for key in ('profile_id','catalog_id','route_id','registry_id','pack_id') if hasattr(pack,key)),None)
        if actual_id is None and isinstance(pack,dict): actual_id=pack.get('pack_id')
        version=pack.get('version') if isinstance(pack,dict) else pack.version
        if actual_id!=entry.ref.id or version!=entry.ref.version:
            errors.append(_error(path+'.ref','REFERENCE_MISMATCH'))
        if not isinstance(pack,dict):
            errors.extend(_review_errors(pack,entry.path,now))
            # Mixed demo releases retain each subject's explicit data_kind/review.
            # Activation, rather than shape validation, rejects public drafts.
    for kind in REQUIRED_KINDS-loaded.keys():
        errors.append(_error('files.'+kind,'MISSING_PACKAGE'))
    if not errors:
        profile=loaded['CategoryProfile'];sources=loaded['SourcesRegistry'];catalog=loaded['CatalogPack'];route=loaded['RoutePack'];templates=loaded['TemplateRegistry'];demo=loaded['DemoCasePack']
        keys=[rule.field_key for rule in profile.fields]
        if len(keys)!=len(set(keys)): errors.append(_error('profile.fields','DUPLICATE_ID'))
        source_ids=[source.source_id for source in sources.sources]
        if len(source_ids)!=len(set(source_ids)): errors.append(_error('sources','DUPLICATE_ID'))
        for rule in profile.fields:
            allowed={'quantity':{'eq','gte','lte','within_range'},'code':{'eq','in_set'},'boolean':{'eq'},'text':{'eq'}}
            if rule.operator not in allowed.get(rule.value_kind,set()) or (rule.value_kind=='quantity')!=(rule.unit is not None): errors.append(_error('profile.fields.'+rule.field_key,'INVALID_OPERATOR_OR_UNIT'))
            if rule.tolerance is not None and profile.review.status!='reviewed': errors.append(_error('profile.fields.'+rule.field_key+'.tolerance','UNREVIEWED_TOLERANCE'))
            if set(rule.source_ids)-set(source_ids): errors.append(_error('profile.fields.'+rule.field_key+'.source_ids','INVALID_REFERENCE'))
        errors.extend(validate_catalog(catalog,(profile,),sources).errors)
        errors.extend(validate_route_pack(route,sources,templates).errors)
        for offer in catalog.offers: errors.extend(_review_errors(offer,'catalog.'+str(offer.snapshot_id),now))
        for source in sources.sources:
            errors.extend(_review_errors(source,'sources.'+source.source_id,now))
            if not source.use_basis or source.data_kind=='public_snapshot' and source.use_basis=='synthetic':
                errors.append(_error('sources.'+source.source_id+'.use_basis','USAGE_BASIS_MISSING'))
        template_ids=[]
        renderer_formats={'reportlab-v1':'pdf','python-docx-v1':'docx'}
        kinds_formats=set()
        for template in templates.templates:
            template_ids.append(template.template_id)
            formats=set()
            for asset in template.assets:
                if asset.format in formats: errors.append(_error('templates.'+template.template_id,'DUPLICATE_FORMAT'))
                formats.add(asset.format)
                pair=(template.document_kind,asset.format)
                if pair in kinds_formats: errors.append(_error('templates.'+template.template_id,'AMBIGUOUS_ARTIFACT'))
                kinds_formats.add(pair)
                if renderer_formats.get(asset.renderer_id)!=asset.format: errors.append(_error('templates.'+template.template_id,'INVALID_RENDERER'))
                try:
                    raw=safe_path(root,asset.path).read_bytes()
                    if len(raw)>MAX_FILE_BYTES or not asset.sha256 or hashlib.sha256(raw).hexdigest()!=asset.sha256: errors.append(_error('templates.'+template.template_id,'HASH_MISMATCH'))
                except (ValueError,OSError): errors.append(_error('templates.'+template.template_id,'UNSAFE_OR_MISSING_PATH'))
        if len(template_ids)!=len(set(template_ids)): errors.append(_error('templates','DUPLICATE_ID'))
        required_artifacts={('purchase_card','pdf'),('application','pdf'),('application','docx'),('product_card','pdf'),('checklist','pdf')}
        if required_artifacts-kinds_formats: errors.append(_error('templates','MISSING_ARTIFACT'))
        for name in ('assets/fonts/DejaVuSans.ttf','assets/fonts/DejaVuSans-Bold.ttf','assets/fonts/LICENSE-DejaVu.txt'):
            if name not in seen_paths: errors.append(_error('assets.fonts','MISSING_ASSET'))
        profile_ref=VersionRef(id=profile.profile_id,version=profile.version)
        for index,item in enumerate(demo.profiles):
            path=f'demo.profiles[{index}]'
            if not item.is_synthetic or item.profile_ref!=profile_ref or item.category_id!=profile.category_id: errors.append(_error(path,'INVALID_REFERENCE'))
            if set(item.prescribed)&set(item.unspecified_fields) or set(item.prescribed)-set(keys): errors.append(_error(path+'.prescribed','INVALID_INPUT'))
            if item.certificate_amount and item.certificate_amount.minor>10000000000: errors.append(_error(path+'.certificate_amount','INPUT_LIMIT'))
            rules={rule.field_key:rule for rule in profile.fields}
            for key,value in item.prescribed.items():
                rule=rules.get(key)
                if rule is None: continue
                if value.kind in ('range','set'):
                    if value.kind=='range' and (rule.operator!='within_range' or value.unit!=rule.unit): errors.append(_error(path+'.prescribed.'+key,'INVALID_VALUE'))
                    if value.kind=='set' and rule.operator!='in_set': errors.append(_error(path+'.prescribed.'+key,'INVALID_VALUE'))
                elif value.kind!=rule.value_kind or value.kind=='quantity' and value.unit!=rule.unit: errors.append(_error(path+'.prescribed.'+key,'INVALID_VALUE'))
        golden=loaded['GoldenCasePack']
        known_scenarios=set(GOLDEN_FIELDS)
        snapshot_ids={str(offer.snapshot_id) for offer in catalog.offers}
        for index,case in enumerate(golden['cases']):
            path=f'golden.cases[{index}]'
            errors.extend(_golden_expected_errors(case,path,set(keys)))
            if case['scenario'] not in known_scenarios or not case.get('expected') or case.get('snapshot_id') not in snapshot_ids or case.get('profile_ref')!=profile_ref.model_dump(): errors.append(_error(path,'INVALID_GOLDEN_CASE'))
            if case.get('route_ref') is not None and case['route_ref']!=route.ref.model_dump():
                errors.append(_error(path+'.route_ref','INVALID_REFERENCE'))
            if 'error_code' in case.get('expected',{}):
                from tsr.contracts import ErrorCode
                if not isinstance(case['expected']['error_code'],str) or case['expected']['error_code'] not in {code.value for code in ErrorCode}:
                    errors.append(_error(path+'.expected.error_code','INVALID_ERROR_CODE'))
    errors.extend(_review_errors(manifest,'manifest',now))
    return ImportReport(release_ref=ref,status='rejected' if errors else 'valid',files=tuple(entry.path for entry in manifest.files),errors=tuple(errors),warnings=tuple(warnings))


def _load_packages(root,manifest):
    packages=[]
    for entry in manifest.files:
        if entry.kind not in PACKAGE_MODELS or entry.kind=='ReleasePackManifest':
            continue
        raw,payload=_read(root,entry.path)
        if hashlib.sha256(raw).hexdigest()!=entry.sha256:
            raise ValueError('HASH_MISMATCH')
        packages.append(ReleasePackage(kind=entry.kind,ref=entry.ref,
            content=PACKAGE_MODELS[entry.kind].model_validate(payload)))
    return tuple(packages)


def load_release(root:Path,manifest_path='data/releases/demo-1.0.0/manifest.json')->DemoRelease:
    """Load a specific immutable release; an active record never falls back to demo.

    Public package payloads from a ReleaseRecord are authoritative after their
    identity and equality with the hash-pinned deployment files have been checked.
    Legacy records without embedded payloads use their own pinned manifest paths.
    """
    root=Path(root)
    record=manifest_path if isinstance(manifest_path,ReleaseRecord) else None
    if record is not None:
        manifest=record.manifest
        if record.ref!=manifest.ref or record.content_hash!=content_hash(manifest):
            raise ValueError('RELEASE_RECORD_HASH_MISMATCH')
    elif isinstance(manifest_path,ReleasePackManifest):
        manifest=manifest_path
    else:
        _,payload=_read(root,str(manifest_path))
        manifest=ReleasePackManifest.model_validate(payload)
    report=validate_release(root,manifest)
    if report.status!='valid':
        raise ValueError('Release validation failed: '+','.join(f'{error.path}:{error.code}' for error in report.errors))
    packages=_load_packages(root,manifest)
    if record is not None and record.packages:
        if len(record.packages)!=len(packages) or {p.kind:p for p in record.packages}!={p.kind:p for p in packages}:
            raise ValueError('RELEASE_PACKAGE_CONTENT_MISMATCH')
        packages=record.packages
    packs={package.kind:package.content for package in packages}
    route=packs['RoutePack'];catalog=packs['CatalogPack'];templates=packs['TemplateRegistry']
    return DemoRelease(profile=packs['CategoryProfile'],offers=catalog.offers,route=route,sources=packs['SourcesRegistry'],
        template_refs=tuple(item.ref for item in templates.templates),
        release_ref=manifest.ref,catalog_ref=catalog.ref,
        route_lifecycle=LifecycleRecord(ref=route.ref,status='active'),
        manifest=manifest,catalog=catalog,templates=templates,demo_profiles=packs['DemoCasePack'].profiles)


def load_demo_release(root:Path)->DemoRelease:
    """Compatibility entrypoint for the unchanged original synthetic fixture."""
    release=load_release(root)
    if release.manifest.data_kind!='synthetic' or release.manifest.review.status!='draft':
        raise ValueError('Demo release must be explicitly synthetic draft')
    return release


def _validated_record(root,manifest,now):
    packages=_load_packages(root,manifest)
    packs={package.kind:package.content for package in packages}
    subjects=[pack for kind,pack in packs.items() if kind not in ('DemoCasePack','ReleasePackManifest')]
    subjects.extend(packs['CatalogPack'].offers)
    subjects.extend(packs['SourcesRegistry'].sources)
    return ReleaseRecord(ref=manifest.ref,manifest=manifest,validated_at=now,
        package_reviews=tuple(subject.review for subject in subjects),
        package_data_kinds=tuple(subject.data_kind for subject in subjects),
        assets_verified=True,content_hash=content_hash(manifest),packages=packages)



def import_release(root,manifest,actor_key,*,db,now=None):
    """Stage validated immutable metadata in a single PostgreSQL transaction."""
    now=now or datetime.now(timezone.utc)
    report=validate_release(root,manifest,now)
    if report.status!='valid': return report
    record=_validated_record(root,manifest,now)
    # Recheck after reading the DTOs; mutation between reads must not change content.
    for entry in manifest.files:
        if hashlib.sha256(safe_path(root,entry.path).read_bytes()).hexdigest()!=entry.sha256:
            return report.model_copy(update={'status':'rejected','errors':(_error(entry.path,'HASH_MISMATCH'),)})
    with db.uow() as uow:
        if not uow.restore_ready():
            return report.model_copy(update={'status':'rejected','errors':(_error('release','RESTORE_NOT_READY'),)})
        existing=uow.releases.get(record.ref)
        if existing is not None and existing.content_hash==record.content_hash:
            record=existing
        uow.releases.insert(record)
        uow.commit()
    return report.model_copy(update={'status':'staged'})


def activate_release(ref,actor_key,mode,now,*,db,allow_synthetic_draft=False,only_if_empty=False,bot_scope='default'):
    from tsr.contracts import Result
    try:
        with db.uow() as uow:
            if not uow.restore_ready():
                return Result.failure('DATA_NOT_READY',safe_message_key='startup.restore_not_ready')
            receipt=uow.releases.activate(ref,mode,actor_key,now,allow_synthetic_draft=allow_synthetic_draft,only_if_empty=only_if_empty,bot_scope=bot_scope)
            uow.commit()
        return Result.success(receipt)
    except ValueError as exc:
        code=str(exc)
        allowed={'VALIDATION_ERROR','DATA_NOT_READY','DATA_REVOKED','DATA_EXPIRED','NOT_FOUND'}
        return Result.failure(code if code in allowed else 'VALIDATION_ERROR')


def revoke_package(ref,reason,actor_key,now,*,db):
    from tsr.contracts import Result
    try:
        with db.uow() as uow:
            if not uow.restore_ready():
                return Result.failure('DATA_NOT_READY',safe_message_key='startup.restore_not_ready')
            receipt=uow.releases.revoke(ref,reason,actor_key,now)
            uow.commit()
        return Result.success(receipt)
    except ValueError as exc:
        return Result.failure('NOT_FOUND' if str(exc)=='NOT_FOUND' else 'VALIDATION_ERROR')


def rollback_release(ref,actor_key,mode,now,*,db,allow_synthetic_draft=False,bot_scope='default'):
    """Rollback is ordinary checked activation; it never clears lifecycle state."""
    return activate_release(ref,actor_key,mode,now,db=db,allow_synthetic_draft=allow_synthetic_draft,bot_scope=bot_scope)
