import datetime
import re

from django.utils import timezone

from fleet_commander.models import CommandJob, CommandSchedule, CommandTask, CommandVariant
from router_manager.models import RouterInformation, RouterStatus
from routerlib.functions import connect_to_ssh

# Placeholders that can be used in the expectations of a verification
PLACEHOLDER_PATTERN = re.compile(r'\{\{\s*([a-z_]+)\s*\}\}')
# Upper limit for the recorded verification output of a single task
MAX_VERIFICATION_OUTPUT = 20000


def run_fleet_ssh_command(ssh_client, command):
    stdin, stdout, stderr = ssh_client.exec_command(command)
    exit_code = stdout.channel.recv_exit_status()
    stdout_text = stdout.read().decode("utf-8", errors="replace")
    stderr_text = stderr.read().decode("utf-8", errors="replace")
    return exit_code, stdout_text, stderr_text


def get_verification_context(router):
    """Versions that are known for a router, used to resolve the placeholders."""
    router_information = RouterInformation.objects.filter(router=router).first()
    if not router_information:
        return {'available_version': '', 'current_version': ''}
    return {
        'available_version': router_information.available_version or '',
        'current_version': router_information.os_version or router_information.model_version or '',
    }


def build_verification_expectations(variant, context):
    """The expectations of a variant, with their placeholders resolved.

    Returns a list of (as written, resolved) tuples.
    """
    expectations = []
    for line in (variant.verify_expect or '').splitlines():
        line = line.strip()
        if not line:
            continue
        resolved = PLACEHOLDER_PATTERN.sub(
            lambda match: str(context.get(match.group(1), match.group(0))), line
        )
        expectations.append((line, resolved))
    return expectations


def missing_placeholder_values(variant, context):
    """Placeholders used in the expectations that have no value for this router."""
    missing = []
    for line in (variant.verify_expect or '').splitlines():
        for name in PLACEHOLDER_PATTERN.findall(line):
            if not context.get(name) and name not in missing:
                missing.append(name)
    return missing


def expectation_matches(expectation, output):
    try:
        return re.search(expectation, output) is not None
    except re.error:
        # Not a valid regular expression, compare it as plain text
        return expectation in output


def run_variant_verification(router, variant):
    """Runs the verification commands of a variant. Returns (output, error)."""
    try:
        ssh_client = connect_to_ssh(
            router.address, router.port, router.username, router.password, router.ssh_key
        )
    except Exception as e:
        return '', f'Unable to connect: {e}'

    output_lines = []
    error = ''
    try:
        for line in (variant.verify_payload or '').strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                exit_code, stdout_text, stderr_text = run_fleet_ssh_command(ssh_client, line)
            except Exception as e:
                error = f'Connection lost while running: {line} ({e})'
                break
            if stdout_text:
                output_lines.append(stdout_text.strip())
            if exit_code != 0:
                error = stderr_text or f'Command exited with code {exit_code}'
                break
    finally:
        if ssh_client:
            ssh_client.close()

    return '\n'.join(output_lines), error


def verify_command_task(task, variant, command):
    """Checks the result of a command task against the expectations of its variant.

    Returns ('success', '') when everything matched, ('retry', message) while the
    verification may still succeed and ('failed', message) when it will not. The
    verification is repeated until it passes or until 'verify_timeout' seconds
    have passed, so a device that reboots in the middle of an update has time to
    come back online.
    """
    context = task.verify_context or {}
    expectations = build_verification_expectations(variant, context)
    if not expectations:
        return 'failed', 'Nothing to verify: the variant has no expectation defined'

    # A placeholder without a value would never match, so it is not worth waiting
    # for the timeout. This happens when a new router was never read out yet.
    missing_values = missing_placeholder_values(variant, context)
    if missing_values:
        return 'failed', (
            'Cannot verify, no value is known for '
            + ', '.join("'{{ " + name + " }}'" for name in missing_values)
            + '. Update the router information of the device first.'
        )

    if not task.verify_deadline:
        task.verify_deadline = timezone.now() + datetime.timedelta(
            seconds=max(command.verify_timeout, 0))

    output, error = run_variant_verification(task.router, variant)
    task.verification_attempts += 1
    task.verification_output = '\n'.join(filter(None, [
        task.verification_output,
        f'--- attempt {task.verification_attempts} at '
        f'{timezone.now().strftime("%Y-%m-%d %H:%M:%S")} ---',
        output,
        error,
    ]))[-MAX_VERIFICATION_OUTPUT:]

    missing = [written for (written, resolved) in expectations
               if not expectation_matches(resolved, output)]
    if not missing:
        return 'success', ''

    message = ('Verification failed: no match for '
               + ', '.join(f"'{expectation}'" for expectation in missing)
               + ' in the output of the verification commands')
    if error:
        message += f' ({error})'
    if timezone.now() >= task.verify_deadline:
        return 'failed', message
    return 'retry', message


