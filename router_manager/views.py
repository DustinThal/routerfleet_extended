import json
import time
from urllib.parse import unquote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from audit_log.bulk import collect
from backup.models import BackupProfile
from backup_data.models import RouterBackup
from routerfleet_tools.models import WebadminSettings
from routerlib.router_functions import update_router_information
from user_manager.models import UserAcl
from .forms import RouterForm, RouterBulkEditForm, RouterGroupForm, SSHKeyForm
from .models import Router, RouterGroup, RouterInformation, RouterStatus, SSHKey, BackupSchedule

# How long the information update cron task may run; it is started once per minute
MAX_UPDATE_INFORMATION_RUNTIME = 50


@login_required
def view_router_list(request):
    router_list = Router.objects.all().prefetch_related(
        'routerstatus', 
        'routerinformation', 
        'backupschedule', 
        'routergroup_set'
    ).order_by('name')

    # The newest backup that brought a change, for the "Last Configuration
    # Change" column. Annotated instead of asked per router, so the list stays one
    # query however many devices it shows. The time is when the configuration was
    # read from the device, which is what finish_time holds wherever it is known.
    change_backups = RouterBackup.objects.filter(
        router=OuterRef('pk'), config_change_detected=True).order_by('-created', '-id')
    router_list = router_list.annotate(
        last_config_change_time=Subquery(
            change_backups.annotate(change_time=Coalesce('finish_time', 'created'))
            .values('change_time')[:1]),
        last_config_change_uuid=Subquery(change_backups.values('uuid')[:1]),
    )

    last_router_status_change = RouterStatus.objects.filter(last_status_change__isnull=False).order_by('-last_status_change').first()
    if last_router_status_change:
        last_status_change_timestamp = last_router_status_change.last_status_change.isoformat()
    else:
        last_status_change_timestamp = 0

    default_backup_profile, default_backup_profile_created = BackupProfile.objects.get_or_create(name='default')
    filter_group = None
    if request.GET.get('filter_group'):
        if request.GET.get('filter_group') == 'all':
            pass
        else:
            filter_group = get_object_or_404(RouterGroup, uuid=request.GET.get('filter_group'))
            router_list = router_list.filter(routergroup=filter_group)

    if not filter_group and request.GET.get('filter_group') != 'all':
        filter_group = RouterGroup.objects.filter(default_group=True).first()
    # Parse the router_visible_columns cookie
    visible_columns = []
    if 'router_visible_columns' in request.COOKIES:
        try:
            visible_columns = json.loads(unquote(request.COOKIES['router_visible_columns']))
        except json.JSONDecodeError:
            # If the cookie is invalid, use default columns
            visible_columns = ["name", "type", "status", "backup", "groups"]
    else:
        # Default columns if cookie doesn't exist
        visible_columns = ["name", "type", "status", "backup", "groups"]

    context = {
        'router_list': router_list,
        'page_title': 'Router List',
        'filter_group_list': RouterGroup.objects.all().order_by('name'),
        'filter_group': filter_group,
        'last_status_change_timestamp': last_status_change_timestamp,
        'visible_columns': visible_columns,
        # Not every user has a UserAcl row, the first user created has none
        'address_link': UserAcl.objects.filter(user=request.user).values_list('address_link', flat=True).first() or 'none',
    }
    return render(request, 'router_manager/router_list.html', context=context)


@login_required()
def view_router_availability(request):
    router = get_object_or_404(Router, uuid=request.GET.get('uuid'))
    data = {
        'router': router,
        'downtime_list': router.routerdowntime_set.all().order_by('-start_time'),
    }
    return render(request, 'router_manager/router_availability.html', context=data)


