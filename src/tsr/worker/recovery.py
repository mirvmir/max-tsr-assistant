from __future__ import annotations

from datetime import datetime, timedelta

from tsr.contracts import DomainError, RetryDecision, TransportResult


def classify_retry(work_kind: str, outcome: TransportResult | DomainError, now: datetime,
                   attempt: int, max_attempts: int = 5, *, outbox_status: str | None = None) -> RetryDecision:
    if isinstance(outcome, TransportResult):
        if outcome.status == "unknown":
            return RetryDecision(mode="explicit_user", next_attempt_at=None, reason_code="delivery_unknown")
        safe = outcome.status == "definitely_rejected" and outcome.retryable
        retry_after = outcome.retry_after or 0
        reason = outcome.error_code or "delivery_finished"
    else:
        safe = outcome.retryability == "safe" and (
            work_kind != "deliver_outbox" or outbox_status in ("pending", "preparing", "retry_wait"))
        retry_after = 0
        reason = outcome.code.value if hasattr(outcome.code, "value") else str(outcome.code)
    if safe and attempt < max_attempts:
        delay = max(min(2 ** attempt, 60), retry_after)
        return RetryDecision(mode="safe", next_attempt_at=now + timedelta(seconds=delay), reason_code=reason)
    return RetryDecision(mode="none", next_attempt_at=None, reason_code=reason)
