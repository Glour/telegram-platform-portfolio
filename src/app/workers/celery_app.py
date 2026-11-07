from celery import Celery

from app.core.config import get_settings
from app.core.sentry import init_sentry

init_sentry()
settings = get_settings()

celery_app = Celery(
    "tg_ai",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.task_routes = {
    "app.workers.tasks.tick_campaign_account_replacements": {"queue": "campaigns.tick"},
    "app.workers.tasks.check_telegram_account": {"queue": "telegram.high"},
    "app.workers.tasks.refresh_telegram_account_statuses": {"queue": "telegram.high"},
    "app.workers.tasks.run_warmup_step": {"queue": "telegram.warmup"},
    "app.workers.tasks.tick_active_warmup_runs": {"queue": "telegram.warmup"},
    "app.workers.tasks.bulk_join_chats": {"queue": "telegram.actions"},
    "app.workers.tasks.bulk_ai_profiles": {"queue": "telegram.actions"},
    "app.workers.tasks.bulk_clone_profile": {"queue": "telegram.actions"},
    "app.workers.tasks.bulk_copy_stories": {"queue": "telegram.actions"},
    "app.workers.tasks.update_account_profile": {"queue": "telegram.actions"},
    "app.workers.tasks.run_comment_action": {"queue": "telegram.actions"},
    "app.workers.tasks.parse_source": {"queue": "telegram.parse"},
    "app.workers.tasks.discover_account_channels": {"queue": "telegram.parse"},
    "app.workers.tasks.generate_ai_comment": {"queue": "ai.generate"},
    "app.workers.tasks.tick_contact_broadcast": {"queue": "campaigns.tick"},
    "app.workers.tasks.tick_active_campaign_runs": {"queue": "campaigns.tick"},
    "app.workers.tasks.refresh_campaign_invite_tracking_metrics": {"queue": "campaigns.tick"},
    "app.workers.tasks.tick_due_content_factories": {"queue": "content_factory.tick"},
    "app.workers.tasks.run_operations_monitor": {"queue": "operations.monitor"},
    "app.workers.tasks.expire_due_workspace_access_periods": {"queue": "operations.monitor"},
    "app.workers.tasks.reconcile_pending_payment_orders": {"queue": "operations.monitor"},
}

celery_app.conf.beat_schedule = {
    "tick-campaign-account-replacements": {
        "task": "app.workers.tasks.tick_campaign_account_replacements",
        "schedule": 15.0,
        "kwargs": {"limit": 20},
    },
    "tick-active-campaign-runs": {
        "task": "app.workers.tasks.tick_active_campaign_runs",
        "schedule": 300.0,
        "kwargs": {"limit": 20},
    },
    "refresh-campaign-invite-tracking-metrics": {
        "task": "app.workers.tasks.refresh_campaign_invite_tracking_metrics",
        "schedule": 600.0,
        "kwargs": {"limit": 20},
    },
    "tick-due-content-factories": {
        "task": "app.workers.tasks.tick_due_content_factories",
        "schedule": 300.0,
        "kwargs": {"limit": 10},
    },
    "tick-active-warmup-runs": {
        "task": "app.workers.tasks.tick_active_warmup_runs",
        "schedule": 300.0,
        "kwargs": {"limit": 20},
    },
    "run-operations-monitor": {
        "task": "app.workers.tasks.run_operations_monitor",
        "schedule": 300.0,
    },
    "expire-due-workspace-access-periods": {
        "task": "app.workers.tasks.expire_due_workspace_access_periods",
        "schedule": 300.0,
        "kwargs": {"limit": 500},
    },
    "reconcile-pending-payment-orders": {
        "task": "app.workers.tasks.reconcile_pending_payment_orders",
        "schedule": 900.0,
        "kwargs": {"limit": 100},
    },
}
