from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("users", "0015_offline_email_notification_preference"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserPresenceSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("connection_id", models.CharField(max_length=255, unique=True)),
                ("app_state", models.CharField(choices=[("unknown", "Unknown"), ("active", "Active"), ("background", "Background")], default="unknown", max_length=16)),
                ("connected_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="presence_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-last_seen"],
                "indexes": [models.Index(fields=["user", "app_state", "last_seen"], name="users_prs_user_state_idx")],
            },
        ),
    ]
