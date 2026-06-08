"""
Django settings for config project — ChatConnect.
Real-time chat, voice call, and video call application.
"""

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY")

DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = ['*']

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# Railway runs behind a proxy
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "daphne",  # ASGI server — must be before django apps
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "channels",
    # Local apps
    "users",
    "chat",
    "calls",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# Database — PostgreSQL (falls back to SQLite for quick local dev)
# ---------------------------------------------------------------------------

if os.getenv("DATABASE_URL"):
    # Railway / production: parse DATABASE_URL automatically
    DATABASES = {
        "default": dj_database_url.config(
            default=os.getenv("DATABASE_URL"),
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
elif os.getenv("DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "chatconnect"),
            "USER": os.getenv("DB_USER", "postgres"),
            "PASSWORD": os.getenv("DB_PASSWORD", "postgres"),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "users.auth.AxonicJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    # Named throttles consumed by individual views (e.g. UserSearchView).
    # Defaults are intentionally conservative; tune per-environment via env
    # if abuse is observed.
    "DEFAULT_THROTTLE_CLASSES": (),
    "DEFAULT_THROTTLE_RATES": {
        "user_search": "30/min",
        "register_request": "5/hour",
        "register_resend": "5/hour",
        "register_verify": "10/hour",
    },
}


# ---------------------------------------------------------------------------
# Simple JWT
# ---------------------------------------------------------------------------

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}


# ---------------------------------------------------------------------------
# Django Channels — Redis channel layer
# ---------------------------------------------------------------------------

# Prefer Railway's private Redis URL when available (no proxy, lower latency,
# fewer dropped connections). Falls back to the public REDIS_URL otherwise.
_REDIS_URL = (
    os.getenv("REDIS_PRIVATE_URL")
    or os.getenv("REDISCLOUD_URL")
    or os.getenv("REDIS_URL")
)

if _REDIS_URL:
    # Use the PubSub backend instead of the default RedisChannelLayer:
    #   - No BRPOP polling loop, so transient TCP read timeouts don't kill the
    #     consumer with "Exception inside application: Timeout reading from ...".
    #   - No DB-1 state to expire / leak.
    #   - Recommended by django-channels for new deployments.
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.pubsub.RedisPubSubChannelLayer",
            "CONFIG": {
                "hosts": [{
                    "address": _REDIS_URL,
                    # Keep the TCP socket alive across long idle periods so the
                    # Railway proxy doesn't silently reap it.
                    "socket_keepalive": True,
                    "socket_keepalive_options": {},
                    "retry_on_timeout": True,
                    "health_check_interval": 30,
                }],
            },
        },
    }
else:
    # In-memory channel layer for local dev without Redis
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        },
    }


# ---------------------------------------------------------------------------
# CORS — allow React / React Native dev servers
# ---------------------------------------------------------------------------

CORS_ALLOW_ALL_ORIGINS = DEBUG  # restrict in production
CORS_ALLOW_CREDENTIALS = True

if not DEBUG:
    CORS_ALLOWED_ORIGINS = [
        origin.strip()
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]


# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
MESSAGE_ACK_TIMEOUT_SECONDS = int(os.getenv("MESSAGE_ACK_TIMEOUT_SECONDS", "8"))
MESSAGE_DELIVERY_SWEEP_INTERVAL_SECONDS = int(
    os.getenv("MESSAGE_DELIVERY_SWEEP_INTERVAL_SECONDS", "15")
)
CALL_INVITE_ACK_TIMEOUT_SECONDS = int(os.getenv("CALL_INVITE_ACK_TIMEOUT_SECONDS", "12"))
CALL_INVITE_RETRY_INTERVAL_SECONDS = int(os.getenv("CALL_INVITE_RETRY_INTERVAL_SECONDS", "20"))
CELERY_BEAT_SCHEDULE = {
    "sweep-stale-message-deliveries": {
        "task": "chat.tasks.sweep_stale_message_deliveries",
        "schedule": MESSAGE_DELIVERY_SWEEP_INTERVAL_SECONDS,
    },
    "sweep-stale-call-invites": {
        "task": "calls.tasks.sweep_stale_call_invites",
        "schedule": CALL_INVITE_RETRY_INTERVAL_SECONDS,
    },
}

