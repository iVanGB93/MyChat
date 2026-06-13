from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0010_user_device"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="user",
            name="expo_push_token",
        ),
    ]