@login_required()
def view_router_details(request):
    router = get_object_or_404(Router, uuid=request.GET.get('uuid'))
    router_status, router_status_created = RouterStatus.objects.get_or_create(router=router)
    router_backup_list = router.routerbackup_set.all().order_by('-created')
    router_information = RouterInformation.objects.filter(router=router).first()
    downtime_last_week = router.routerdowntime_set.filter(start_time__gte=timezone.now() - timezone.timedelta(days=7)).aggregate(total=Sum('total_down_time'))['total']
    if downtime_last_week is None:
        downtime_last_week = 0
    total_last_week = 7 * 24 * 60 * 60  # total seconds in a week
    last_week_availability = round((total_last_week - downtime_last_week) / total_last_week * 100, 3)
    if downtime_last_week > 0 and last_week_availability == 100:
        last_week_availability = 99.999

    if router_status.backup_lock:
        if not router_backup_list.filter(success=False, error=False).exists():
            router_status.backup_lock = None
            router_status.save()
            messages.warning(request, 'Backup lock removed|Backup lock was removed as there are no active backup tasks')

    context = {
        'router': router,
        'router_information': router_information,
        'router_status': router_status,
        'router_backup_list': router_backup_list,
        'page_title': 'Router Details',
        'offline_time_last_week': downtime_last_week,
        'last_week_availability': last_week_availability,
        'command_task_list': router.commandtask_set.all().order_by('-created')[:25],
    }


    return render(request, 'router_manager/router_details.html', context=context)


@login_required()
def view_manage_router(request):
    if not UserAcl.objects.filter(user=request.user).filter(user_level__gte=30).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    webadmin_settings, webadmin_settings_created = WebadminSettings.objects.get_or_create(name='webadmin_settings')

    if request.GET.get('uuid'):
        router = get_object_or_404(Router, uuid=request.GET.get('uuid'))
        if request.GET.get('action') == 'delete':
            if request.GET.get('confirmation') == 'delete':
                router.delete()
                messages.success(request, 'Router deleted successfully')
                webadmin_settings.router_config_last_updated = timezone.now()
                webadmin_settings.save()
                return redirect('router_list')
            else:
                messages.warning(request, 'Router not deleted|Invalid confirmation')
                return redirect('router_list')
        elif request.GET.get('action') == 'refresh_information':
            router_information, created = RouterInformation.objects.get_or_create(router=router)
            router_information.next_retry = None
            router_information.retry_count = 0
            router_information.success = False
            router_information.error = False
            router_information.error_message = ''
            router_information.save()
            # Run the update right away instead of waiting for the cron task
            success, error_message = update_router_information(router_information)
            if success:
                messages.success(request, 'Router information updated')
            else:
                # The message is shown as a "title|body" toast, so the body must not contain a pipe
                messages.warning(request, 'Router information update failed|' + (
                    error_message or 'See the router information panel for details.'
                ).replace('|', '/'))
            return redirect('/router/details/?uuid=' + str(router.uuid))
    else:
        router = None

    form = RouterForm(request.POST or None, instance=router)
    if form.is_valid():
        form.save()
        messages.success(request, 'Router saved successfully|It may take a few minutes until monitoring starts for this router.')
        router_status, router_status_created = RouterStatus.objects.get_or_create(router=form.instance)
        BackupSchedule.objects.filter(router=form.instance).delete()
        if form.instance.router_type == 'monitoring':
            RouterInformation.objects.filter(router=form.instance).delete()
        webadmin_settings.router_config_last_updated = timezone.now()
        webadmin_settings.save()
        return redirect('router_list')

    context = {
        'form': form,
        'page_title': 'Manage Router',
        'instance': router
    }
    return render(request, 'generic_form.html', context=context)


@login_required()
def view_router_group_list(request):
    context = {
        'router_group_list': RouterGroup.objects.all().order_by('name'),
        'page_title': 'Router Group List',
    }
    return render(request, 'router_manager/router_group_list.html', context=context)


