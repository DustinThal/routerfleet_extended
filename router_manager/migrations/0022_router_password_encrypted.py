from django.db import migrations

import routerlib.encryption


def encrypt_existing_passwords(apps, schema_editor):
    Router = apps.get_model('router_manager', 'Router')
    for router in Router.objects.exclude(password__isnull=True).exclude(password=''):
        router.password = routerlib.encryption.encrypt_value(router.password)
        router.save(update_fields=['password'])


class Migration(migrations.Migration):

    dependencies = [
        ('router_manager', '0021_routerstatus_command_lock'),
    ]

    operations = [
        migrations.AlterField(
            model_name='router',
            name='password',
            field=routerlib.encryption.EncryptedTextField(blank=True, null=True),
        ),
        migrations.RunPython(encrypt_existing_passwords, migrations.RunPython.noop),
    ]