def request_router_information_update(router):
    """Queues an information update, so the new version shows up in the UI."""
    router_information = RouterInformation.objects.filter(router=router).first()
    if router_information:
        router_information.update_requested = True
        router_information.save(update_fields=['update_requested'])


def task_aborted(task):
    """Whether the task was stopped in the web interface.

    The task is executed by the cron container, the abort comes from the web
    process, so the state has to be read from the database instead of the
    instance that is being worked on.
    """
    return CommandTask.objects.filter(pk=task.pk, status='aborted').exists()


def abort_command_task(task, user=None):
    """Stops a task that has not finished yet. Returns whether it was stopped."""
    if task.status != 'pending':
        return False

    task.status = 'aborted'
    task.next_retry = None
    task.finished_at = timezone.now()
    task.error_message = f'Aborted by {user.username}' if user else 'Aborted'
    task.save()
    return True


def execute_command_task(task):
    command = task.job.command
    router = task.router

    if task_aborted(task):
        # Stopped while it was waiting for its next run
        return

    if not router:
        task.status = 'error'
        task.error_message = 'Router no longer exists'
        task.finished_at = timezone.now()
        task.save()
        return

    router_status, _ = RouterStatus.objects.get_or_create(router=router)
    router_status.command_lock = timezone.now()
    router_status.save(update_fields=['command_lock'])

    ssh_client = None
    last_exit_code = 0
    try:
        task.started_at = task.started_at or timezone.now()
        task.status = 'pending'

        variant = task.command_variant
        if not variant:
            try:
                variant = CommandVariant.objects.get(
                    command=command, router_type=router.router_type, enabled=True
                )
                task.command_variant = variant
            except CommandVariant.DoesNotExist:
                task.status = 'error'
                task.error_message = f'No enabled variant for router type: {router.router_type}'
                task.finished_at = timezone.now()
                return

        verification_enabled = variant.has_verification

        if not task.payload_executed:
            # Only a run of the payload counts as a retry. The attempts of a
            # verification are counted separately, they are bounded by the
            # verify timeout instead of max_retry.
            task.retry_count += 1
            if task.retry_count > command.max_retry:
                task.status = 'error'
                task.error_message = task.error_message or 'Max retries reached'
                task.finished_at = timezone.now()
                task.save(update_fields=['started_at', 'status', 'retry_count', 'error_message', 'finished_at'])
                return

            task.command_payload = variant.payload
            task.verify_context = get_verification_context(router)
            task.save(update_fields=['started_at', 'status', 'retry_count', 'command_variant',
                                     'command_payload', 'verify_context'])

            ssh_client = connect_to_ssh(
                router.address, router.port, router.username, router.password, router.ssh_key
            )

            if verification_enabled:
                # The payload is executed once, a later attempt only verifies the
                # result. This keeps an update from running twice on a device
                # that reboots while it is being updated.
                task.payload_executed = True

            payload_lines = variant.payload.strip().splitlines()
            all_stdout = []

            for line in payload_lines:
                line = line.strip()
                if not line:
                    continue
                if task_aborted(task):
                    # Stopped in the meantime, the remaining commands - an
                    # install in particular - are not executed any more
                    break
                try:
                    exit_code, stdout_text, stderr_text = run_fleet_ssh_command(ssh_client, line)
                except Exception as e:
                    # An update reboots the device, which closes the connection
                    # before an exit code is returned
                    last_exit_code = -1
                    task.error_message = f'Connection lost while running: {line}'
                    all_stdout.append(f'[connection lost while running: {line}] {e}')
                    break
                last_exit_code = exit_code
                if stdout_text:
                    all_stdout.append(stdout_text)
                if exit_code != 0:
                    task.error_message = stderr_text or f'Command exited with code {exit_code}'
                    break

            executed_commands = '\n'.join(
                line.strip() for line in payload_lines if line.strip()
            )
            task.command_executed = executed_commands

            if command.capture_output or verification_enabled:
                # For a command with verification the output is always kept, it is
                # the only record of what the router answered
                task.command_output = '\n'.join(all_stdout)

        if task_aborted(task):
            pass
        elif verification_enabled:
            state, message = verify_command_task(task, variant, command)
            if state == 'success':
                task.status = 'success'
                task.verified = True
                task.error_message = None
                task.next_retry = None
                task.finished_at = timezone.now()
                request_router_information_update(router)
            elif state == 'retry':
                # Only the verification runs again, until verify_timeout has passed
                task.next_retry = timezone.now() + datetime.timedelta(
                    seconds=max(command.verify_interval, 5))
            else:
                task.status = 'error'
                task.error_message = message
                task.next_retry = None
                task.finished_at = timezone.now()
        elif last_exit_code == 0 and not task.error_message:
            task.status = 'success'
            task.next_retry = None
            task.finished_at = timezone.now()
        else:
            handle_task_retry(task, command)

    except Exception as e:
        task.error_message = str(e)
        handle_task_retry(task, command)
    finally:
        if ssh_client:
            ssh_client.close()

        # Clear command lock
        router_status.command_lock = None
        router_status.save(update_fields=['command_lock'])

        if not task_aborted(task):
            task.save()
            check_job_completion(task.job)


