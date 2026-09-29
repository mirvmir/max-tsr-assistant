"""Idempotent deletion maintenance via infrastructure ports."""
from tsr.contracts import DeleteReport
from tsr.ports import FileStore


def cleanup_deleted_cases(db, files: FileStore):
    removed = 0
    errors = []
    requests = db.list_cleanup_requests()
    for request in requests:
        with db.uow() as uow:
            artifacts = uow.artifacts.list_by_case(request.case_id)
        failed = False
        for artifact in artifacts:
            # Only an explicit confirmed absence permits erasing the private
            # metadata reference. An IO failure remains pending for a retry.
            deletion = files.delete_blob(artifact.encrypted_blob_ref)
            if not deletion.ok:
                failed = True
            else:
                removed += int(deletion.value)
        if failed:
            errors.append("cleanup.file_unavailable")
            continue
        try:
            completed = db.complete_case_cleanup(request.case_id, request.deletion_epoch)
        except Exception:
            errors.append('cleanup.metadata_unavailable')
            continue
        if not completed:
            errors.append("cleanup.epoch_changed")
    return DeleteReport(removed_count=removed, remaining_count=len(db.list_cleanup_requests()), errors=tuple(errors))