# Reliability monitor thresholds (ops-tunable via environment)
MONITOR_MSG_ACK_RATE_HEALTHY = float(os.getenv("MONITOR_MSG_ACK_RATE_HEALTHY", "0.99"))
MONITOR_MSG_ACK_RATE_DEGRADED = float(os.getenv("MONITOR_MSG_ACK_RATE_DEGRADED", "0.95"))
MONITOR_CALL_ACK_RATE_HEALTHY = float(os.getenv("MONITOR_CALL_ACK_RATE_HEALTHY", "0.98"))
MONITOR_CALL_ACK_RATE_DEGRADED = float(os.getenv("MONITOR_CALL_ACK_RATE_DEGRADED", "0.90"))
MONITOR_MSG_PENDING_OVERDUE_WARN = int(os.getenv("MONITOR_MSG_PENDING_OVERDUE_WARN", "1"))
MONITOR_MSG_PENDING_OVERDUE_CRIT = int(os.getenv("MONITOR_MSG_PENDING_OVERDUE_CRIT", "10"))
MONITOR_CALL_PENDING_OVERDUE_WARN = int(os.getenv("MONITOR_CALL_PENDING_OVERDUE_WARN", "1"))
MONITOR_CALL_PENDING_OVERDUE_CRIT = int(os.getenv("MONITOR_CALL_PENDING_OVERDUE_CRIT", "5"))
MONITOR_MSG_PUSH_FALLBACK_WARN = int(os.getenv("MONITOR_MSG_PUSH_FALLBACK_WARN", "1"))
MONITOR_MSG_PUSH_FALLBACK_CRIT = int(os.getenv("MONITOR_MSG_PUSH_FALLBACK_CRIT", "20"))
MONITOR_MSG_ACK_AVG_MS_HEALTHY = int(os.getenv("MONITOR_MSG_ACK_AVG_MS_HEALTHY", "1200"))
MONITOR_MSG_ACK_AVG_MS_DEGRADED = int(os.getenv("MONITOR_MSG_ACK_AVG_MS_DEGRADED", "3500"))


# ---------------------------------------------------------------------------
# Email (SMTP) — used by the email-verification registration flow
# ---------------------------------------------------------------------------
# In production set EMAIL_HOST / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD via env.
# In dev (DEBUG=True) without EMAIL_HOST_USER configured we fall back to
# the console backend so verification codes are printed to the Django log.
#
# GoDaddy / Microsoft 365 SMTP defaults:
#   M365 (recommended — GoDaddy now provisions mailboxes on M365):
#     EMAIL_HOST=smtp.office365.com  EMAIL_PORT=587
#     EMAIL_USE_SSL=False            EMAIL_USE_TLS=True
#   Legacy GoDaddy Workspace Email:
#     EMAIL_HOST=smtpout.secureserver.net  EMAIL_PORT=465
#     EMAIL_USE_SSL=True                   EMAIL_USE_TLS=False
# ---------------------------------------------------------------------------

EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.office365.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() in ("true", "1", "yes")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in ("true", "1", "yes")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "15"))
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "noreply@axonic.app")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# SendGrid Web API \u2014 used on hosts that block outbound SMTP (e.g. Railway).
# When SENDGRID_API_KEY is set, _send_verification_email uses the HTTPS API
# instead of SMTP. Local dev without an API key still uses SMTP/console.
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")

# Resend Web API \u2014 same use case as SendGrid; takes precedence when set.
# Sign up at https://resend.com, verify your domain or use the shared
# onboarding@resend.dev sender.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    # No credentials configured — print emails to the log instead of failing.
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


# ---------------------------------------------------------------------------
# LiveKit
# ---------------------------------------------------------------------------

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")


# ---------------------------------------------------------------------------
# TURN / STUN — ICE server configuration for WebRTC P2P calls
#
# Set TURN_URLS to a comma-separated list of TURN URLs, e.g.:
#   TURN_URLS=turn:your-server.com:3478,turns:your-server.com:5349
#
# Credentials — two modes:
#   1. Static (simple):  set TURN_USERNAME + TURN_CREDENTIAL
#   2. HMAC time-limited (recommended for Coturn):  set TURN_SECRET only
#      Coturn must have `use-auth-secret` and `static-auth-secret=<TURN_SECRET>`
# ---------------------------------------------------------------------------

TURN_URLS = [u.strip() for u in os.getenv("TURN_URLS", "").split(",") if u.strip()]
TURN_SECRET = os.getenv("TURN_SECRET", "")        # HMAC secret (Coturn REST API)
TURN_USERNAME = os.getenv("TURN_USERNAME", "")    # static username (alternative)
TURN_CREDENTIAL = os.getenv("TURN_CREDENTIAL", "") # static credential (alternative)
TURN_TTL = int(os.getenv("TURN_TTL", "86400"))    # credential lifetime in seconds


# ---------------------------------------------------------------------------
# Internationalization / Static / Media
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# Default primary key field type
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
