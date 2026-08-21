# Generated manually because the local Python runtime is unavailable.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0014_passwordresetrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="notif_offline_email_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Email me about messages when no app delivery channel is available",
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="notif_offline_email_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