@login_required()
def view_manage_router_group(request):
    if not UserAcl.objects.filter(user=request.user).filter(user_level__gte=40).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    if request.GET.get('uuid'):
        router_group = get_object_or_404(RouterGroup, uuid=request.GET.get('uuid'))
        if request.GET.get('action') == 'delete':
            if request.GET.get('confirmation') == 'delete':
                router_group.delete()
                messages.success(request, 'Router Group deleted successfully')
                return redirect('router_group_list')
            else:
                messages.warning(request, 'Router Group not deleted|Invalid confirmation')
                return redirect('router_group_list')
    else:
        router_group = None

    form = RouterGroupForm(request.POST or None, instance=router_group)
    if form.is_valid():
        form.save()
        messages.success(request, 'Router Group saved successfully')
        return redirect('router_group_list')

    context = {
        'form': form,
        'page_title': 'Manage Router Group',
        'instance': router_group
    }
    return render(request, 'generic_form.html', context=context)


@login_required()
def view_ssh_key_list(request):
    context = {
        'sshkey_list': SSHKey.objects.all().order_by('name'),
        'page_title': 'SSH Key List',
    }
    return render(request, 'router_manager/sshkey_list.html', context=context)


@login_required()
def view_manage_sshkey(request):
    if not UserAcl.objects.filter(user=request.user).filter(user_level__gte=40).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    if request.GET.get('uuid'):
        sshkey = get_object_or_404(SSHKey, uuid=request.GET.get('uuid'))
        if request.GET.get('action') == 'delete':
            if request.GET.get('confirmation') == 'delete':
                sshkey.delete()
                messages.success(request, 'SSH Key deleted successfully')
                return redirect('ssh_keys_list')
            else:
                messages.warning(request, 'SSH Key not deleted|Invalid confirmation')
                return redirect('ssh_keys_list')
    else:
        sshkey = None

    form = SSHKeyForm(request.POST or None, instance=sshkey)
    if form.is_valid():
        form.save()
        messages.success(request, 'SSH Key saved successfully')
        return redirect('ssh_keys_list')

    context = {
        'form': form,
        'page_title': 'Manage SSH Key',
        'instance': sshkey
    }
    return render(request, 'generic_form.html', context=context)


def create_instant_backup(router):
    if RouterBackup.objects.filter(router=router, success=False, error=False).exists():
        return 'Active router backup task already exists'

    if router.routerstatus.backup_lock:
        return 'Router backup is currently locked'

    if not router.backup_profile:
        return 'Router has no backup profile'

    router_backup = RouterBackup.objects.create(
        router=router,
        schedule_time=timezone.now(),
        schedule_type='instant'
    )

    router.routerstatus.backup_lock = router_backup.schedule_time
    router.routerstatus.save()

    return None


@login_required()
def view_create_instant_backup_task(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})

    router = get_object_or_404(Router, uuid=request.GET.get('uuid'))
    router_details_url = f'/router/details/?uuid={router.uuid}'

    error = create_instant_backup(router)
    if error:
        messages.warning(request, f'Backup task not created | {error}')
    else:
        messages.success(request, 'Backup task created successfully')

    return redirect(router_details_url)


@login_required
def view_create_instant_backup_multiple_routers(request):
    if request.method == 'POST':
        if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
            return JsonResponse({'error': 'Permission denied.'}, status=403)

        uuids = request.POST.getlist('routers[]')
        if not uuids:
            return JsonResponse({'error': 'No routers selected.'}, status=400)

        results = []
        for uuid in uuids:
            router = get_object_or_404(Router, uuid=uuid)
            error = create_instant_backup(router)
            results.append({'router': router.name, 'status': error})

        return JsonResponse({'results': results})

    return JsonResponse({'error': 'Invalid request method.'}, status=405)


@login_required
def view_update_routers_information_multiple(request):
    """
    Queue an information update for the selected routers.

    The update itself is done by the update_router_information cron task, which
    picks requested routers up before its regular refresh queue.
    """
    if request.method == 'POST':
        if not UserAcl.objects.filter(user=request.user, user_level__gte=30).exists():
            return JsonResponse({'error': 'Permission denied.'}, status=403)

        uuids = request.POST.getlist('routers[]')
        if not uuids:
            return JsonResponse({'error': 'No routers selected.'}, status=400)

        requested = 0
        for router in Router.objects.filter(uuid__in=uuids):
            router_information, created = RouterInformation.objects.get_or_create(router=router)
            router_information.update_requested = True
            router_information.retry_count = 0
            router_information.error = False
            router_information.error_message = ''
            router_information.save()
            requested += 1

        return JsonResponse({'requested': requested})

    return JsonResponse({'error': 'Invalid request method.'}, status=405)


