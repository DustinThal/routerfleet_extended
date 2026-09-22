from django.db import models
from django.db.models import Q
from router_manager.models import Router
import uuid
import hashlib
import os


class RouterBackup(models.Model):
    router = models.ForeignKey(Router, on_delete=models.CASCADE)
    success = models.BooleanField(default=False)
    error = models.BooleanField(default=False)
    backup_pending_retrieval = models.BooleanField(default=False)
    # Set by save(): whether this backup brought a different configuration than
    # the one before it. It is stored instead of computed on the fly so that a
    # list can filter on it.
    config_change_detected = models.BooleanField(default=False)
    error_message = models.TextField(blank=True, null=True)
    retry_count = models.IntegerField(default=0)
    next_retry = models.DateTimeField(blank=True, null=True)
    schedule_time = models.DateTimeField(blank=True, null=True)
    schedule_type = models.CharField(max_length=10, choices=(('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly'), ('instant', 'Instant')))
    queue_length = models.IntegerField(default=0) # Seconds
    finish_time = models.DateTimeField(blank=True, null=True)
    backup_text = models.TextField(blank=True, null=True)
    backup_text_hash = models.CharField(max_length=64, blank=True, db_index=True)
    backup_text_filename = models.CharField(max_length=255, blank=True, null=True)
    backup_binary = models.FileField(upload_to='backups/', blank=True, null=True)
    task_console_output = models.TextField(blank=True, null=True, default='')
    task_lock = models.DateTimeField(blank=True, null=True)

    updated = models.DateTimeField(auto_now=True)
    created = models.DateTimeField(auto_now_add=True)
    uuid = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)

    def previous_backup(self):
        """The backup this one is compared against.

        That is the backup of the same device that was written before it and that
        brought a configuration with it. A backup without one — it failed, or it
        is still waiting for its text — is looked past, so the comparison reaches
        the last configuration that was really seen.
        """
        others = RouterBackup.objects.filter(router_id=self.router_id).exclude(backup_text_hash='')
        if self.pk:
            others = others.exclude(pk=self.pk)
            if self.created:
                # Only what was written before this one: a backup that is saved a
                # second time (a retry that finished) must not be compared against
                # a backup that came after it
                others = others.filter(
                    Q(created__lt=self.created) | Q(created=self.created, id__lt=self.id))
        return others.order_by('-created', '-id').first()

    def config_change_check(self) -> bool:
        """Whether this backup brings a different configuration than the one
        before it.

        The very first backup of a device is no change: it is compared against
        nothing, so nothing can be said about what it changed. It is the backup
        the later ones are measured against.
        """
        if not self.backup_text_hash:
            return False
        previous = self.previous_backup()
        return previous is not None and previous.backup_text_hash != self.backup_text_hash

    def save(self, *args, **kwargs):
        if self.backup_text:
            self.backup_text_hash = hashlib.sha256(self.backup_text.encode('utf-8')).hexdigest()
        else:
            self.backup_text_hash = ''
        self.config_change_detected = self.config_change_check()
        super(RouterBackup, self).save(*args, **kwargs)

