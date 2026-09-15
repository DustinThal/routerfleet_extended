from django.db import migrations

# The expectation the update commands were shipped with until now: it compares
# the installed version with the version RouterFleet knows for the router.
SHIPPED_VERIFY_EXPECT = 'installed-version: {{ available_version }}'

VERIFY_EXPECT = 'installed-version: {{ expected_version }}'

# The verification printed the version with an equal sign, while the router
# writes its own output as 'installed-version: 7.17.3'. The expectation reads
# like the router writes it, so the line is written the same way
SHIPPED_VERSION_LINE = ':put ("installed-version=" . [/system/resource/get version])'
VERSION_LINE = ':put ("installed-version: " . [/system/resource/get version])'

# Lets the router report the version it offers on the channel that was just set
EXPECTED_VERSION_LINE = ':put ("expected-version=" . [/system/package/update/get latest-version])'

CHECK_FOR_UPDATES_LINE = '/system/package/update/check-for-updates'
DELAY_LINE = ':delay 5s'


def is_install_line(line):
    """Matches '/system/package/update/install' and the spaced spelling of it."""
    words = line.strip().replace('/', ' ').split()
    return len(words) >= 2 and words[-2] == 'update' and words[-1] == 'install'


def insert_marker(payload):
    """Makes the payload report the version the router is going to install."""
    lines = payload.splitlines()
    if any('expected-version=' in line for line in lines):
        return payload

    # Reported right before the install, so it is the version the install is
    # going to work with
    install_index = next(
        (index for index, line in enumerate(lines) if is_install_line(line)),
        len(lines),
    )
    if not any('check-for-updates' in line for line in lines):
        # The version is only known once the router has checked for updates, and
        # that check runs in the background on the router
        lines[install_index:install_index] = [CHECK_FOR_UPDATES_LINE, DELAY_LINE]
        install_index += 2

    lines.insert(install_index, EXPECTED_VERSION_LINE)
    return '\n'.join(lines)


def verify_update_against_router_version(apps, schema_editor):
    """Lets the existing update commands check the version the router offers.

    The payload of an update command sets the update channel. Which version gets
    installed depends on that channel, but the expectation was filled with the
    version RouterFleet read out before the channel was changed. As soon as the
    channels differ, that expectation can never match and an update that worked
    is reported as failed. The router is asked for the version instead: the
    payload reports it and the verification expects it.
    """
    CommandVariant = apps.get_model('fleet_commander', 'CommandVariant')

    variants = CommandVariant.objects.filter(
        router_type__in=['routeros', 'routeros-branded'],
        payload__contains='/system/package/update',
        verify_expect__contains=SHIPPED_VERIFY_EXPECT,
    )

    for variant in variants:
        if not any(is_install_line(line) for line in variant.payload.splitlines()):
            # Without an install there is no version to check the payload against
            continue

        variant.payload = insert_marker(variant.payload)
        # Only the shipped expectation is exchanged, anything the variant adds
        # around it is kept
        variant.verify_expect = variant.verify_expect.replace(SHIPPED_VERIFY_EXPECT, VERIFY_EXPECT)
        if variant.verify_payload and SHIPPED_VERSION_LINE in variant.verify_payload:
            variant.verify_payload = variant.verify_payload.replace(SHIPPED_VERSION_LINE, VERSION_LINE)
        variant.save(update_fields=['payload', 'verify_payload', 'verify_expect'])


class Migration(migrations.Migration):

    dependencies = [
        ('fleet_commander', '0006_verify_update_command_result'),
    ]

    operations = [
        migrations.RunPython(verify_update_against_router_version, migrations.RunPython.noop),
    ]
