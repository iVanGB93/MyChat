from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0005_messagedelivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagedelivery",
            name="routed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="messagedelivery",
            name="routed_via",
            field=models.CharField(
                choices=[
                    ("unknown", "Unknown"),
                    ("chat_ws", "Chat WS"),
                    ("notif_ws", "Notification WS"),
                    ("push", "Push"),
                    ("pending_only", "Pending Only"),
                ],
                default="unknown",
                max_length=24,
            ),
        ),
    ]
