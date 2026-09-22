import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Count, Q
from django.utils import timezone

from router_manager.models import Router, RouterGroup, SUPPORTED_ROUTER_TYPES


class Command(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True, null=True)

    enabled = models.BooleanField(default=True)

    capture_output = models.BooleanField(default=False)
    max_retry = models.IntegerField(default=3)
    retry_interval = models.IntegerField(default=30, help_text="Retry interval in seconds")

    # Verification of the result. The exit code of a command is not reliable on
    # every router (an update that reboots the device never returns one), so a
    # variant can define commands that check the result instead.
    verify_timeout = models.PositiveIntegerField(
        default=300, help_text="Seconds to keep verifying after the payload has run")
    verify_interval = models.PositiveIntegerField(
        default=30, help_text="Seconds between two verification attempts")

    updated = models.DateTimeField(auto_now=True)
    created = models.DateTimeField(auto_now_add=True)
    uuid = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)

    def __str__(self) -> str:
        return self.name

    @property
    def can_execute(self):
        return self.enabled and self.variants.exists()


class CommandSchedule(models.Model):
    command = models.ForeignKey(Command, on_delete=models.PROTECT, related_name="schedules")
    router = models.ManyToManyField(Router, blank=True, related_name="command_schedules")
    router_group = models.ManyToManyField(RouterGroup, blank=True, related_name="command_schedules")

    # Devices and groups that are taken out of this schedule again. An exclusion
    # always wins: a device that a selected group brings in and that stands in an
    # excluded group, or was excluded itself, is not executed.
    exclude_router = models.ManyToManyField(
        Router, blank=True, related_name="excluded_command_schedules",
        verbose_name="Excluded routers",
        help_text="These devices are left out, also when a selected group brings them in.")
    exclude_router_group = models.ManyToManyField(
        RouterGroup, blank=True, related_name="excluded_command_schedules",
        verbose_name="Excluded router groups",
        help_text="Every device of these groups is left out, also when another selected group brings it in.")
    enabled = models.BooleanField(default=True)

    start_at = models.DateTimeField(blank=True, null=True)
    end_at = models.DateTimeField(blank=True, null=True)
    repeat_interval = models.IntegerField(help_text="Repeat interval in minutes", default=0)

    last_run = models.DateTimeField(blank=True, null=True)
    next_run = models.DateTimeField(blank=True, null=True)

    updated = models.DateTimeField(auto_now=True)
    created = models.DateTimeField(auto_now_add=True)
    uuid = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)

    @property
    def repeat_interval_display(self):
        val = self.repeat_interval
        if val % 1440 == 0:
            return f"{int(val / 1440)}d"
        elif val % 60 == 0:
            return f"{int(val / 60)}h"
        return f"{val}m"

    @property
    def calculate_next_run(self):
        if not self.enabled or self.repeat_interval <= 0:
            return None

        now = timezone.now()
        base = self.start_at if self.start_at else now

        if base <= now:
            delta = now - base
            intervals_passed = int(delta.total_seconds() / 60 / self.repeat_interval) + 1
            next_run = base + timedelta(minutes=self.repeat_interval * intervals_passed)
        else:
            next_run = base

        if self.end_at and next_run > self.end_at:
            return None

        return next_run

    def update_next_run(self):
        self.next_run = self.calculate_next_run
        self.save(update_fields=["next_run"])
        return self.next_run

    def selected_routers(self) -> set:
        """The devices the selection brings in, the exclusions not subtracted."""
        routers = set(self.router.filter(enabled=True))
        for group in self.router_group.all():
            routers.update(group.routers.filter(enabled=True))
        return routers

    def target_routers(self) -> set:
        """The devices this schedule runs on.

        The exclusions are subtracted at the end, so an exclusion wins over
        everything: a device that a selected group brings in and that stands in
        an excluded group, or was excluded itself, is not run.
        """
        routers = self.selected_routers()

        excluded = set(self.exclude_router.all())
        for group in self.exclude_router_group.all():
            excluded.update(group.routers.all())

        return routers - excluded

    @property
    def target_summary(self):
        """What this schedule runs on, for the list of schedules."""
        selected = self.selected_routers()
        targets = self.target_routers()
        if not selected:
            return 'No device selected'

        text = f'{len(targets)} ' + ('device' if len(targets) == 1 else 'devices')
        excluded = len(selected) - len(targets)
        if excluded:
            text += f', {excluded} excluded'
        return text

    def disable_if_invalid(self) -> bool:
        has_active_variant = self.command.variants.filter(enabled=True).exists()
        has_active_router = bool(self.target_routers())
        if not has_active_variant or not has_active_router or not self.command.enabled:
            self.enabled = False
            self.save(update_fields=["enabled"])
        return self.enabled


