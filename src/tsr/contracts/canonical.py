"""Deterministic JSON projections. Envelope IDs never enter manifest content hashes."""
from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import unicodedata
from uuid import UUID
from pydantic import BaseModel, ValidationError
from . import models


def candidate_projection(candidate):
    return {'owner_id':candidate.owner_id,'case_id':candidate.case_id,'field_key':candidate.field_key,
            'value':candidate.value,'case_guard':candidate.case_guard,'dialog_revision':candidate.dialog_revision}


def _plain(value):
    if isinstance(value,BaseModel): return _plain(value.model_dump(mode='python',by_alias=True))
    if isinstance(value,Mapping):
        normalized={unicodedata.normalize('NFC',str(k)):_plain(v) for k,v in value.items()}
        if len(normalized)!=len(value): raise ValueError('duplicate normalized keys')
        return normalized
    if isinstance(value,(tuple,list)): return [_plain(v) for v in value]
    if isinstance(value,(set,frozenset)):
        return sorted((_plain(v) for v in value),key=lambda v:json.dumps(v,sort_keys=True,ensure_ascii=False))
    if isinstance(value,str): return unicodedata.normalize('NFC',value)
    if isinstance(value,UUID): return str(value)
    if isinstance(value,datetime):
        if value.tzinfo is None: raise ValueError('UTC timestamp required')
        return value.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    if isinstance(value,Decimal): return models.normalize_decimal(format(value,'f'))
    if isinstance(value,Enum): return _plain(value.value)
    if isinstance(value,bytes): raise ValueError('bytes are not a JSON content contract')
    return value


def canonical_bytes(value,projection=None):
    if projection is not None:
        if callable(projection): value=projection(value)
        elif projection=='candidate': value=candidate_projection(value)
        elif projection=='manifest': value=value.content if isinstance(value,models.DocumentManifest) else value
        else: raise ValueError('unknown canonical projection')
    elif isinstance(value,models.DocumentManifest): value=value.content
    elif isinstance(value,models.Candidate): value=candidate_projection(value)
    return json.dumps(_plain(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def content_hash(value,projection=None):
    return hashlib.sha256(canonical_bytes(value,projection)).hexdigest()


def validate_dto(type_name,payload):
    cls=getattr(models,type_name,None)
    if cls is None or not isinstance(cls,type) or not issubclass(cls,BaseModel):
        return models.Result.failure(models.ErrorCode.VALIDATION_ERROR)
    if isinstance(payload,Mapping) and 'schema_version' in payload:
        version=payload['schema_version']
        if not isinstance(version,str) or version.split('.')[0]!='1':
            return models.Result.failure(models.ErrorCode.UNSUPPORTED_SCHEMA)
    try: return models.Result.success(cls.model_validate(payload))
    except (ValidationError,ValueError,TypeError): return models.Result.failure(models.ErrorCode.VALIDATION_ERROR)
