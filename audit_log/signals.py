"""Everything the audit trail listens to.

Two rules hold for every receiver here.

It is connected with a sender. A receiver without one counts as a listener for
every model in the project, and Django then keeps a model out of its fast delete
path - the housekeeping that removes hundreds of expired backups would stop doing
it in one statement and start doing it row by row, every ten minutes.

It never raises. An audit trail that can break a sign-in or a save is worse than no
audit trail at all, so anything unexpected is logged and dropped.
"""
import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.core.exceptions import FieldDoesNotExist
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.utils import timezone

from . import bulk, context, registry
from .models import (
    REMOVAL_ERROR, AuditLogImmutable, ChangeRecord, LoginRecord, removal_allowed,
)

logger = logging.getLogger(__name__)


def connect():
    """Listen to everything worth listening to.

    Called from ready(), after every model of the project exists. Every receiver
    carries a dispatch_uid, so connecting twice is the same as connecting once.
    """
    registry.build()

    for watched in list(registry.WATCHED.values()):
        label = watched.label
        pre_save.connect(on_pre_save, sender=watched.model,
                         dispatch_uid=f'audit_log.pre_save.{label}')
        post_save.connect(on_post_save, sender=watched.model,
                          dispatch_uid=f'audit_log.post_save.{label}')
        if watched.delete:
            post_delete.connect(on_post_delete, sender=watched.model,
                                dispatch_uid=f'audit_log.post_delete.{label}')
        for name in watched.m2m:
            through = m2m_through(watched.model, name)
            if through is None:
                continue
            m2m_changed.connect(on_m2m_changed, sender=through,
                                dispatch_uid=f'audit_log.m2m.{label}.{name}')

    for model in (LoginRecord, ChangeRecord):
        pre_delete.connect(on_audit_delete, sender=model,
                           dispatch_uid=f'audit_log.guard.{model._meta.label}')

    user_logged_in.connect(on_user_logged_in, dispatch_uid='audit_log.user_logged_in')
    user_logged_out.connect(on_user_logged_out, dispatch_uid='audit_log.user_logged_out')
    user_login_failed.connect(on_user_login_failed, dispatch_uid='audit_log.user_login_failed')


def m2m_through(model, name):
    """The model Django puts between the two ends of a membership.

    That model is what m2m_changed is sent for, so that is what has to be listened
    to: listening to the two models themselves would never hear anything.
    """
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        return None
    return field.remote_field.through if field.remote_field else None


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------

def on_pre_save(sender, instance, raw=False, using=None, update_fields=None, **kwargs):
    """Keep the row as it was, so that post_save has something to compare against.

    A save that names the fields it writes - which is what every hot path of this
    project does, for run state that is not watched - is turned away here, before
    anything is read.
    """
    try:
        if raw:
            return
        watched = registry.watched(sender)
        if watched is None or not _touches(watched, update_fields):
            return
        if instance.pk is None:
            # Nothing to compare against: this is a new row, and what it was
            # created with is read from the instance in post_save
            return
        before = _read_row(sender, instance.pk, using, watched)
        if before is not None:
            instance._audit_before = registry.snapshot(before, watched)
    except Exception:
        logger.warning('The audit trail could not read a row before it changed.', exc_info=True)


def on_post_save(sender, instance, created=False, raw=False, using=None, update_fields=None,
                 **kwargs):
    try:
        if raw:
            return
        watched = registry.watched(sender)
        if watched is None or not _touches(watched, update_fields):
            return
        if created:
            _forget(instance)
            if not watched.create:
                return
            changes = registry.filled(
                registry.diff(watched, {}, registry.snapshot(instance, watched)))
            if not changes or _suppressed(watched.name):
                return
            _write(build_record(watched, ChangeRecord.ACTION_CREATED, instance=instance,
                                changes=changes))
            return
        before = _forget(instance)
        if before is None:
            return
        changes = registry.diff(watched, before, registry.snapshot(instance, watched))
        if not changes:
            # A save that changed nothing is not a change, and that is what keeps
            # the log quiet while the cron writes its own progress
            return
        if _suppressed(watched.name):
            return
        _write(build_record(watched, ChangeRecord.ACTION_CHANGED, instance=instance,
                            changes=changes))
    except Exception:
        logger.warning('The audit trail could not record a change.', exc_info=True)


