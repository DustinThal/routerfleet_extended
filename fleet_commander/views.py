import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, ProtectedError
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from router_manager.models import Router
from user_manager.models import UserAcl
from .command_functions import build_verification_expectations, create_jobs_from_schedules, \
    execute_command_task, create_manual_job, abort_command_task
from .forms import CommandForm, CommandVariantForm, CommandScheduleForm, CommandExecuteForm
from .models import Command, CommandVariant, CommandSchedule, CommandJob, CommandTask


def create_default_commands():
    new_command = Command.objects.create(
        name='Show Version',
        description = "Collects and displays version information for your routers. This command is commonly used to gather details about the router model, software version, uptime, and hardware.",
        enabled=True,
        capture_output=True,
        max_retry=3,
        retry_interval=30,
    )
    for router_type in ['routeros', 'routeros-branded']:
        CommandVariant.objects.create(
            command=new_command, router_type=router_type, enabled=True,
            payload='/system resource print\n/system package print'
        )
    CommandVariant.objects.create(
        command=new_command, router_type='openwrt', enabled=True,
        payload='uname -a\ncat /etc/openwrt_release\nubus call system board'
    )
    CommandVariant.objects.create(
        command=new_command, router_type='ubiquiti-airos', enabled=True,
        payload='uname -a\ncat /etc/version\ncat /proc/uptime'
    )

    new_command = Command.objects.create(
        name='Update Router Firmware',
        description = "Updates the router's firmware to the latest version. This command is essential for maintaining security and performance by ensuring that your routers are running the most recent software.",
        enabled=True,
        capture_output=True,
        max_retry=3,
        retry_interval=30,
        verify_timeout=600,
        verify_interval=30,
    )
    for router_type in ['routeros', 'routeros-branded']:
        CommandVariant.objects.create(
            command=new_command, router_type=router_type, enabled=True,
            # The download is a step of its own, so a missing update file is
            # visible in the output instead of only failing the install
            payload='/system/package/update/set channel=long-term\n'
                    '/system/package/update/check-for-updates\n'
                    ':delay 5s\n'
                    # The version to install depends on the channel that was just
                    # set, so the router has to report it: the version known to
                    # RouterFleet belongs to the channel from before
                    ':put ("expected-version=" . [/system/package/update/get latest-version])\n'
                    '/system/package/update/download\n'
                    '/system/package/update/install',
            # The install reboots the router, so the only reliable proof is the
            # version it reports once it is back online. The print and the
            # explicit line both write the version the same way, so the
            # expectation matches either of them
            verify_payload='/system/package/update/print\n'
                           ':put ("installed-version: " . [/system/resource/get version])',
            verify_expect='installed-version: {{ expected_version }}',
        )
    return