class CommandVariant(models.Model):
    command = models.ForeignKey(Command, on_delete=models.PROTECT, related_name="variants")
    router_type = models.CharField(max_length=100, choices=SUPPORTED_ROUTER_TYPES)
    payload = models.TextField()

    # Optional verification. These commands are executed after the payload and
    # their output decides whether the task was successful. Every line of
    # verify_expect has to be found in that output. {{ available_version }} and
    # {{ current_version }} are replaced with the versions known for the router.
    verify_payload = models.TextField(blank=True, null=True)
    verify_expect = models.TextField(blank=True, null=True)

    enabled = models.BooleanField(default=True)

    updated = models.DateTimeField(auto_now=True)
    created = models.DateTimeField(auto_now_add=True)
    uuid = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)

    @property
    def has_verification(self):
        return bool((self.verify_payload or '').strip() and (self.verify_expect or '').strip())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["command", "router_type"],
                name="uniq_command_variant_per_router_type",
            )
        ]
        indexes = [
            models.Index(fields=["router_type", "enabled"]),
        ]

    def __str__(self) -> str:
        return f"{self.command.name} ({self.router_type})"


class CommandJob(models.Model):
    command = models.ForeignKey(Command, on_delete=models.PROTECT, related_name="jobs")
    user_source = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True)
    user_source_name = models.CharField(max_length=150, blank=True, null=True)
    exec_source = models.CharField(max_length=100, choices=(('schedule', 'Schedule'), ('manual', 'Manual')), default='manual')
    completed = models.DateTimeField(blank=True, null=True)
    task_count = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    aborted_count = models.PositiveIntegerField(default=0)

    updated = models.DateTimeField(auto_now=True)
    created = models.DateTimeField(auto_now_add=True)
    uuid = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)

    def __str__(self):
        return f"#{self.id}: {self.command.name}"

    def update_counters(self):
        tasks = self.tasks.aggregate(
            total=Count('id'),
            success=Count('id', filter=Q(status='success')),
            error=Count('id', filter=Q(status='error')),
            aborted=Count('id', filter=Q(status='aborted')),
        )
        self.task_count = tasks['total']
        self.success_count = tasks['success']
        self.error_count = tasks['error']
        self.aborted_count = tasks['aborted']

        update_fields = ['task_count', 'success_count', 'error_count', 'aborted_count']

        finished_count = tasks['success'] + tasks['error'] + tasks['aborted']
        if finished_count >= tasks['total'] and not self.completed:
            self.completed = timezone.now()
            update_fields.append('completed')

        self.save(update_fields=update_fields)

    @property
    def progress_percentage(self):
        if self.task_count == 0:
            return 0
        # An aborted task is finished as well, a job that was stopped has to
        # reach 100% instead of staying below it forever
        finished = self.success_count + self.error_count + self.aborted_count
        return int((finished / self.task_count) * 100)


class CommandTask(models.Model):
    job = models.ForeignKey(CommandJob, on_delete=models.CASCADE, related_name="tasks")
    command_variant = models.ForeignKey(CommandVariant, on_delete=models.SET_NULL, null=True, blank=True)
    router = models.ForeignKey(Router, on_delete=models.SET_NULL, blank=True, null=True)
    router_name = models.CharField(max_length=100, blank=True, null=True)
    router_uuid = models.UUIDField()

    command_payload = models.TextField(blank=True, null=True)
    command_executed = models.TextField(blank=True, null=True)
    command_output = models.TextField(blank=True, null=True)

    # Verification of the result. The payload is executed once; while the
    # verification has not passed the task is retried and only verifies again, so
    # a device that reboots in the middle of an update is not updated twice.
    payload_executed = models.BooleanField(default=False)
    verify_context = models.JSONField(default=dict, blank=True)
    verify_deadline = models.DateTimeField(blank=True, null=True)
    verification_attempts = models.PositiveIntegerField(default=0)
    verification_output = models.TextField(blank=True, null=True)
    verified = models.BooleanField(default=False)

    status = models.CharField(max_length=100, choices=(('pending', 'Pending'), ('error', 'Error'), ('success', 'Success'), ('aborted', 'Aborted')), default='pending')
    retry_count = models.PositiveIntegerField(default=0)
    next_retry = models.DateTimeField(blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    updated = models.DateTimeField(auto_now=True)
    created = models.DateTimeField(auto_now_add=True)
    uuid = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "router_uuid"],
                name="uniq_commandtask_job_router_uuid",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "next_retry"]),
            models.Index(fields=["router_uuid", "created"]),
            models.Index(fields=["job", "status"]),
        ]

    def save(self, *args, **kwargs):
        if self.router:
            self.router_uuid = self.router.uuid
            self.router_name = self.router.name
        self.full_clean()
        super().save(*args, **kwargs)
        self.job.update_counters()

    def __str__(self) -> str:
        return f"{self.job.command.name} -> {self.router_name or self.router_uuid} ({self.status})"