@login_required
def view_edit_routers_multiple(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=30).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})

    if request.method == 'POST':
        if 'routers[]' in request.POST:
            # First hop: selection arrives in the request body so the URL never grows
            # with the number of selected routers
            router_uuids = [str(uuid) for uuid in Router.objects.filter(
                uuid__in=request.POST.getlist('routers[]')
            ).values_list('uuid', flat=True)]
            if not router_uuids:
                messages.warning(request, 'No routers selected')
                return redirect('router_list')
            request.session['router_selection'] = router_uuids
            return redirect('edit_router_multiple')

        form = RouterBulkEditForm(request.POST)
        router_uuids = request.POST.getlist('router_uuids')
        if form.is_valid():
            routers = Router.objects.filter(uuid__in=router_uuids)
            ssh_key_uuid = form.cleaned_data.get('ssh_key')
            backup_profile_uuid = form.cleaned_data.get('backup_profile')
            ssh_key = None
            backup_profile = None
            if ssh_key_uuid and ssh_key_uuid != 'clear':
                ssh_key = get_object_or_404(SSHKey, uuid=ssh_key_uuid)
            if backup_profile_uuid and backup_profile_uuid != 'clear':
                backup_profile = get_object_or_404(BackupProfile, uuid=backup_profile_uuid)

            updated_count = 0
            # One entry for the whole edit instead of one per router: the form says
            # what to set, and the number of devices it reached is the interesting
            # part of it
            with collect('Bulk edit of routers'):
                for router in routers:
                    if form.cleaned_data.get('port'):
                        router.port = form.cleaned_data['port']
                    if form.cleaned_data.get('username'):
                        router.username = form.cleaned_data['username']
                    if form.cleaned_data.get('monitoring'):
                        router.monitoring = form.cleaned_data['monitoring'] == 'true'
                    if form.cleaned_data.get('enabled'):
                        router.enabled = form.cleaned_data['enabled'] == 'true'
                    if backup_profile_uuid:
                        router.backup_profile = backup_profile
                    if form.cleaned_data.get('internal_notes'):
                        router.internal_notes = form.cleaned_data['internal_notes']
                    if router.router_type != 'monitoring':
                        if form.cleaned_data.get('password'):
                            router.password = form.cleaned_data['password']
                            router.ssh_key = None
                        if ssh_key_uuid:
                            if ssh_key_uuid == 'clear':
                                router.ssh_key = None
                            else:
                                router.ssh_key = ssh_key
                            if not form.cleaned_data.get('password'):
                                router.password = ''
                    router.save()
                    updated_count += 1

            request.session.pop('router_selection', None)
            webadmin_settings, webadmin_settings_created = WebadminSettings.objects.get_or_create(name='webadmin_settings')
            webadmin_settings.router_config_last_updated = timezone.now()
            webadmin_settings.save()
            messages.success(request, f'{updated_count} routers updated successfully')
            return redirect('router_list')
    else:
        form = RouterBulkEditForm()

    router_uuids = request.session.get('router_selection') or request.GET.getlist('routers[]')
    if not router_uuids:
        messages.warning(request, 'No routers selected')
        return redirect('router_list')

    routers = Router.objects.filter(uuid__in=router_uuids)
    context = {
        'routers': routers,
        'form': form,
        'page_title': 'Edit Multiple Routers',
    }
    return render(request, 'router_manager/edit_routers_multiple.html', context=context)