@login_required()
def view_command_list(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    command_list = Command.objects.all().order_by('name')
    if not command_list:
        create_default_commands()
        messages.success(request, 'Default commands created successfully')
        command_list = Command.objects.all().order_by('name')

    context = {
        'command_list': command_list,
        'page_title': 'Fleet Commander',
    }
    return render(request, 'fleet_commander/command_list.html', context)


@login_required()
def view_command_details(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    command = get_object_or_404(Command, uuid=request.GET.get('uuid'))
    context = {
        'command': command,
        'variant_list': command.variants.all().order_by('router_type'),
        'schedule_list': command.schedules.all().order_by('-created'),
        'page_title': command.name,
    }
    return render(request, 'fleet_commander/command_details.html', context)


@login_required()
def view_manage_command(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=40).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})

    if request.GET.get('uuid'):
        command = get_object_or_404(Command, uuid=request.GET.get('uuid'))
        if request.GET.get('action') == 'delete':
            if request.GET.get('confirmation') == 'delete':
                try:
                    command.delete()
                    messages.success(request, 'Command deleted successfully')
                except ProtectedError:
                    messages.warning(request, 'Cannot delete command because it is referenced by existing variants, schedules, or jobs.')
                    return redirect(f'/fleet_commander/command/details/?uuid={command.uuid}')
            else:
                messages.warning(request, 'Command not deleted|Invalid confirmation')
                return redirect(f'/fleet_commander/command/details/?uuid={command.uuid}')
            return redirect('/fleet_commander/')
    else:
        command = None

    form = CommandForm(request.POST or None, instance=command)
    if form.is_valid():
        saved = form.save()
        messages.success(request, 'Command saved successfully')
        if command:
            return redirect(f'/fleet_commander/command/details/?uuid={saved.uuid}')
        else:
            return redirect('/fleet_commander/')

    form_description_content = '''
    <strong>Enabled</strong>
    <p>Enable or disable the command. Disabled commands cannot be executed.</p>
    
    <strong>Capture Output</strong>
    <p>If unchecked, RouterFleet executes the command and immediately closes the connection. If checked, RouterFleet will wait for the command to finish to capture its output. Wait times can be long and may delay the execution queue if you have many commands pending. <strong>Only check this if you really need the output.</strong> The output of a command with a verification is always captured, it is what the verification is checked against.</p>
    
    <strong>Max Retry and Retry Interval</strong>
    <p>Maximum number of retries if the command fails, and the interval (in seconds) between each attempt.</p>

    <strong>Verification</strong>
    <p>The result of a command can be verified instead of trusting its exit code. The verification commands and
    their expected result are defined per variant (router type). A task is only successful when the device
    answers what the variant expects. This is the reliable way to check an update: a router that reboots while
    it is being updated never returns an exit code at all.</p>

    <strong>Verify Timeout and Verify Interval</strong>
    <p>How long (in seconds) RouterFleet keeps verifying a command, and how long it waits between two attempts.
    The payload is only executed once, a later attempt only verifies again, so a device that reboots is not
    updated twice. The timeout has to be long enough for the device to come back online.</p>
    '''

    context = {
        'form': form,
        'page_title': 'Manage Command',
        'instance': command,
        'form_description': {
            'size': '',
            'content': form_description_content
        },
    }
    return render(request, 'generic_form.html', context)


@login_required()
def view_execute_command(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=40).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})

    command = get_object_or_404(Command, uuid=request.GET.get('command_uuid'), enabled=True)
    if not command.can_execute:
        messages.warning(request, 'No variants defined for this command. Please create a variant before executing.')
        return redirect(f'/fleet_commander/command/details/?uuid={command.uuid}')
    form = CommandExecuteForm(request.POST or None, command=command)

    if form.is_valid():
        job = create_manual_job(
            command,
            routers=form.cleaned_data['routers'],
            router_groups=form.cleaned_data['router_groups'],
            user=request.user
        )
        if job:
            messages.success(request, f'Job created for {job.tasks.count()} targets')
            return redirect(f'/fleet_commander/job/details/?uuid={job.uuid}')
        else:
            messages.warning(request, 'No targets selected or found enabled')

    context = {
        'form': form,
        'page_title': f'Execute Command: {command.name}',
        'command': command,
    }
    return render(request, 'generic_form.html', context)


@login_required()
def view_run_command_multiple(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=40).exists():
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
            return redirect('fleet_commander_execute_multiple')

        router_uuids = request.POST.getlist('router_uuids')
        command_uuid = request.POST.get('command')

        if not router_uuids:
            messages.warning(request, 'No routers selected')
            return redirect('router_list')

        if not command_uuid:
            messages.warning(request, 'No command selected')
            return redirect('router_list')

        command = get_object_or_404(Command, uuid=command_uuid, enabled=True)
        routers = Router.objects.filter(uuid__in=router_uuids)

        if not command.can_execute:
            messages.warning(request, 'No variants defined for this command. Please create a variant before executing.')
            return redirect('router_list')

        job = create_manual_job(
            command,
            routers=routers,
            router_groups=None,
            user=request.user
        )
        request.session.pop('router_selection', None)
        if job:
            messages.success(request, f'Job created for {job.tasks.count()} targets')
            return redirect(f'/fleet_commander/job/details/?uuid={job.uuid}')
        else:
            messages.warning(request, 'No targets selected or found enabled')
            return redirect('router_list')

    # GET request - display form
    router_uuids = request.session.get('router_selection') or request.GET.getlist('routers[]')
    if not router_uuids:
        messages.warning(request, 'No routers selected')
        return redirect('router_list')

    routers = Router.objects.filter(uuid__in=router_uuids)
    commands = Command.objects.filter(enabled=True).order_by('name')

    context = {
        'routers': routers,
        'commands': commands,
        'page_title': 'Run Command on Multiple Routers',
    }

    return render(request, 'fleet_commander/run_command_multiple.html', context)


