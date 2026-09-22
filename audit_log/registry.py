"""What is worth recording, and what is not.

An allowlist: a model is watched because it stands written down here, never
because something happened to save it. The other way round - watch everything and
filter the noise out afterwards - would fill the log with rows nobody asked for
and put a read in front of every single write of the project: WebadminSettings is
saved each time a page is drawn, and the housekeeping saves hundreds of rows an
hour.

Two things follow from the list, and they are the whole reason automated work
stays quiet:

* run state is not on it - last_run, next_run, last_login, update_requested,
  profile_error_information, the last daily report - so a job writing its own
  progress produces nothing at all, without a single special case for it.
* a saved instance is compared against the row as it was, and only what differs is
  written down, so a save that changes nothing writes nothing.

Adding a model here is the whole of what it takes to have it recorded.
"""
import datetime

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.utils import timezone

from .models import HIDDEN_EMPTY, HIDDEN_SET, UNREADABLE

# A text value is cut to this. The log says what changed, it is not a second copy
# of the database.
TEXT_LIMIT = 400
# A membership change lists the devices that moved, not the whole group
M2M_LIST_LIMIT = 50

WATCHED = {}


class Masked:
    """A value that is compared as itself and written down as a marker.

    A password, a private key, a token: the log has to be able to say that it was
    changed, and it must not hold what it was changed to. A plain marker cannot do
    both - two different passwords would look alike, and every re-save of a router
    would look like a change. So the snapshot keeps the value and answers equality
    with it, while printing itself as the marker, and only the marker is ever
    written (see plain()).
    """

    __slots__ = ('marker', 'value')

    def __init__(self, marker, value):
        self.marker = marker
        self.value = value

    def __eq__(self, other):
        return isinstance(other, Masked) and other.value == self.value

    def __ne__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        return self.marker

    def __repr__(self):
        return self.marker


def plain(value):
    """A snapshot value as the database can hold it: never the secret itself."""
    return str(value) if isinstance(value, Masked) else value


def stored(changes):
    """The same changes, ready to be written down."""
    return {label: {side: plain(value) for side, value in change.items()}
            for label, change in changes.items()}


class Watched:
    """One watched model, and which parts of it are."""

    def __init__(self, model, fields, create=False, delete=True, redact=(), m2m=(), repr=None):
        self.model = model
        self.label = model._meta.label
        self.name = model._meta.object_name
        self.fields = tuple(fields)
        # Whether a record of this model being created is worth writing. False
        # where the row creates itself the first time a page is opened: an entry
        # would claim a person made something that made itself.
        self.create = create
        self.delete = delete
        # Fields that are compared on their real value and stored masked
        self.redact = frozenset(redact)
        self.m2m = tuple(m2m)
        self.repr = repr
        self._labels = {}

    def label_of(self, name):
        """The name of a field as it is written in the log."""
        if name not in self._labels:
            self._labels[name] = field_label(self.model, name)
        return self._labels[name]

    def describe(self, instance):
        """What the object is called in the log."""
        described = self.repr(instance) if self.repr else str(instance)
        return _short(described, 255)


def watch(model, fields, create=False, delete=True, redact=(), m2m=(), repr=None):
    WATCHED[model._meta.label] = Watched(model, fields, create, delete, redact, m2m, repr)


def watched(model):
    """The entry for a model, or None - which is the answer for most of them."""
    return WATCHED.get(model._meta.label)


def field_label(model, name):
    """A field as it is named in the log: 'port' -> 'Port', 'ssh_key' -> 'Ssh key'."""
    try:
        verbose = str(model._meta.get_field(name).verbose_name)
    except FieldDoesNotExist:
        verbose = name.replace('_', ' ')
    return verbose[:1].upper() + verbose[1:]


def editable(model, exclude=()):
    """Every field a person can edit, minus the ones named.

    Editable is the useful half of the distinction: a field no form can reach -
    created, updated, uuid, anything the model declares as not editable - is not
    something anybody changed on purpose.
    """
    skip = set(exclude)
    names = []
    for field in model._meta.get_fields():
        if not getattr(field, 'concrete', False) or field.auto_created:
            continue
        if not field.editable or field.name in skip:
            continue
        names.append(field.name)
    return names


def related_names(watched):
    """The relations a snapshot would otherwise fetch one by one."""
    names = []
    for name in watched.fields:
        try:
            field = watched.model._meta.get_field(name)
        except FieldDoesNotExist:
            continue
        if field.is_relation and (field.many_to_one or field.one_to_one):
            names.append(name)
    return names


def snapshot(instance, watched, fields=None):
    """The watched fields of an instance as the log would write them."""
    return {name: value_of(instance, watched, name)
            for name in (watched.fields if fields is None else fields)}


def value_of(instance, watched, name):
    """One field, as the change log writes it down.

    Every read is guarded on its own: a value that cannot be read - a password
    whose key is gone, a relation to something that is no longer there - is worth a
    note in the log, and not the loss of the whole entry.
    """
    try:
        value = getattr(instance, name, None)
        if name in watched.redact:
            # Compared on the real value, stored as a marker: the log proves that
            # it was changed without holding what it was changed to
            return Masked(HIDDEN_SET if value else HIDDEN_EMPTY, value)
        display = None
        get_display = getattr(instance, f'get_{name}_display', None)
        if callable(get_display):
            display = get_display()
        return shown(value, display)
    except Exception:
        return UNREADABLE


