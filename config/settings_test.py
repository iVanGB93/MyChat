"""Safe local settings used by Axonic's automated backend checks.

The normal development settings can load Railway service URLs from ``.env``.
Tests must never connect to those services, so this module replaces every
stateful integration with an isolated local implementation.
"""

from .settings import *  # noqa: F403


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
MEDIA_STORAGE_BACKEND = "database"
PROFILE_MEDIA_STORAGE_BACKEND = "database"

# Keep password-based tests fast while preserving Django's password behavior.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
