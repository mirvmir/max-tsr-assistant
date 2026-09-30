"""Bounded retention phases; physical deletion happens outside SQL transactions."""
from datetime import timedelta
from tsr.contracts import DeleteReport,RetentionPolicy


def run_retention(now,policy:RetentionPolicy,*,db,files,bot_scope=None)->DeleteReport:
    policy=RetentionPolicy.model_validate(policy)
    targets=db.prepare_retention(now,policy,bot_scope)
    removed=0;errors=[]
    # Expired records have already revoked access but retain their private refs
    # until storage explicitly confirms absence. Failed IO stays retryable.
    for artifact in targets.artifacts:
        outcome=files.delete_blob(artifact.encrypted_blob_ref)
        if not outcome.ok:
            errors.append('retention.file_unavailable')
            continue
        if db.complete_artifact_cleanup(artifact.artifact_id,artifact.encrypted_blob_ref,now):
            removed+=int(outcome.value)
        else:
            errors.append('retention.claim_changed')
    for request in targets.cleanup:
        with db.uow() as unit:
            artifacts=unit.artifacts.list_by_case(request.case_id)
        failed=False
        for artifact in artifacts:
            outcome=files.delete_blob(artifact.encrypted_blob_ref)
            if not outcome.ok:
                failed=True
            else:
                removed+=int(outcome.value)
        if failed:
            errors.append('retention.file_unavailable')
            continue
        if not db.complete_case_cleanup(request.case_id,request.deletion_epoch):
            errors.append('retention.epoch_changed')
    # Read global refs/claims again immediately before janitor IO. Shared private
    # storage must keep other scopes' files and every live renderer's staged data.
    protected=db.active_claims(now)
    refs=db.referenced_blob_refs()
    removed+=files.purge_orphans(refs,now-timedelta(seconds=policy.orphan_grace_seconds),protected_claims=protected)
    remaining=len(db.list_cleanup_requests(bot_scope))
    return DeleteReport(removed_count=removed,remaining_count=remaining+len(errors),errors=tuple(sorted(set(errors))))
