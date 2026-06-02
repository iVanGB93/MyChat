from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0004_add_blocked_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="notif_messages_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Receive push notifications for new messages",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="notif_calls_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Receive push notifications for incoming calls",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="notif_sound_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Play in-app sound for new messages / calls",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="token_version",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