@login_required()
def view_manage_command_variant(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=40).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})

    if request.GET.get('uuid'):
        variant = get_object_or_404(CommandVariant, uuid=request.GET.get('uuid'))
        command = variant.command
        if request.GET.get('action') == 'delete':
            if request.GET.get('confirmation') == 'delete':
                variant.delete()
                messages.success(request, 'Variant deleted successfully')
            else:
                messages.warning(request, 'Variant not deleted|Invalid confirmation')
            return redirect(f'/fleet_commander/command/details/?uuid={command.uuid}')
    else:
        variant = None
        command = get_object_or_404(Command, uuid=request.GET.get('command_uuid'))

    form = CommandVariantForm(request.POST or None, instance=variant, command=command)
    if form.is_valid():
        form.save()
        messages.success(request, 'Variant saved successfully')
        return redirect(f'/fleet_commander/command/details/?uuid={command.uuid}')

    form_description_content = '''
    <strong>Payload</strong>
    <p>The commands for this router type, one command per line. They are executed in order and the execution
    stops at the first command that fails.</p>

    <strong>Verification Commands</strong>
    <p>Optional. Commands that check the result of the payload, one command per line. They are executed after
    the payload, on a new connection. Leave this empty to keep the exit code of the payload as the result of
    the task.</p>

    <strong>Expected Result</strong>
    <p>One expectation per line. Every line has to be found in the output of the verification commands,
    otherwise the task fails. This is how an update is checked: for example the version the router reports has
    to be the version that was offered for it. Regular expressions are supported.</p>
    <p>The placeholders <code>{{ available_version }}</code> (the version RouterFleet found as an update for
    this router) and <code>{{ current_version }}</code> (the version the router was running before the payload)
    are replaced with the values known for the router.</p>
    <p><code>{{ expected_version }}</code> is the version the <strong>router</strong> offers. It is taken from
    a line the payload has to print, for example
    <code>:put ("expected-version=" . [/system/package/update/get latest-version])</code>. Use it for a payload
    that sets the update channel: which version gets installed depends on that channel, while
    <code>{{ available_version }}</code> is the version RouterFleet read out on the channel the router had
    before, so the two differ as soon as the payload changes the channel.</p>
    <p>While the verification does not match, the task is retried and only the verification runs again, until
    the verify timeout of the command has passed. That gives a router that reboots during an update the time
    to come back online.</p>
    '''

    context = {
        'form': form,
        'page_title': 'Manage Variant',
        'instance': variant,
        'form_description': {
            'size': '',
            'content': form_description_content
        },
    }
    return render(request, 'generic_form.html', context)


@login_required()
def view_manage_command_schedule(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=40).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})

    if request.GET.get('uuid'):
        schedule = get_object_or_404(CommandSchedule, uuid=request.GET.get('uuid'))
        command = schedule.command
        if request.GET.get('action') == 'delete':
            if request.GET.get('confirmation') == 'delete':
                schedule.delete()
                messages.success(request, 'Schedule deleted successfully')
            else:
                messages.warning(request, 'Schedule not deleted|Invalid confirmation')
            return redirect(f'/fleet_commander/command/details/?uuid={command.uuid}')
    else:
        schedule = None
        command = get_object_or_404(Command, uuid=request.GET.get('command_uuid'))

    form = CommandScheduleForm(request.POST or None, instance=schedule, command=command)
    if form.is_valid():
        saved = form.save()
        saved.update_next_run()
        messages.success(request, 'Schedule saved successfully')
        return redirect(f'/fleet_commander/command/details/?uuid={command.uuid}')

    form_description_content = '''
    <strong>Start At</strong>
    <p>The designated date and time when the schedule should begin execution. All schedules require a starting point.</p>
    
    <strong>End At</strong>
    <p>Optional. If set, the schedule will not execute after this date and time.</p>
    
    <strong>Repeat Interval</strong>
    <p>How frequently the command should run. You must specify a letter indicating the unit of time:</p>
    <ul>
        <li><strong>d</strong> for days (e.g., <code>1d</code>, <code>30d</code>)</li>
        <li><strong>h</strong> for hours (e.g., <code>2h</code>, <code>24h</code>)</li>
        <li><strong>m</strong> for minutes (e.g., <code>30m</code>, <code>90m</code>)</li>
    </ul>
    <p>Leave it empty or set to <code>0</code> for a one-time execution.</p>
    
    <strong>Target</strong>
    <p>You must select at least one router or one router group to execute the scheduled command.</p>
    '''

    context = {
        'form': form,
        'page_title': 'Manage Schedule',
        'instance': schedule,
        'form_description': {
            'size': '',
            'content': form_description_content
        },
    }
    return render(request, 'generic_form.html', context)