@login_required
def view_manage_router_groups_multiple(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})

    if request.method == 'POST':
        if 'routers[]' in request.POST:
            # First hop: selection arrives in the request body so the URL never grows
            # with the number of selected routers
            router_uuids = [str(uuid) for uuid in Router.objects.filter(
                uuid__in=request.POST.getlist('routers[]')
            ).values_list('uuid', flat=True)]
            if not router_uuids:
                messages.warning(request, 'No routers selected')
                return redirect('router_list')
            request.session['router_selection'] = router_uuids
            return redirect('manage_router_groups_multiple')

        router_uuids = request.POST.getlist('router_uuids')
        add_group_uuid = request.POST.get('add_group')
        remove_group_uuid = request.POST.get('remove_group')

        if not router_uuids:
            messages.warning(request, 'No routers selected')
            return redirect('router_list')

        # Validate that the same group is not selected for both add and remove
        if add_group_uuid and remove_group_uuid and add_group_uuid == remove_group_uuid:
            messages.warning(request, 'Cannot add and remove from the same group')
            return redirect('router_list')

        routers = Router.objects.filter(uuid__in=router_uuids)

        # Process add to group
        if add_group_uuid:
            try:
                group = RouterGroup.objects.get(uuid=add_group_uuid)
                # One entry naming the group and the number of routers, rather than
                # one membership line each
                with collect(f'Added routers to group {group.name}'):
                    for router in routers:
                        group.routers.add(router)
                messages.success(request, f'Added {routers.count()} router(s) to group {group.name}')
            except RouterGroup.DoesNotExist:
                messages.error(request, 'Group not found')

        # Process remove from group
        if remove_group_uuid:
            try:
                group = RouterGroup.objects.get(uuid=remove_group_uuid)
                with collect(f'Removed routers from group {group.name}'):
                    for router in routers:
                        group.routers.remove(router)
                messages.success(request, f'Removed {routers.count()} router(s) from group {group.name}')
            except RouterGroup.DoesNotExist:
                messages.error(request, 'Group not found')

        request.session.pop('router_selection', None)
        return redirect('router_list')

    # GET request - display form
    router_uuids = request.session.get('router_selection') or request.GET.getlist('routers[]')
    if not router_uuids:
        messages.warning(request, 'No routers selected')
        return redirect('router_list')

    routers = Router.objects.filter(uuid__in=router_uuids)
    groups = RouterGroup.objects.all().order_by('name')

    context = {
        'routers': routers,
        'groups': groups,
        'page_title': 'Manage Router Groups',
    }

    return render(request, 'router_manager/manage_router_groups.html', context)


def _router_due_for_information_update(router_list, refresh_interval):
    """Next router that needs an information update, requested ones first."""
    router = router_list.filter(routerinformation__update_requested=True).first()
    if not router:
        router = router_list.filter(routerinformation__isnull=True).first()
    if not router:
        router = router_list.filter(routerinformation__next_retry__lt=timezone.now()).first()
    if not router:
        router = router_list.filter(routerinformation__last_retrieval__isnull=True).first()
    if not router:
        router = router_list.filter(routerinformation__last_retrieval__lt=timezone.now() - timezone.timedelta(hours=refresh_interval)).first()
    return router


def view_cron_update_router_information(request):
    data = {'status': 'success', 'updated': 0, 'failed': 0}
    refresh_interval = 24 #hours
    # Work through the queue until the next cron run is due, so that a batch of
    # requested routers is not spread over one minute per router
    deadline = time.time() + MAX_UPDATE_INFORMATION_RUNTIME

    router_list = Router.objects.filter(enabled=True).exclude(router_type='monitoring').exclude(routerstatus__status_online=False)

    while time.time() < deadline:
        router = _router_due_for_information_update(router_list, refresh_interval)
        if not router:
            break
        router_information, created = RouterInformation.objects.get_or_create(router=router)
        if router_information.update_requested:
            # Clear the request before running, so a crash does not leave it pending forever
            router_information.update_requested = False
            router_information.save(update_fields=['update_requested'])
        success, error_message = update_router_information(router_information)
        if success:
            data['updated'] += 1
        else:
            data['failed'] += 1

    if not data['updated'] and not data['failed']:
        data['message'] = 'No routers need update'

    return JsonResponse(data)
