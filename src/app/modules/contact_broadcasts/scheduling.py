from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.modules.campaigns.models import CampaignRun
from app.workers.celery_app import celery_app


async def queue_next_tick(session, run):
    # One durable wake-up token per run. Stale/duplicated Celery tasks cannot spawn extra chains.
    run = (
        await session.execute(
            select(CampaignRun)
            .where(CampaignRun.id == run.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if run.status not in {"pending", "running"}:
        return
    now = datetime.now(UTC)
    progress = dict(run.progress or {})
    existing = progress.get("tick_scheduled_for")
    if progress.get("tick_token") and existing and datetime.fromisoformat(existing) > now - timedelta(seconds=60):
        return
    target = datetime.fromisoformat(progress["next_send_at"]) if progress.get("next_send_at") else now
    countdown = min(300, max(1, (target - now).total_seconds()))
    token = str(uuid4())
    run.progress = {
        **progress,
        "tick_token": token,
        "tick_scheduled_for": (now + timedelta(seconds=countdown)).isoformat(),
    }
    await session.commit()
    celery_app.send_task(
        "app.workers.tasks.tick_contact_broadcast", kwargs={"run_id": run.id, "token": token}, countdown=countdown
    )
