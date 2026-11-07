from app.workers.celery_app import celery_app


def test_account_status_refresh_is_not_scheduled() -> None:
    assert "refresh-telegram-account-statuses" not in celery_app.conf.beat_schedule


def test_account_status_refresh_uses_high_priority_queue() -> None:
    route = celery_app.conf.task_routes["app.workers.tasks.refresh_telegram_account_statuses"]

    assert route["queue"] == "telegram.high"


def test_content_factory_tick_is_scheduled() -> None:
    schedule = celery_app.conf.beat_schedule["tick-due-content-factories"]

    assert schedule["task"] == "app.workers.tasks.tick_due_content_factories"
    assert schedule["schedule"] == 300.0
    assert schedule["kwargs"] == {"limit": 10}


def test_content_factory_tick_uses_content_factory_queue() -> None:
    route = celery_app.conf.task_routes["app.workers.tasks.tick_due_content_factories"]

    assert route["queue"] == "content_factory.tick"
