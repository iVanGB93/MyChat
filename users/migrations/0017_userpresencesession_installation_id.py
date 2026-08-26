from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0016_userpresencesession"),
    ]

    operations = [
        migrations.AddField(
            model_name="userpresencesession",
            name="installation_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
    ]
