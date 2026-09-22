import uuid

from django.contrib.auth.decorators import login_required
from django.contrib.sessions.models import Session
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from user_manager.models import UserAcl

from .models import ChangeRecord, LoginRecord

# The pages show the newest entries. Nothing else in this project pages on the
# server, and the log grows without an end - so it is capped here rather than made
# the first page that takes a minute to draw. Older entries stay in the database.
ROW_LIMIT = 1000


@login_required
def view_login_history(request):
    if not UserAcl.objects.filter(user=request.user).filter(user_level__gte=50).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    login_list = list(LoginRecord.objects.all()[:ROW_LIMIT])
    context = {
        'page_title': 'Login History',
        'login_list': login_list,
        'open_sessions': open_sessions(login_list),
        'row_limit': ROW_LIMIT,
    }
    return render(request, 'audit_log/login_history.html', context)


@login_required
def view_change_log(request):
    if not UserAcl.objects.filter(user=request.user).filter(user_level__gte=50).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    context = {
        'page_title': 'Change Log',
        'change_list': ChangeRecord.objects.all()[:ROW_LIMIT],
        'row_limit': ROW_LIMIT,
    }
    return render(request, 'audit_log/change_log.html', context)


@login_required
def view_change_detail(request):
    """One entry, field by field, for the popup.

    JSON only: the page the popup is opened from does not move.
    """
    if not UserAcl.objects.filter(user=request.user).filter(user_level__gte=50).exists():
        return JsonResponse({'error': 'You are not allowed to read the change log.'}, status=403)
    try:
        identifier = uuid.UUID(str(request.GET.get('uuid', '')))
    except (AttributeError, TypeError, ValueError):
        # An address that is not a uuid is not a uuid that is not there, but the
        # answer to the caller is the same either way
        return JsonResponse({'error': 'This entry is not in the change log.'}, status=404)
    record = ChangeRecord.objects.filter(uuid=identifier).first()
    if record is None:
        return JsonResponse({'error': 'This entry is not in the change log.'}, status=404)
    return JsonResponse({
        'action': record.get_action_display(),
        'model': record.model_name,
        'object': record.display_object,
        'actor': record.actor_name,
        'actor_kind': record.actor_kind,
        'level': record.actor_level,
        'moment': timezone.localtime(record.created).strftime('%Y-%m-%d %H:%M'),
        'ip': record.ip,
        'forwarded': record.forwarded,
        'user_agent': record.user_agent,
        'count': record.affected_count,
        'note': record.note,
        # A list and not the dict it is stored as: a JSON object has no order, and
        # the popup shows the fields in the order they were recorded in
        'changes': [{'field': label, 'old': change.get('old'), 'new': change.get('new')}
                    for label, change in changes_of(record)],
    })


def changes_of(record):
    """The recorded fields as pairs, whatever the column happens to hold."""
    changes = record.changes
    if not isinstance(changes, dict):
        return []
    return [(label, change if isinstance(change, dict) else {})
            for label, change in changes.items()]


def open_sessions(records):
    """The session keys of the page's entries that are still alive.

    One query for the page rather than one per row. A session that was signed out is
    removed from the table, so what is left here is what is still signed in.
    """
    keys = [record.session_key for record in records if record.session_key]
    if not keys:
        return set()
    return set(Session.objects.filter(session_key__in=keys, expire_date__gt=timezone.now())
               .values_list('session_key', flat=True))