def on_post_delete(sender, instance, using=None, **kwargs):
    """What was removed, with the values it had.

    post_delete and not pre_delete: a model with a post_delete listener is never
    deleted in one statement, so Django hands the full instance over here - the
    entry can say what was removed instead of only that something was. It is also
    why every receiver of this module names its sender.
    """
    try:
        watched = registry.watched(sender)
        if watched is None or not watched.delete:
            return
        if _suppressed(watched.name):
            return
        # A deletion is worth recording whether or not any of the watched fields
        # was ever set: what matters is that it is gone
        changes = registry.filled(
            registry.diff(watched, registry.snapshot(instance, watched), {}), side='old')
        _write(build_record(watched, ChangeRecord.ACTION_DELETED, instance=instance,
                            changes=changes))
    except Exception:
        logger.warning('The audit trail could not record a deletion.', exc_info=True)


def on_m2m_changed(sender, instance, action, reverse=False, model=None, pk_set=None, using=None,
                   **kwargs):
    """A membership that was added to or taken away from.

    Only the forward direction: nothing in this project writes a membership from
    the far side of the relation, and the entry belongs to the object that holds
    the membership anyway.

    post_add is arithmetically exact - pk_set is what was actually inserted, so what
    was there before is what is there now minus that. remove() may be given members
    that were never in the set and clear() passes no ids at all, so those two
    snapshot the set before they act and compare afterwards.
    """
    try:
        if reverse:
            return
        watched = registry.watched(type(instance))
        if watched is None:
            return
        field_name = _m2m_field_name(watched, sender)
        if field_name is None:
            return

        if action in ('pre_remove', 'pre_clear'):
            _stash(instance, field_name, _m2m_pks(instance, field_name))
            return
        if action in ('post_remove', 'post_clear'):
            before = _unstash(instance, field_name)
            if before is None:
                return
            after = _m2m_pks(instance, field_name)
        elif action == 'post_add':
            after = _m2m_pks(instance, field_name)
            before = after - set(pk_set or ())
        else:
            return

        if before == after:
            return
        if _suppressed(watched.name):
            return
        names = _names(model, before | after)
        changes = {watched.label_of(field_name): {
            'old': _listed(names, before - after),
            'new': _listed(names, after - before),
        }}
        _write(build_record(watched, ChangeRecord.ACTION_CHANGED, instance=instance,
                            changes=changes))
    except Exception:
        logger.warning('The audit trail could not record a membership change.', exc_info=True)


# ---------------------------------------------------------------------------
# Signing in and out
# ---------------------------------------------------------------------------

def on_user_logged_in(sender, request=None, user=None, **kwargs):
    try:
        if user is None:
            return
        _, fields = _actor_fields(request)
        fields['username'] = context.user_name(user)
        fields['user_level'] = context.user_level(user)
        fields['session_key'] = context.text(
            getattr(getattr(request, 'session', None), 'session_key', ''), 40)
        LoginRecord(successful=True, **fields).save()
    except Exception:
        logger.warning('The audit trail could not record a sign-in.', exc_info=True)


def on_user_login_failed(sender, credentials=None, request=None, **kwargs):
    """An attempt that was refused.

    The credentials Django passes here are never read: the password in them is
    already masked, and a password does not belong in a log under any circumstances.
    A name that belongs to nobody is worth a row all the same - it is how a run of
    attempts is noticed.
    """
    try:
        fields = context.identity(request)
        fields['actor'] = None
        fields['actor_kind'] = context.ACTOR_ANONYMOUS
        fields['username'] = context.text((credentials or {}).get('username'), 150)
        fields['user_level'] = None
        fields['session_key'] = ''
        LoginRecord(successful=False, **fields).save()
    except Exception:
        logger.warning('The audit trail could not record a refused sign-in.', exc_info=True)


