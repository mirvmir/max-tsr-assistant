"""Read-only operational readiness. Diagnostic codes never contain user data."""
from datetime import datetime, timezone
from tsr.contracts import HealthSnapshot
from .ingress import secret_value


def collect_readiness(settings, db, application=None, now=None) -> HealthSnapshot:
    now = now or datetime.now(timezone.utc)
    reasons = []
    try:
        release_ref = application.release.release_ref if application is not None else None
        health = db.collect_health(now, settings.bot_scope, release_ref=release_ref, mode=settings.mode,
                                   worker_heartbeat_seconds=settings.worker_heartbeat_stale_seconds)
    except Exception:
        return HealthSnapshot(status='degraded', release_commit=settings.release_commit, db_ready=False,
                              reasons=('database_unavailable',))
    if not health.db_ready:
        reasons.append('database_unavailable')
    age = health.worker_heartbeat_age
    if not health.worker_count or age is None or age < 0 or age > settings.worker_heartbeat_stale_seconds:
        reasons.append('worker_unavailable')
    if health.worker_count and health.release_commit != settings.release_commit:
        reasons.append('worker_release_mismatch')
    if not health.release_ready:
        reasons.append('active_release_invalid')
    elif health.active_release_ref is not None:
        try:
            from tsr.contracts import check_release_policy
            with db.uow() as unit:
                record=unit.releases.get(health.active_release_ref)
            if record is None or not check_release_policy(record,settings.mode,now,allow_synthetic_draft=settings.allow_synthetic_draft).ok:
                reasons.append('active_release_policy_invalid')
        except Exception:
            reasons.append('active_release_policy_invalid')
    try:
        if not settings.encryption_key_bytes or not secret_value(settings.identity_hmac_key):
            reasons.append('critical_secrets_missing')
    except Exception:
        reasons.append('critical_secrets_missing')
    if not all(secret_value(getattr(settings, key, None)) for key in ('readiness_secret','max_webhook_secret')):
        reasons.append('critical_secrets_missing')
    configured = bool(secret_value(settings.max_token))
    if not configured:
        reasons.append('max_unconfigured')
    elif not settings.webhook_url or not settings.webhook_url.startswith('https://'):
        reasons.append('webhook_unconfigured')
    else:
        try:
            subscription = db.read_subscription_health(settings.bot_scope)
            age = (now - subscription.checked_at).total_seconds() if subscription else None
            if subscription is None or not subscription.healthy or age is None or age < 0 or age > settings.subscription_health_ttl_seconds:
                reasons.append('subscription_unavailable')
        except Exception:
            reasons.append('subscription_unavailable')
    reasons.extend(health.reasons)
    return health.model_copy(update={'status':'degraded' if reasons else 'ready',
                                   'reasons':tuple(dict.fromkeys(reasons))})
