from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("chat", "0016_messagedelivery_push_attempts")]
    operations = [migrations.AddField(
        model_name="messagedelivery", name="sender_confirmed_at",
        field=models.DateTimeField(blank=True, null=True, db_index=True),
    )]