def handle_task_retry(task, command):
    # retry_count was already incremented for the attempt that just failed, so
    # max_retry attempts have been made once it reaches the configured number
    if task.retry_count >= command.max_retry:
        task.status = 'error'
        task.next_retry = None
        task.finished_at = timezone.now()
    else:
        task.next_retry = timezone.now() + datetime.timedelta(seconds=command.retry_interval)


def check_job_completion(job):
    pending_count = job.tasks.filter(status='pending').count()
    if pending_count == 0:
        job.completed = timezone.now()
        job.save(update_fields=['completed'])


def create_jobs_from_schedules():
    now = timezone.now()
    data = {'jobs_created': 0, 'tasks_created': 0}

    due_schedules = CommandSchedule.objects.filter(
        enabled=True, command__enabled=True, next_run__lte=now
    ).select_related('command')

    for schedule in due_schedules:
        schedule.disable_if_invalid()
        if not schedule.enabled:
            continue

        routers = set(schedule.router.filter(enabled=True))
        for group in schedule.router_group.all():
            routers.update(group.routers.filter(enabled=True))

        if not routers:
            schedule.last_run = now
            schedule.save(update_fields=['last_run'])
            schedule.update_next_run()
            continue

        job = CommandJob.objects.create(
            command=schedule.command,
            exec_source='schedule',
        )
        data['jobs_created'] += 1

        for router in routers:
            variant = CommandVariant.objects.filter(
                command=schedule.command, router_type=router.router_type, enabled=True
            ).first()

            CommandTask.objects.create(
                job=job,
                command_variant=variant,
                router=router,
                command_payload=variant.payload if variant else '',
            )
            data['tasks_created'] += 1

        schedule.last_run = now
        schedule.save(update_fields=['last_run'])
        schedule.update_next_run()

    return data


def create_manual_job(command, routers=None, router_groups=None, user=None):
    all_routers = set()
    if routers:
        all_routers.update(routers.filter(enabled=True))
    if router_groups:
        for group in router_groups:
            all_routers.update(group.routers.filter(enabled=True))

    if not all_routers:
        return None

    job = CommandJob.objects.create(
        command=command,
        exec_source='manual',
        user_source=user,
        user_source_name=user.username if user else None,
    )

    for router in all_routers:
        variant = CommandVariant.objects.filter(
            command=command, router_type=router.router_type, enabled=True
        ).first()

        CommandTask.objects.create(
            job=job,
            command_variant=variant,
            router=router,
            command_payload=variant.payload if variant else '',
        )
    return job
