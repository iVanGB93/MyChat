web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && daphne -b 0.0.0.0 -p $PORT config.asgi:application
worker: celery -A config worker --loglevel=INFO --concurrency=2
beat: celery -A config beat --loglevel=INFO --pidfile= --schedule=/tmp/celerybeat-schedule
