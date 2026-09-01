from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0015_mediablob_multipart_upload"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagedelivery",
            name="last_push_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="messagedelivery",
            name="push_attempt_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
