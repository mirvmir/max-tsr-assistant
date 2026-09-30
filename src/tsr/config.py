"""Explicit startup configuration. Importing this module never reads secrets."""
from __future__ import annotations
import base64
from collections.abc import Callable, Mapping
from pathlib import Path
import os
from typing import Literal
from pydantic import Field, SecretStr, ValidationError, model_validator
from tsr.contracts import DTO, ErrorCode, EvaluationPolicy, Result, RetentionPolicy, BackupPolicy


class Settings(DTO):
    database_url: str = Field(repr=False)
    mode: Literal['demo','pilot'] = 'demo'
    allow_synthetic_draft: bool = True
    encryption_key: SecretStr
    identity_hmac_key: SecretStr
    max_token: SecretStr | None = None
    max_webhook_secret: SecretStr | None = None
    readiness_secret: SecretStr | None = None
    max_api_url: str = 'https://platform-api2.max.ru'
    max_ca_bundle: Path | None = None
    webhook_url: str | None = None
    bot_scope: str = 'tsr-demo'
    private_root: Path = Path('var/private')
    release_root: Path = Path('.')
    release_manifest: Path = Path('data/releases/demo-1.0.0/manifest.json')
    backup_root: Path = Path('var/private/backups')
    deletion_journal_root: Path = Path('var/private/deletion-journal')
    db_connect_timeout_seconds: int = Field(default=5,gt=0)
    db_statement_timeout_ms: int = Field(default=3000,gt=0)
    db_lock_timeout_ms: int = Field(default=1000,gt=0)
    db_idle_transaction_timeout_ms: int = Field(default=5000,gt=0)
    db_backup_snapshot_timeout_ms: int = Field(default=300000,gt=0)
    max_backup_bytes: int = Field(default=536870912,gt=0)
    maintenance_interval_seconds: int = Field(default=300,gt=0)
    worker_heartbeat_stale_seconds: int = Field(default=60,gt=0)
    subscription_health_ttl_seconds: int = Field(default=300,gt=0)
    retention_case_days: int = Field(default=90,gt=0)
    retention_artifact_days: int = Field(default=7,gt=0)
    retention_inbox_hours: int = Field(default=24,gt=0)
    retention_dedupe_days: int = Field(default=30,gt=0)
    retention_backup_days: int = Field(default=7,gt=0)
    orphan_grace_seconds: int = Field(default=300,gt=0)
    retention_batch_size: int = Field(default=100,gt=0,le=1000)
    release_commit: str = 'development'
    candidate_ttl_seconds: int = Field(default=900,gt=0)
    preview_ttl_seconds: int = Field(default=900,gt=0)
    handle_ttl_seconds: int = Field(default=900,gt=0)
    artifact_ttl_seconds: int = Field(default=604800,gt=0)
    max_body_bytes: int = Field(default=131072,gt=0)
    max_input_chars: int = Field(default=2000,gt=0)
    max_file_bytes: int = Field(default=10_485_760,gt=0)
    lease_seconds: int = Field(default=60,gt=0)
    heartbeat_seconds: int = Field(default=15,gt=0)
    max_attempts: int = Field(default=5,ge=1,le=20)
    render_slots: Literal[1] = 1
    render_timeout_seconds: float = Field(default=45,gt=0,allow_inf_nan=False)
    network_timeout_seconds: float = Field(default=15,gt=0)
    reviewed_data_ready: bool = False
    privacy_ready: bool = False

    @property
    def evaluation_policy(self):
        return EvaluationPolicy(case_mode=self.mode,allow_synthetic_draft=self.allow_synthetic_draft)

    @property
    def backup_policy(self):
        return BackupPolicy(retention_days=self.retention_backup_days,max_archive_bytes=self.max_backup_bytes,max_blob_bytes=self.max_file_bytes)

    @property
    def retention_policy(self):
        return RetentionPolicy(case_days=self.retention_case_days,artifact_days=self.retention_artifact_days,
            inbox_hours=self.retention_inbox_hours,dedupe_days=self.retention_dedupe_days,backup_days=self.retention_backup_days,
            worker_heartbeat_seconds=self.worker_heartbeat_stale_seconds,orphan_grace_seconds=self.orphan_grace_seconds,batch_size=self.retention_batch_size)

    @property
    def encryption_key_bytes(self):
        value=self.encryption_key.get_secret_value()
        try: key=bytes.fromhex(value)
        except ValueError:
            try: key=base64.b64decode(value,validate=True)
            except Exception: raise ValueError('invalid encryption key') from None
        if len(key)!=32: raise ValueError('encryption key must contain 32 bytes')
        return key

    @property
    def max_base_url(self): return self.max_api_url
    @property
    def case_mode(self): return self.mode

    @model_validator(mode='after')
    def safety(self):
        self.encryption_key_bytes
        if not self.database_url.startswith(('postgresql://','postgres://')): raise ValueError('PostgreSQL required')
        if not self.identity_hmac_key.get_secret_value(): raise ValueError('identity key required')
        if self.heartbeat_seconds>=self.lease_seconds: raise ValueError('heartbeat must be shorter than lease')
        if self.webhook_url and not self.webhook_url.startswith('https://'): raise ValueError('HTTPS webhook required')
        if self.mode=='pilot':
            if not self.reviewed_data_ready or not self.privacy_ready or self.allow_synthetic_draft:
                raise ValueError('DATA_NOT_READY')
            if not self.max_token or not self.max_webhook_secret or not self.readiness_secret or not self.webhook_url:
                raise ValueError('pilot secrets required')
        return self


