from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0013_chatroom_avatar"),
    ]

    operations = [
        migrations.AlterField(
            model_name="mediablob",
            name="data",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="mediablob",
            name="storage_backend",
            field=models.CharField(default="database", max_length=16),
        ),
        migrations.AddField(
            model_name="mediablob",
            name="object_key",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="mediablob",
            name="upload_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
