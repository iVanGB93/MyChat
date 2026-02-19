from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="expo_push_token",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Expo push notification token for this device",
                max_length=200,
            ),
        ),
    ]
