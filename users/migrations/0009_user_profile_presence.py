import users.db_storage
from django.db import migrations, models
from django.utils import timezone


def backfill_profile_and_presence(apps, schema_editor):
    User = apps.get_model("users", "User")
    UserProfile = apps.get_model("users", "UserProfile")
    UserPresence = apps.get_model("users", "UserPresence")

    profiles = []
    presences = []
    now = timezone.now()

    for user in User.objects.all().iterator():
        profiles.append(
            UserProfile(
                user_id=user.id,
                avatar=user.avatar,
                bio=user.bio,
                display_name=user.display_name,
                user_tag=user.user_tag,
                discoverable_by_username=user.discoverable_by_username,
                discoverable_by_email=user.discoverable_by_email,
                connectivity_mode=user.connectivity_mode,
                notif_messages_enabled=user.notif_messages_enabled,
                notif_calls_enabled=user.notif_calls_enabled,
                notif_sound_enabled=user.notif_sound_enabled,
            )
        )
        presences.append(
            UserPresence(
                user_id=user.id,
                is_online=user.is_online,
                last_seen=user.last_seen or now,
            )
        )

    UserProfile.objects.bulk_create(profiles, ignore_conflicts=True)
    UserPresence.objects.bulk_create(presences, ignore_conflicts=True)


def noop_reverse(apps, schema_editor):
    return


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0008_pendingregistration"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserPresence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_online", models.BooleanField(default=False)),
                ("last_seen", models.DateTimeField(default=timezone.now)),
                ("notification_socket_connected", models.BooleanField(default=False)),
                ("notification_socket_count", models.PositiveIntegerField(default=0)),
                ("chat_socket_connected", models.BooleanField(default=False)),
                ("chat_socket_count", models.PositiveIntegerField(default=0)),
                ("app_state", models.CharField(choices=[("unknown", "Unknown"), ("active", "Active"), ("background", "Background")], default="unknown", max_length=16)),
                ("active_room_id", models.CharField(blank=True, default="", max_length=64)),
                ("last_notification_seen_at", models.DateTimeField(blank=True, null=True)),
                ("last_chat_seen_at", models.DateTimeField(blank=True, null=True)),
                ("last_app_state_change_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=models.deletion.CASCADE, related_name="presence", to="users.user")),
            ],
            options={"ordering": ["-last_seen"]},
        ),
        migrations.CreateModel(
            name="UserProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("avatar", models.ImageField(blank=True, null=True, storage=users.db_storage.DatabaseStorage(), upload_to="avatars/")),
                ("bio", models.CharField(blank=True, default="", max_length=200)),
                ("display_name", models.CharField(blank=True, default="", max_length=50)),
                ("user_tag", models.CharField(blank=True, db_index=True, max_length=16, null=True, unique=True)),
                ("discoverable_by_username", models.BooleanField(default=True)),
                ("discoverable_by_email", models.BooleanField(default=False)),
                ("connectivity_mode", models.CharField(choices=[("auto", "Auto (P2P with relay fallback)"), ("p2p", "Always P2P"), ("server", "Always Server (relay)")], default="auto", max_length=10)),
                ("notif_messages_enabled", models.BooleanField(default=True)),
                ("notif_calls_enabled", models.BooleanField(default=True)),
                ("notif_sound_enabled", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=models.deletion.CASCADE, related_name="profile", to="users.user")),
            ],
            options={"ordering": ["user__username"]},
        ),
        migrations.RunPython(backfill_profile_and_presence, noop_reverse),
    ]