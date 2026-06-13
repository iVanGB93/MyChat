from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0011_remove_user_expo_push_token"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="userdevice",
            new_name="users_userd_user_id_a5f926_idx",
            old_name="users_userd_user_id_4eed50_idx",
        ),
        migrations.RenameIndex(
            model_name="userdevice",
            new_name="users_userd_expo_pu_a58d3d_idx",
            old_name="users_userd_expo_p_4ba856_idx",
        ),
    ]