from django.db import migrations

import routerlib.encryption


def encrypt_task_passwords(apps, schema_editor):
    ImportTask = apps.get_model('import_tool', 'ImportTask')
    for import_task in ImportTask.objects.exclude(password__isnull=True).exclude(password=''):
        import_task.password = routerlib.encryption.encrypt_value(import_task.password)
        import_task.save(update_fields=['password'])


def encrypt_csv_data(apps, schema_editor):
    CsvData = apps.get_model('import_tool', 'CsvData')
    for csv_data in CsvData.objects.all():
        import_data = csv_data.import_data
        if isinstance(import_data, list):
            for row in import_data:
                if isinstance(row, dict) and row.get('password'):
                    row['password'] = routerlib.encryption.encrypt_value(row['password'])
            csv_data.import_data = import_data
        if csv_data.raw_csv_data:
            csv_data.raw_csv_data = routerlib.encryption.mask_passwords_in_csv(csv_data.raw_csv_data)
        csv_data.save(update_fields=['import_data', 'raw_csv_data'])


class Migration(migrations.Migration):

    dependencies = [
        ('import_tool', '0007_alter_importtask_router_type'),
        ('router_manager', '0022_router_password_encrypted'),
    ]

    operations = [
        migrations.AlterField(
            model_name='importtask',
            name='password',
            field=routerlib.encryption.EncryptedTextField(blank=True, null=True),
        ),
        migrations.RunPython(encrypt_task_passwords, migrations.RunPython.noop),
        migrations.RunPython(encrypt_csv_data, migrations.RunPython.noop),
    ]
