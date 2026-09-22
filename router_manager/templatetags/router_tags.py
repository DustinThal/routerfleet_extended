import re

from django import template

from router_manager.models import Router, RouterInformation

register = template.Library()

# The number part of a version. '7.16.2 (stable)' of RouterOS, the kernel
# '4.14.151' of an airOS device and 'OpenWrt 24.10.3 r28714-00d1fe2b72' all
# carry it at the beginning, followed by something that is not comparable
VERSION_PATTERN = re.compile(r'\d+(?:\.\d+)*')

# Version strings that are only known after the router information was read
NEUTRAL_STATE = {'css_class': '', 'title': ''}


def version_key(value):
    """The comparable numbers of a version string, None without any."""
    match = VERSION_PATTERN.search(value or '')
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split('.'))


def compare_versions(left, right):
    """-1 when left is the older one, 0 when both are the same, 1 when newer.

    None when one of them carries no version at all. A version without the
    patch level counts as that patch level: 7.16 and 7.16.0 are the same.
    """
    left_key = version_key(left)
    right_key = version_key(right)
    if left_key is None or right_key is None:
        return None

    length = max(len(left_key), len(right_key))
    left_key += (0,) * (length - len(left_key))
    right_key += (0,) * (length - len(right_key))
    return (left_key > right_key) - (left_key < right_key)


@register.filter
def firmware_state(router_information):
    """Colour and explanation for the firmware of a router.

    The RouterBOOT firmware has to be on the version of the OS, a firmware that
    stayed behind is what keeps a device from booting into a new OS.
    """
    firmware = getattr(router_information, 'firmware_version', '') or ''
    os_version = getattr(router_information, 'os_version', '') or ''

    comparison = compare_versions(firmware, os_version)
    if comparison is None:
        # Nothing to compare, one of the two was never read out
        return NEUTRAL_STATE
    if comparison == 0:
        return {'css_class': 'text-success',
                'title': f'Firmware matches the OS version {os_version}'}
    if comparison < 0:
        return {'css_class': 'text-danger',
                'title': f'Firmware {firmware} is behind the OS version {os_version}'}
    # Newer than the OS, there is nothing to update
    return NEUTRAL_STATE


@register.filter
def os_version_state(router_information):
    """Colour and explanation for the OS version of a router.

    Green while the version the router offers is the one it runs, red while an
    update is waiting, yellow when RouterFleet has no version to compare with -
    that is only known after the information of the device was read out.
    """
    os_version = getattr(router_information, 'os_version', '') or ''
    available_version = getattr(router_information, 'available_version', '') or ''

    if not os_version:
        return NEUTRAL_STATE
    if not available_version:
        return {'css_class': 'text-warning',
                'title': 'No version known the router could update to. Update the router information.'}

    comparison = compare_versions(os_version, available_version)
    if comparison is None:
        # An offered version without a number in it can not be compared
        return NEUTRAL_STATE
    if comparison == 0:
        return {'css_class': 'text-success', 'title': f'Up to date with {available_version}'}
    if comparison < 0:
        return {'css_class': 'text-danger',
                'title': f'{available_version} is available, the router runs {os_version}'}
    # Ahead of what the channel offers, there is nothing to update
    return NEUTRAL_STATE


def update_state_counts():
    """How many devices are up to date, are missing an update and have no
    information at all.

    The very rule os_version_state colours the router list by, so the overview
    and the list can not tell two different stories. Devices that are only
    monitored stay out: they carry no version at all and the list shows none
    for them either.
    """
    information_of_router = {
        information.router_id: information
        for information in RouterInformation.objects.exclude(router__router_type='monitoring')
    }
    counts = {'up_to_date': 0, 'update_waiting': 0, 'no_information': 0}

    router_ids = Router.objects.exclude(router_type='monitoring').values_list('id', flat=True)
    for router_id in router_ids:
        state = os_version_state(information_of_router.get(router_id))
        if state['css_class'] == 'text-success':
            counts['up_to_date'] += 1
        elif state['css_class'] == 'text-danger':
            counts['update_waiting'] += 1
        else:
            # Nothing was read out of the device, or there is no version to
            # compare the one it runs with
            counts['no_information'] += 1

    return counts
