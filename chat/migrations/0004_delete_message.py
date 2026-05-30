from django.db import migrations


class Migration(migrations.Migration):
    """Drop the legacy Message table.

    Messages are no longer persisted server-side; they are relayed in real
    time over the chat WebSocket and stored client-side only. This migration
    removes the orphaned table along with any remaining data.
    """

    dependencies = [
        ("chat", "0003_add_pending_delivery"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Message",
        ),
    ]