def on_user_logged_out(sender, request=None, user=None, **kwargs):
    """Closes the entry the session opened.

    The signal is sent before the session is flushed, which is why the session key
    can still be read here. It is also sent for an anonymous request to the logout
    URL, where there is no user and nothing to close.
    """
    try:
        if user is None:
            return
        session_key = context.text(
            getattr(getattr(request, 'session', None), 'session_key', ''), 40)
        open_records = LoginRecord.objects.filter(actor=user, successful=True,
                                                  logged_out_at__isnull=True)
        if session_key:
            open_records = open_records.filter(session_key=session_key)
        record = open_records.order_by('-created').first()
        if record is None:
            return
        record.logged_out_at = timezone.now()
        record.save(update_fields=['logged_out_at'])
    except Exception:
        logger.warning('The audit trail could not record a sign-out.', exc_info=True)


# ---------------------------------------------------------------------------
# Keeping the trail
# ---------------------------------------------------------------------------

def on_audit_delete(sender, instance, **kwargs):
    """The one thing that keeps the trail: not the missing button, not the admin,
    but this.

    A queryset delete - the admin's bulk action, a filter in a shell, a cascade -
    never calls Model.delete(), so overriding that method would be a promise that
    leaks. This is called for every removal there is.
    """
    if removal_allowed():
        return
    raise AuditLogImmutable(REMOVAL_ERROR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_record(watched, action, instance=None, changes=None, count=0, note=''):
    """One entry, with who did it and from where filled in."""
    user, fields = _actor_fields()
    fields['action'] = action
    fields['changes'] = registry.stored(changes or {})
    fields['affected_count'] = count
    fields['note'] = note
    if watched is not None and instance is not None:
        fields['model_label'] = watched.label
        fields['object_uuid'] = getattr(instance, 'uuid', None)
        fields['object_repr'] = watched.describe(instance)
    fields['actor_name'] = context.user_name(user)
    fields['actor_level'] = context.user_level(user)
    return ChangeRecord(**fields)


def _actor_fields(request=None):
    """Who and where, from the request the middleware left behind - or from a
    management command, where there is no request and no person."""
    user, kind = context.actor(request)
    fields = context.identity(request)
    fields['actor'] = user
    fields['actor_kind'] = kind
    return user, fields


def _touches(watched, update_fields):
    """Whether a save can have changed anything worth recording.

    A save that names the fields it writes and names none of the watched ones cannot
    have changed the configuration, so nothing is read and nothing is compared.
    """
    if update_fields is None:
        return True
    return bool(set(update_fields) & set(watched.fields))


def _read_row(model, pk, using, watched):
    queryset = model._base_manager.using(using or 'default')
    related = registry.related_names(watched)
    if related:
        queryset = queryset.select_related(*related)
    return queryset.filter(pk=pk).first()


def _forget(instance):
    """Take the stashed row back off the instance, so that a second save does not
    compare against the row as it was two changes ago."""
    return instance.__dict__.pop('_audit_before', None)


def _write(record):
    try:
        record.save()
    except Exception:
        logger.warning('The audit trail could not write an entry.', exc_info=True)


def _suppressed(name):
    """Whether this record must not be written.

    True inside a block that is being counted into one entry instead - the counting
    happens here, so a block does not have to know what it removed - and True inside
    suspend(), where nothing is written at all.
    """
    if bulk.is_suspended():
        return True
    batch = bulk.current_batch()
    if batch is not None:
        batch.suppressed(name)
        return True
    return False


def _m2m_field_name(watched, through):
    for name in watched.m2m:
        if m2m_through(watched.model, name) is through:
            return name
    return None


def _m2m_pks(instance, field_name):
    return set(getattr(instance, field_name).values_list('pk', flat=True))


def _stash(instance, field_name, pks):
    stashed = getattr(instance, '_audit_m2m', None)
    if stashed is None:
        stashed = {}
        instance._audit_m2m = stashed
    stashed[field_name] = pks


def _unstash(instance, field_name):
    return getattr(instance, '_audit_m2m', {}).pop(field_name, None)


def _names(model, pks):
    """What the objects behind a set of ids are called."""
    if not pks or model is None:
        return {}
    names = {}
    for obj in model._base_manager.filter(pk__in=pks):
        names[obj.pk] = str(obj)
    return names


def _listed(names, pks):
    """The names, in a stable order, and capped."""
    limit = registry.M2M_LIST_LIMIT
    ordered = sorted(names.get(pk, str(pk)) for pk in pks)
    if len(ordered) <= limit:
        return ordered
    return ordered[:limit] + [f'and {len(ordered) - limit} more']
