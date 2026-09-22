from django.db import migrations


def backfill_config_change_detected(apps, schema_editor):
    """Fill the flag for the backups that were written before it was filled.

    The rule is the one RouterBackup.save() applies: a backup that brings a
    configuration and that differs from the configuration seen for the same
    device before it is a change. The first configuration seen of a device is no
    change — it has nothing to be compared with — it is only the one the later
    backups are measured against.
    """
    RouterBackup = apps.get_model('backup_data', 'RouterBackup')
    last_hash_of_router = {}
    changed_backup_ids = []
    backups = RouterBackup.objects.exclude(backup_text_hash='').order_by(
        'router_id', 'created', 'id').values_list('id', 'router_id', 'backup_text_hash')
    for backup_id, router_id, backup_text_hash in backups.iterator():
        previous_hash = last_hash_of_router.get(router_id)
        if previous_hash is not None and previous_hash != backup_text_hash:
            changed_backup_ids.append(backup_id)
        last_hash_of_router[router_id] = backup_text_hash

    # In chunks: a fleet with years of backups has a lot of them
    for start in range(0, len(changed_backup_ids), 500):
        RouterBackup.objects.filter(id__in=changed_backup_ids[start:start + 500]).update(
            config_change_detected=True)


def forget_config_change_detected(apps, schema_editor):
    RouterBackup = apps.get_model('backup_data', 'RouterBackup')
    RouterBackup.objects.filter(config_change_detected=True).update(config_change_detected=False)


class Migration(migrations.Migration):

    dependencies = [
        ('backup_data', '0011_routerbackup_task_lock'),
    ]

    operations = [
        migrations.RunPython(backfill_config_change_detected, forget_config_change_detected),
    ]
