from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("calls", "0003_calllog_invite_acked_at_calllog_push_sent_at_and_more")]
    operations = [
        migrations.AddField(model_name="calllog", name="video_quality", field=models.CharField(max_length=10, default="automatic")),
        migrations.AddField(model_name="calllog", name="quality_revision", field=models.PositiveIntegerField(default=0)),
    ]
