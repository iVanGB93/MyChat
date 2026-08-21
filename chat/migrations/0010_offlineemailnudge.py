# Generated manually because the local Python runtime is unavailable.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0009_mediablob_document_type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OfflineEmailNudge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last_sent_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="offline_email_nudges_received", to=settings.AUTH_USER_MODEL)),
                ("room", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="offline_email_nudges", to="chat.chatroom")),
                ("sender", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="offline_email_nudges_sent", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="offlineemailnudge",
            constraint=models.UniqueConstraint(fields=("room", "sender", "recipient"), name="uniq_offline_email_nudge_conversation"),
        ),
        migrations.AddIndex(
            model_name="offlineemailnudge",
            index=models.Index(fields=["recipient", "last_sent_at"], name="chat_nudge_recipient_idx"),
        ),
    ]
