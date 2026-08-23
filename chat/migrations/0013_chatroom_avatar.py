# Generated manually to keep the migration graph deterministic.

from django.db import migrations, models

import users.db_storage


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0012_rename_chat_groupm_room_id_3ee1c8_idx_chat_groupm_room_id_d6bb7c_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatroom",
            name="avatar",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=users.db_storage.DatabaseStorage(),
                upload_to="group-avatars/",
            ),
        ),
    ]