@login_required()
def view_job_list(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    context = {
        'job_list': CommandJob.objects.all().select_related('command').order_by('-created'),
        'page_title': 'Job History',
    }
    return render(request, 'fleet_commander/job_list.html', context)


@login_required()
def view_job_details(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    job = get_object_or_404(CommandJob, uuid=request.GET.get('uuid'))
    context = {
        'job': job,
        'task_list': job.tasks.all().order_by('-created'),
        'page_title': f'Job: {job.command.name}',
    }
    return render(request, 'fleet_commander/job_details.html', context)


@login_required()
def view_task_details(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    task = get_object_or_404(CommandTask, uuid=request.GET.get('uuid'))
    verification_expectations = []
    if task.command_variant:
        verification_expectations = build_verification_expectations(
            task.command_variant, task.verify_context or {}
        )
    context = {
        'task': task,
        'verification_expectations': verification_expectations,
        'page_title': f'Task: {task.router_name or task.router_uuid}',
    }
    return render(request, 'fleet_commander/task_details.html', context)


ABORT_CONFIRMATION = 'abort'


def abort_confirmed(request):
    return request.GET.get('confirmation') == ABORT_CONFIRMATION


@login_required()
def view_abort_command_task(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    task = get_object_or_404(CommandTask, uuid=request.GET.get('uuid'))
    job = task.job

    if not abort_confirmed(request):
        messages.warning(request, 'Task not stopped|Invalid confirmation')
        return redirect(f'/fleet_commander/task/details/?uuid={task.uuid}')

    if abort_command_task(task, request.user):
        messages.success(request, f'Task for {task.router_name or task.router_uuid} stopped')
    else:
        messages.warning(request, 'Task not stopped|Only a task that has not finished yet can be stopped')

    return redirect(f'/fleet_commander/job/details/?uuid={job.uuid}')


@login_required()
def view_abort_command_job(request):
    if not UserAcl.objects.filter(user=request.user, user_level__gte=20).exists():
        return render(request, 'access_denied.html', {'page_title': 'Access Denied'})
    job = get_object_or_404(CommandJob, uuid=request.GET.get('uuid'))

    if not abort_confirmed(request):
        messages.warning(request, 'Job not stopped|Invalid confirmation')
        return redirect(f'/fleet_commander/job/details/?uuid={job.uuid}')

    stopped = 0
    for task in job.tasks.filter(status='pending'):
        if abort_command_task(task, request.user):
            stopped += 1

    if stopped:
        messages.success(request, f'Job stopped|{stopped} pending tasks of this job were aborted')
    else:
        messages.warning(request, 'Job not stopped|This job has no pending tasks left')

    return redirect(f'/fleet_commander/job/details/?uuid={job.uuid}')


def view_cron_create_command_jobs(request):
    data = create_jobs_from_schedules()
    return JsonResponse(data)


def view_cron_perform_command_tasks(request):
    data = {'tasks_performed': 0}
    max_execution_time = 45
    execution_start_time = timezone.now()

    pending_tasks = CommandTask.objects.filter(
        status='pending',
        router__routerstatus__command_lock__isnull=True, router__routerstatus__backup_lock__isnull=True
    ).filter(
        Q(next_retry__isnull=True) | Q(next_retry__lte=timezone.now())
    ).select_related('job__command', 'router', 'command_variant')

    for task in pending_tasks:
        execute_command_task(task)
        data['tasks_performed'] += 1

        if timezone.now() - execution_start_time > datetime.timedelta(seconds=max_execution_time):
            break

    return JsonResponse(data)
