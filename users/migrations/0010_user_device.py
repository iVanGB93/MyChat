from django.db import migrations, models
from django.utils import timezone


def backfill_user_devices(apps, schema_editor):
    User = apps.get_model("users", "User")
    UserDevice = apps.get_model("users", "UserDevice")
    now = timezone.now()

    rows = []
    for user in User.objects.exclude(expo_push_token="").iterator():
        rows.append(
            UserDevice(
                user_id=user.id,
                installation_id=f"legacy-user-{user.id}",
                expo_push_token=user.expo_push_token,
                platform="unknown",
                device_name="",
                app_version="",
                is_active=True,
                last_seen=user.last_seen or now,
            )
        )
    UserDevice.objects.bulk_create(rows, ignore_conflicts=True)


def noop_reverse(apps, schema_editor):
    return


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0009_user_profile_presence"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("installation_id", models.CharField(db_index=True, max_length=128, unique=True)),
                ("expo_push_token", models.CharField(blank=True, default="", max_length=200)),
                ("platform", models.CharField(choices=[("android", "Android"), ("ios", "iOS"), ("web", "Web"), ("unknown", "Unknown")], default="unknown", max_length=16)),
                ("device_name", models.CharField(blank=True, default="", max_length=120)),
                ("app_version", models.CharField(blank=True, default="", max_length=32)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(default=timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="devices", to="users.user")),
            ],
            options={"ordering": ["-last_seen", "-updated_at"]},
        ),
        migrations.AddIndex(
            model_name="userdevice",
            index=models.Index(fields=["user", "is_active"], name="users_userd_user_id_4eed50_idx"),
        ),
        migrations.AddIndex(
            model_name="userdevice",
            index=models.Index(fields=["expo_push_token"], name="users_userd_expo_p_4ba856_idx"),
        ),
        migrations.RunPython(backfill_user_devices, noop_reverse),
    ]