def shown(value, display=None):
    """A value as the log writes it: readable, and never longer than it has to be."""
    if isinstance(value, bool):
        return value
    if display is not None and str(display).strip():
        # A choice is stored as one thing and read as another. Both are worth
        # having - "40 (configuration Manager)" - unless they are the same word
        if str(display).lower() != str(value).lower():
            return f'{value} ({display})'
        return str(display)
    if value is None:
        return ''
    # A moment is written in the time zone the interface shows it in, and as it
    # reads on a clock: a diff between two raw ISO strings is unreadable
    if isinstance(value, datetime.datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime('%Y-%m-%d %H:%M')
    if isinstance(value, datetime.date):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, datetime.time):
        return value.strftime('%H:%M')
    if isinstance(value, (int, float)):
        return value
    return _short(value, TEXT_LIMIT)


def diff(watched, before, after):
    """What changed between two snapshots, keyed by the name it is written under.

    Both sides are walked, not only the new one: a deletion is a snapshot compared
    against nothing, and reading only the new side would leave every removal with an
    empty list of what was removed.

    An empty result is the answer for a save that changed nothing, and it is what
    keeps the log quiet while the housekeeping writes its own progress.
    """
    changes = {}
    names = list(after) + [name for name in before if name not in after]
    for name in names:
        was = before.get(name)
        is_now = after.get(name)
        if was != is_now:
            changes[watched.label_of(name)] = {'old': was, 'new': is_now}
    return changes


def filled(changes, side='new'):
    """The same, without the fields that were empty on a given side.

    A created object starts from nothing, so every field it was left empty says
    nothing about who made it; a deletion is the mirror image of that, which is why
    the side that carries the value is the one that has to be named.
    """
    return {label: change for label, change in changes.items()
            if change[side] not in ('', None)}


def _short(value, length):
    text = str(value)
    if len(text) > length:
        return text[:length] + '...'
    return text


def build():
    """Fill in the allowlist.

    Called once, when the app is ready: by then every model of the project exists,
    which is why they are looked up by name instead of being imported here.
    """
    WATCHED.clear()
    from django.conf import settings

    # A router is the object the project is about: what it is called, where it is
    # and how to reach it. The password is compared and never written down.
    watch(apps.get_model('router_manager', 'Router'),
          fields=['name', 'enabled', 'address', 'port', 'username', 'password', 'ssh_key',
                  'monitoring', 'backup_profile', 'router_type', 'internal_notes'],
          create=True, redact=('password',))
    watch(apps.get_model('router_manager', 'RouterGroup'),
          fields=['name', 'default_group', 'internal_notes'],
          create=True, m2m=['routers'])
    watch(apps.get_model('router_manager', 'SSHKey'),
          fields=['name', 'public_key', 'private_key'],
          create=True, redact=('private_key',))

    # A profile is created for the installation when one is first needed, so only
    # the changes to it are a person's doing
    watch(apps.get_model('backup', 'BackupProfile'),
          fields=editable(apps.get_model('backup', 'BackupProfile'),
                          exclude=['profile_error_information']),
          create=False)

    watch(apps.get_model('fleet_commander', 'Command'),
          fields=['name', 'description', 'enabled', 'capture_output', 'max_retry',
                  'retry_interval', 'verify_timeout', 'verify_interval'],
          create=True)
    watch(apps.get_model('fleet_commander', 'CommandVariant'),
          fields=['command', 'router_type', 'payload', 'verify_payload', 'verify_expect', 'enabled'],
          create=True)
    # The four memberships are listed one by one on purpose: the model also holds
    # last_run and next_run, which the cron writes and nobody edited
    watch(apps.get_model('fleet_commander', 'CommandSchedule'),
          fields=['command', 'enabled', 'start_at', 'end_at', 'repeat_interval'],
          create=True,
          m2m=['router', 'router_group', 'exclude_router', 'exclude_router_group'],
          # No __str__ of its own: without this the log would read
          # "CommandSchedule object (7)"
          repr=lambda instance: f'{instance.command} (schedule)')
    watch(apps.get_model('fleet_commander', 'ScheduleDefaults'),
          fields=['start_time', 'repeat_interval'],
          create=False)

    watch(apps.get_model('message_center', 'MessageChannel'),
          fields=['name', 'enabled', 'channel_type', 'destination', 'token',
                  'status_change_offline', 'status_change_online', 'backup_fail',
                  'daily_status_report', 'daily_backup_report'],
          create=True, redact=('token',))
    watch(apps.get_model('message_center', 'MessageSettings'),
          fields=editable(apps.get_model('message_center', 'MessageSettings'),
                          exclude=['name', 'last_daily_status_report', 'last_daily_backup_report']),
          create=False,
          # A single row with no name of its own
          repr=lambda instance: 'Message settings')

    watch(apps.get_model('integration_manager', 'ExternalIntegration'),
          fields=['name', 'integration_type', 'integration_url',
                  'wireguard_webadmin_default_user_level', 'token'],
          create=False, redact=('token',))

    # A user and their level. address_link is deliberately missing: it is the
    # self-service link a person sets for themselves, not a permission.
    watch(apps.get_model(settings.AUTH_USER_MODEL),
          fields=['username', 'first_name', 'last_name', 'email', 'is_active',
                  'is_superuser', 'password'],
          create=True, redact=('password',))
    watch(apps.get_model('user_manager', 'UserAcl'),
          fields=['user', 'user_level'],
          create=True)