def _read_secret(path: str) -> str:
    return Path(path).read_text(encoding='utf-8').strip()


def load_settings(env: Mapping[str,str] | None = None, secret_reader: Callable[[str],str] | None = None) -> Result[Settings]:
    """Read only explicit named secret files; expected startup failures are sanitized."""
    env=os.environ if env is None else env
    reader=secret_reader or _read_secret
    secret_names={
        'encryption_key':('TSR_ENCRYPTION_KEY','TSR_ENCRYPTION_KEY_FILE'),
        'identity_hmac_key':('TSR_IDENTITY_HMAC_KEY','TSR_IDENTITY_HMAC_KEY_FILE'),
        'max_token':('TSR_MAX_BOT_TOKEN','TSR_MAX_BOT_TOKEN_FILE'),
        'max_webhook_secret':('TSR_WEBHOOK_SECRET','TSR_WEBHOOK_SECRET_FILE'),
        'readiness_secret':('TSR_HEALTH_TOKEN','TSR_HEALTH_TOKEN_FILE'),
    }
    data={}
    try:
        for name,(value_name,file_name) in secret_names.items():
            if value_name in env and file_name in env: return Result.failure(ErrorCode.VALIDATION_ERROR,safe_message_key='startup.configuration_invalid')
            value=reader(env[file_name]) if file_name in env else env.get(value_name)
            if value is not None: data[name]=SecretStr(value)
        names={
            'database_url':'TSR_DATABASE_URL','mode':'TSR_MODE','allow_synthetic_draft':'TSR_ALLOW_SYNTHETIC_DRAFT',
            'max_api_url':'TSR_MAX_API_URL','max_ca_bundle':'TSR_MAX_CA_BUNDLE','webhook_url':'TSR_WEBHOOK_URL','bot_scope':'TSR_BOT_SCOPE',
            'private_root':'TSR_PRIVATE_ROOT','release_root':'TSR_RELEASE_ROOT','release_manifest':'TSR_RELEASE_MANIFEST','release_commit':'TSR_RELEASE_COMMIT',
            'backup_root':'TSR_BACKUP_ROOT','deletion_journal_root':'TSR_DELETION_JOURNAL_ROOT',
            'reviewed_data_ready':'TSR_REVIEWED_DATA_READY','privacy_ready':'TSR_PRIVACY_READY',
        }
        for name in ('candidate_ttl_seconds','preview_ttl_seconds','handle_ttl_seconds','artifact_ttl_seconds','max_body_bytes',
                     'max_input_chars','max_file_bytes','lease_seconds','heartbeat_seconds','max_attempts','render_slots','render_timeout_seconds','network_timeout_seconds',
                     'max_backup_bytes','maintenance_interval_seconds','worker_heartbeat_stale_seconds','subscription_health_ttl_seconds',
                     'retention_case_days','retention_artifact_days','retention_inbox_hours','retention_dedupe_days','retention_backup_days',
                     'orphan_grace_seconds','retention_batch_size',
                     'db_connect_timeout_seconds','db_statement_timeout_ms','db_lock_timeout_ms','db_idle_transaction_timeout_ms','db_backup_snapshot_timeout_ms'):
            names[name]='TSR_'+name.upper()
        for name,key in names.items():
            if key in env: data[name]=env[key]
        if data.get('mode')=='pilot' and not all(str(data.get(x,'false')).lower() in {'true','1'} for x in ('reviewed_data_ready','privacy_ready')):
            return Result.failure(ErrorCode.DATA_NOT_READY,safe_message_key='startup.pilot_not_ready')
        return Result.success(Settings.model_validate(data))
    except ValidationError as exc:
        code=ErrorCode.DATA_NOT_READY if any('DATA_NOT_READY' in str(e.get('msg','')) for e in exc.errors(include_input=False)) else ErrorCode.VALIDATION_ERROR
        return Result.failure(code,safe_message_key='startup.configuration_invalid')
    except (OSError,ValueError,TypeError):
        return Result.failure(ErrorCode.VALIDATION_ERROR,safe_message_key='startup.configuration_invalid')
