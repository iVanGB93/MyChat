"""Daily backups, deliberately enabled only after a successful restore drill."""
import logging
from celery import shared_task
from django.core.management import call_command
from django.conf import settings
from io import StringIO

logger = logging.getLogger(__name__)


@shared_task(soft_time_limit=2100, time_limit=2160, ignore_result=True)
def backup_database():
    if not settings.DATABASE_BACKUPS_ENABLED:
        return "disabled"
    output = StringIO()
    try:
        call_command("backup_database", stdout=output)
    except Exception:
        # Do not log exception details containing URLs or storage credentials.
        logger.error("[DatabaseBackup] FAILED: inspect backup prerequisites and last successful archive")
        raise RuntimeError("Database backup failed") from None
    logger.info("[DatabaseBackup] SUCCESS %s", output.getvalue().strip())
