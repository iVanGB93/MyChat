"""Add display_name, user_tag (unique), and discoverability preferences.

The migration runs in three steps so existing rows get a backfilled
`user_tag` before the UNIQUE constraint is applied:

  1. Add `display_name` and the two discoverability bool flags.
  2. Add `user_tag` as a NULLable, non-unique column.
  3. Backfill `user_tag` for every existing row with a unique value.
  4. Tighten `user_tag` to UNIQUE + indexed.
"""

import secrets

from django.db import migrations, models


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # excludes 0/O/1/I


def _new_tag() -> str:
    return "AXN-" + "".join(secrets.choice(_ALPHABET) for _ in range(4))


def backfill_user_tags(apps, schema_editor):
    User = apps.get_model("users", "User")
    existing = set(
        User.objects.exclude(user_tag__isnull=True)
        .exclude(user_tag="")
        .values_list("user_tag", flat=True)
    )
    for user in User.objects.filter(models.Q(user_tag__isnull=True) | models.Q(user_tag="")):
        for _ in range(20):
            tag = _new_tag()
            if tag in existing:
                continue
            user.user_tag = tag
            existing.add(tag)
            break
        user.save(update_fields=["user_tag"])


def noop_reverse(apps, schema_editor):
    # No-op: dropping the column on reverse already discards the data.
    return


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0006_fileblob_alter_blockeduser_id_alter_user_avatar"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="display_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Free-form display name shown in the UI. Defaults to username.",
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="discoverable_by_username",
            field=models.BooleanField(
                default=True,
                help_text="Allow other users to find this account by username search.",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="discoverable_by_email",
            field=models.BooleanField(
                default=False,
                help_text="Allow other users to find this account by exact email match.",
            ),
        ),
        # Step 1: add user_tag as nullable / non-unique so backfill can run.
        migrations.AddField(
            model_name="user",
            name="user_tag",
            field=models.CharField(
                blank=True,
                help_text="Short shareable handle, e.g. 'AXN-7K3P'.",
                max_length=16,
                null=True,
            ),
        ),
        migrations.RunPython(backfill_user_tags, noop_reverse),
        # Step 2: tighten constraints now that every row has a value.
        migrations.AlterField(
            model_name="user",
            name="user_tag",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Short shareable handle, e.g. 'AXN-7K3P'.",
                max_length=16,
                null=True,
                unique=True,
            ),
        ),
    ]
