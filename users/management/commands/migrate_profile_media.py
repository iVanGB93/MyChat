"""Copy referenced DB avatars to Spaces without deleting the original blobs."""
from django.core.management.base import BaseCommand, CommandError
from django.core.files.base import ContentFile
from django.db import transaction
from django.conf import settings
from users.db_storage import FileBlob, save_profile_object, is_profile_object
from users.models import User, UserProfile
from chat.models import ChatRoom


class Command(BaseCommand):
    help = "Preview avatar migration; --apply copies to Spaces and switches references. Original blobs are retained."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        models = (User, UserProfile, ChatRoom)
        names = {name for model in models for name in model.objects.exclude(avatar="").exclude(avatar=None).values_list("avatar", flat=True)
                 if not is_profile_object(name)}
        self.stdout.write(f"Referenced legacy avatars: {len(names)}")
        if not options["apply"]:
            self.stdout.write("Preview only. No objects or database records changed.")
            return
        if settings.PROFILE_MEDIA_STORAGE_BACKEND != "spaces":
            raise CommandError("Enable PROFILE_MEDIA_STORAGE_BACKEND=spaces before applying.")
        copied = 0
        for name in sorted(names):
            blob = FileBlob.objects.filter(name=name).first()
            if blob is None:
                self.stderr.write(f"Missing legacy blob: {name}")
                continue
            content = ContentFile(bytes(blob.data), name=name)
            content.content_type = blob.content_type
            key = save_profile_object(name, content)
            with transaction.atomic():
                for model in models:
                    model.objects.filter(avatar=name).update(avatar=key)
            copied += 1
        self.stdout.write(self.style.SUCCESS(f"Migrated {copied} avatars. Original FileBlob rows retained for recovery."))
