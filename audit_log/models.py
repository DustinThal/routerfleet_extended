import threading
import uuid
from contextlib import contextmanager

from django.conf import settings
from django.db import models

# What a value that is not written down in full is replaced with. A password is
# compared on its real value and stored as one of these, so the log proves that it
# was changed without holding a second copy of it.
HIDDEN_SET = '(set)'
HIDDEN_EMPTY = '(empty)'
UNREADABLE = '(unreadable)'

REMOVAL_ERROR = ('Audit entries are not removed from the interface. Use '
                 '"manage.py prune_audit_log" to remove entries older than a given age.')

_removal = threading.local()


class AuditLogImmutable(Exception):
    """Raised when something tries to remove an entry of the audit trail.

    Nothing in the interface removes one: not the two pages, not the admin, not a
    queryset in a shell. The command line has prune_audit_log for it, and that is
    the only place that opens the door - see allow_removal().
    """


def allow_removal():
    _removal.allowed = True


def forbid_removal():
    _removal.allowed = False


def removal_allowed():
    return bool(getattr(_removal, 'allowed', False))


@contextmanager
def removal_allowed_for_pruning():
    """Outside of this, a delete of an entry is refused."""
    previous = removal_allowed()
    allow_removal()
    try:
        yield
    finally:
        if previous:
            allow_removal()
        else:
            forbid_removal()


class LoginRecord(models.Model):
    """One attempt to sign in, successful or not.

    Kept beside the sessions table instead of read out of it: a session is removed
    when it ends or expires, and what is asked here - who signed in, from where,
    when - has to outlive the session it describes.

    The username is stored as it was typed rather than as a reference, because an
    attempt with a name that belongs to nobody is exactly the kind of thing worth
    reading later.
    """

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    username = models.CharField(max_length=150)
    successful = models.BooleanField(default=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                              blank=True, related_name='+')
    user_level = models.IntegerField(null=True, blank=True)
    session_key = models.CharField(max_length=40, blank=True)
    logged_out_at = models.DateTimeField(null=True, blank=True)
    actor_kind = models.CharField(max_length=16, blank=True)
    # CharFields, not an IP field: a header is written by whoever sends it, and an
    # odd one has to be recorded, not refused in the middle of somebody's login
    ip = models.CharField(max_length=45, blank=True)
    forwarded = models.CharField(max_length=255, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    uuid = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)

    class Meta:
        ordering = ['-created']

    def __str__(self):
        return f'{self.username} ({"signed in" if self.successful else "refused"})'

    @property
    def address_title(self):
        """What a hover over the address shows: the chain it arrived through, when
        there was one."""
        return self.forwarded or self.ip


class ChangeRecord(models.Model):
    """One change to the configuration: what was done, to what, by whom.

    Values are stored as they read at the time, not as references to something
    that may be renamed or deleted later. That is also why the actor is kept twice,
    as the account and as its name: closing an account must not blank out what it
    did before.
    """

    ACTION_CREATED = 'created'
    ACTION_CHANGED = 'changed'
    ACTION_DELETED = 'deleted'
    ACTION_BULK = 'bulk'
    ACTION_CHOICES = (
        (ACTION_CREATED, 'Created'),
        (ACTION_CHANGED, 'Changed'),
        (ACTION_DELETED, 'Deleted'),
        (ACTION_BULK, 'Bulk change'),
    )

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    model_label = models.CharField(max_length=64, blank=True)
    object_uuid = models.UUIDField(null=True, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                              blank=True, related_name='+')
    actor_name = models.CharField(max_length=150, blank=True)
    actor_level = models.IntegerField(null=True, blank=True)
    actor_kind = models.CharField(max_length=16, blank=True)
    ip = models.CharField(max_length=45, blank=True)
    forwarded = models.CharField(max_length=255, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    # {'Port': {'old': 22, 'new': 2222}} - insertion order is kept, and the popup
    # shows the fields in the order they were recorded in
    changes = models.JSONField(default=dict, blank=True)
    # How many rows a bulk entry stands for
    affected_count = models.IntegerField(default=0)
    # What a bulk entry was about: it has no single object to be named after
    note = models.CharField(max_length=200, blank=True)
    uuid = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)

    class Meta:
        ordering = ['-created']

    def __str__(self):
        return f'{self.get_action_display()} {self.display_object}'

    @property
    def model_name(self):
        """'Router' from 'router_manager.Router'."""
        return self.model_label.rsplit('.', 1)[-1] if self.model_label else ''

    @property
    def display_object(self):
        """What the Object column shows. A bulk entry has no one object, only what
        the block was about."""
        if self.action == self.ACTION_BULK:
            return self.note
        return self.object_repr

    @property
    def field_summary(self):
        """The text of the link that opens the entry."""
        if self.action == self.ACTION_BULK:
            return f'{self.affected_count} rows'
        if isinstance(self.changes, dict):
            return ', '.join(str(name) for name in self.changes)
        return ''

    @property
    def address_title(self):
        return self.forwarded or self.ip
