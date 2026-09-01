from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0014_mediablob_object_storage"),
    ]

    operations = [
        migrations.AddField(
            model_name="mediablob",
            name="multipart_part_size",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="mediablob",
            name="multipart_upload_id",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
    ]

