from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from .models import SignInChallenge


@shared_task
def cleanup_signin_challenges():
    # Preserve hourly abuse counters, but do not retain unused email addresses.
    count, _ = SignInChallenge.objects.filter(
        last_sent_at__lt=timezone.now() - timedelta(days=2),
        expires_at__lt=timezone.now(),
    ).delete()
    return {"deleted": count}
