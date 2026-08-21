# Generated manually because the local development environment has no Python runtime.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0010_offlineemailnudge"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="GroupMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("admin", "Admin"), ("member", "Member")], default="member", max_length=12)),
                ("joined_at", models.DateTimeField(auto_now_add=True)),
                ("added_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="group_members_added", to=settings.AUTH_USER_MODEL)),
                ("room", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="group_memberships", to="chat.chatroom")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="group_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["room", "role"], name="chat_groupm_room_id_3ee1c8_idx")],
                "constraints": [models.UniqueConstraint(fields=("room", "user"), name="uniq_group_membership_user_room")],
            },
        ),
    ]
