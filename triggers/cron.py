from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from core.config import settings
from workflows.backup_workflow import get_backup_config, run_backup_workflow
from workflows.notification_workflow import (
    process_due_reminders_workflow,
    run_scheduled_digests_workflow,
)
from workflows.webhook_workflow import process_pending_webhook_retries

scheduler = AsyncIOScheduler(timezone=settings.TZ)


async def _job_minute_tick() -> None:
    try:
        process_due_reminders_workflow()
        run_scheduled_digests_workflow()
        await process_pending_webhook_retries()
    except Exception as exc:
        logger.warning(f"Cron minute tick error: {exc}")


async def _job_auto_backup() -> None:
    try:
        cfg = get_backup_config()
        if cfg.get("auto_backup_enabled", True):
            run_backup_workflow(notes="Backup automático agendado")
    except Exception as exc:
        logger.warning(f"Auto backup job error: {exc}")


def start_scheduler() -> None:
    if scheduler.running:
        return
    # Run every minute to check due reminders, user digest times (08:00, 13:00, 18:00 or custom), and webhook retries
    scheduler.add_job(
        _job_minute_tick,
        CronTrigger(minute="*", timezone=settings.TZ),
        id="minute_tick",
        replace_existing=True,
    )
    # Daily automatic backup at 03:00 America/Sao_Paulo
    scheduler.add_job(
        _job_auto_backup,
        CronTrigger(hour=3, minute=0, timezone=settings.TZ),
        id="daily_backup",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"APScheduler started in timezone {settings.TZ_NAME}.")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
