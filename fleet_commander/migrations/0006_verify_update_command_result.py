from django.db import migrations

# The payload the update commands were shipped with until now
SHIPPED_PAYLOAD_MARKERS = ('update/set channel=long-term', 'update/install')

UPDATE_PAYLOAD = ('/system/package/update/set channel=long-term\n'
                  '/system/package/update/check-for-updates\n'
                  ':delay 5s\n'
                  '/system/package/update/download\n'
                  '/system/package/update/install')

VERIFY_PAYLOAD = ('/system/package/update/print\n'
                  ':put ("installed-version=" . [/system/resource/get version])')

VERIFY_EXPECT = 'installed-version: {{ available_version }}'


def add_verification_to_update_commands(apps, schema_editor):
    """Lets the existing update commands check the result on the router.

    Commands that were created before the verification existed would keep
    reporting success without checking anything, so the variants that update a
    RouterOS device get the verification commands to confirm the installed
    version afterwards.
    """
    CommandVariant = apps.get_model('fleet_commander', 'CommandVariant')

    variants = CommandVariant.objects.filter(
        router_type__in=['routeros', 'routeros-branded'],
        payload__contains='/system/package/update',
    )

    for variant in variants:
        if (variant.verify_payload or '').strip():
            # Somebody already defined a verification for this command
            continue

        # Only touch the payload when it is still the one that was shipped
        shipped_payload = all(
            marker in variant.payload for marker in SHIPPED_PAYLOAD_MARKERS
        ) and 'update/check-for-updates' not in variant.payload
        if shipped_payload:
            variant.payload = UPDATE_PAYLOAD

        variant.verify_payload = VERIFY_PAYLOAD
        variant.verify_expect = VERIFY_EXPECT
        variant.save(update_fields=['payload', 'verify_payload', 'verify_expect'])

        command = variant.command
        if command.verify_timeout == 300:
            # A device that reboots while it is being updated needs time to come
            # back online before the result can be read out
            command.verify_timeout = 600
            command.save(update_fields=['verify_timeout'])


class Migration(migrations.Migration):

    dependencies = [
        ('fleet_commander', '0005_command_verify_interval_command_verify_timeout_and_more'),
    ]

    operations = [
        migrations.RunPython(add_verification_to_update_commands, migrations.RunPython.noop),
    ]
