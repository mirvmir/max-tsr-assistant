"""Private ciphertext storage. Published metadata and permits are required to read."""
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from tsr.contracts import EncryptedBlob, Result, StagedArtifact


class PrivateFiles:
    def __init__(self, root: Path, crypto, max_output_bytes: int = 10_485_760):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.crypto = crypto
        self.max_output_bytes = max_output_bytes

    def _path(self, ref: str):
        # UUID-only names, no user-provided components or traversable subdirectories.
        if not re.fullmatch(r'[0-9a-f]{32}\.blob', ref):
            raise ValueError('Unavailable artifact')
        path = self.root / ref
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError('Unavailable artifact')
        return path

    def stage_encrypted(self, rendered, claim, manifest_ref):
        now = datetime.now(timezone.utc)
        if (rendered.job_id != claim.job_id or rendered.fence_token != claim.fence_token
                or claim.kind != 'render_artifact' or claim.lease_until <= now):
            return Result.failure('LEASE_LOST')
        if (rendered.format != manifest_ref.artifact_spec.format
                or len(rendered.bytes) > self.max_output_bytes
                or sha256(rendered.bytes).hexdigest() != rendered.plaintext_sha256):
            return Result.failure('VALIDATION_ERROR')
        try:
            blob = self.crypto.encrypt(rendered.bytes)
            envelope = {'version': 1, 'key_id': blob.key_id,
                        **{key: base64.b64encode(getattr(blob, key)).decode('ascii')
                           for key in ('nonce', 'ciphertext', 'tag')}}
            payload = json.dumps(envelope, separators=(',', ':')).encode('ascii')
            artifact_id = uuid4()
            ref = f'{artifact_id.hex}.blob'
            path = self._path(ref)
            temporary = self.root / f'{artifact_id.hex}.tmp'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
            return Result.success(StagedArtifact(
                artifact_id=artifact_id, job_id=rendered.job_id, fence_token=rendered.fence_token,
                manifest_id=manifest_ref.manifest_id,
                document_kind=manifest_ref.artifact_spec.document_kind, format=rendered.format,
                plaintext_sha256=rendered.plaintext_sha256, encrypted_blob_ref=ref,
                size_bytes=len(rendered.bytes), created_at=now,
            ))
        except (OSError, ValueError):
            return Result.failure('TEMPORARY_FAILURE', safe_message_key='file_stage_failed', retryability='safe')

    def read_authorized_artifact(self, permit, record):
        now = datetime.now(timezone.utc)
        guard = permit.current_case_guard
        if (permit.owner_id != record.owner_id or permit.case_id != record.case_id
                or permit.artifact_id != record.artifact_id
                or guard.case_id != record.case_id
                or guard.expected_deletion_epoch != record.deletion_epoch
                or record.publication_status != 'published'
                or permit.artifact_original_revision != record.case_revision):
            return Result.failure('ACCESS_DENIED')
        if permit.expires_at <= now or record.expires_at <= now:
            return Result.failure('ARTIFACT_EXPIRED')
        if permit.disposition == 'historical':
            if not permit.warning_acknowledged:
                return Result.failure('ACCESS_DENIED')
        elif guard.expected_revision != record.case_revision:
            return Result.failure('STALE_REVISION')
        if record.size_bytes > self.max_output_bytes or record.size_bytes < 0:
            return Result.failure('ACCESS_DENIED')
        try:
            path = self._path(record.encrypted_blob_ref)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                if os.fstat(stream.fileno()).st_size > self.max_output_bytes * 2 + 4096:
                    raise ValueError('Unavailable artifact')
                envelope = json.loads(stream.read(self.max_output_bytes * 2 + 4097))
            if envelope.get('version') != 1:
                raise ValueError('Unavailable artifact')
            blob = EncryptedBlob(key_id=envelope['key_id'], **{
                key: base64.b64decode(envelope[key], validate=True)
                for key in ('nonce', 'ciphertext', 'tag')})
            data = self.crypto.decrypt(blob)
            if len(data) != record.size_bytes or sha256(data).hexdigest() != record.plaintext_sha256:
                raise ValueError('Unavailable artifact')
            return Result.success(data)
        except (OSError, ValueError, KeyError, TypeError):
            return Result.failure('NOT_FOUND', safe_message_key='material_unavailable')

    def delete_blob(self, ref: str) -> Result[bool]:
        """Confirm durable absence; already absent succeeds, IO errors stay failures."""
        try:
            path = self._path(ref)
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                removed = False
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return Result.success(removed)
        except ValueError:
            return Result.failure('VALIDATION_ERROR', safe_message_key='file_delete_failed')
        except OSError:
            return Result.failure('TEMPORARY_FAILURE', safe_message_key='file_delete_failed', retryability='safe')

    def remove_blob(self, ref: str) -> bool:
        """Legacy best-effort removal; cleanup callers use delete_blob for confirmation."""
        result = self.delete_blob(ref)
        return result.ok and result.value

    def purge_orphans(self, referenced_refs, older_than: datetime) -> int:
        """Caller supplies all published AND live staged refs and a lease-safe cutoff."""
        keep = frozenset(referenced_refs)
        removed = 0
        for path in (*self.root.glob('*.blob'), *self.root.glob('*.tmp')):
            try:
                if (path.name in keep or path.is_symlink()
                        or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= older_than):
                    continue
                if path.suffix == '.blob':
                    removed += self.remove_blob(path.name)
                elif re.fullmatch(r'[0-9a-f]{32}\.tmp', path.name):
                    # Interrupted atomic writes contain ciphertext too. Respect
                    # the same lease-safe cutoff and live staging references.
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
