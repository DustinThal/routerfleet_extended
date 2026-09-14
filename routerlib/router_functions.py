import json
import datetime
import logging
import re
import time
import requests
from django.utils import timezone
from router_manager.models import RouterInformation, Router
from routerlib.functions import connect_to_ssh

logger = logging.getLogger(__name__)

ROUTEROS_UPDATE_CHECK_DELAY = 5  # seconds to wait for the router to reach MikroTik's update server

OPENWRT_VERSIONS_URL = 'https://downloads.openwrt.org/.versions.json'
OPENWRT_TARGET_URL = 'https://downloads.openwrt.org/releases/{version}/targets/{board}/profiles.json'
OPENWRT_RELEASE_CACHE_TTL = 6 * 60 * 60  # seconds

_openwrt_release_cache = {'time': 0, 'stable': '', 'oldstable': ''}
_openwrt_target_cache = {}


def _parse_routeros_key_value_output(output: str) -> dict:
    """
    Parse lines like "key:    value" into a dict.
    Skip blank lines or lines starting with '[' (the prompt).
    """
    data = {}
    # Normalize and split
    for raw_line in output.replace('\r', '').splitlines():
        line = raw_line.strip()
        # skip empty or prompt lines
        if not line or line.startswith('['):
            continue
        if ':' not in line:
            continue
        key, val = line.split(':', 1)
        data[key.strip()] = val.strip()
    return data


def _get_routeros_update_info(ssh) -> dict:
    """
    Ask the router to check MikroTik's update server and read the result back.

    The check runs in the background on the router, so the result only shows up
    a few seconds after the command has been issued.
    """
    ssh.exec_command('/system package update check-for-updates')
    time.sleep(ROUTEROS_UPDATE_CHECK_DELAY)
    stdin, stdout, stderr = ssh.exec_command('/system package update print')
    return _parse_routeros_key_value_output(stdout.read().decode('utf-8', errors='ignore'))


def _parse_routeros_update_info(update_info: dict, resource_info: dict) -> tuple:
    """Return (available_version, update_available) for a RouterOS device."""
    latest_version = update_info.get('latest-version', '')
    installed_version = update_info.get('installed-version', '') or resource_info.get('version', '')
    update_available = bool(latest_version and installed_version and latest_version != installed_version)
    return latest_version, update_available


def _openwrt_release_versions() -> tuple:
    """
    Return the (stable, oldstable) OpenWrt release versions.

    Cached for a few hours so that a large fleet does not query the download
    server once per router.
    """
    if time.time() - _openwrt_release_cache['time'] > OPENWRT_RELEASE_CACHE_TTL:
        # Remember the attempt even when it fails, so an unreachable download
        # server is not retried for every single router
        _openwrt_release_cache['time'] = time.time()
        try:
            response = requests.get(OPENWRT_VERSIONS_URL, timeout=15)
            response.raise_for_status()
            versions = response.json()
            _openwrt_release_cache['stable'] = versions.get('stable_version', '')
            _openwrt_release_cache['oldstable'] = versions.get('oldstable_version', '')
        except Exception as e:
            logger.warning(f'Could not retrieve the OpenWrt release list: {e}')
    return _openwrt_release_cache['stable'], _openwrt_release_cache['oldstable']


def _openwrt_target_available(version: str, board: str) -> bool:
    """Whether a release still ships images for the given OpenWrt board."""
    cache_key = (version, board)
    if cache_key not in _openwrt_target_cache:
        available = False
        try:
            response = requests.get(
                OPENWRT_TARGET_URL.format(version=version, board=board), timeout=15, stream=True
            )
            available = response.status_code == 200
            response.close()
        except Exception as e:
            logger.warning(f'Could not check OpenWrt release "{version}" for board "{board}": {e}')
        _openwrt_target_cache[cache_key] = available
    return _openwrt_target_cache[cache_key]


def _version_key(version: str) -> tuple:
    """Comparable key for release numbers such as '24.10.8'."""
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r'[.\-]', version or ''))


def get_openwrt_available_version(os_release: dict) -> tuple:
    """
    Return (available_version, update_available) for an OpenWrt device.

    Only releases newer than the installed one and that still ship images for
    the device's board are offered.
    """
    installed_version = os_release.get('VERSION_ID', '')
    board = os_release.get('OPENWRT_BOARD', '')
    if not installed_version or not board:
        return '', False

    stable_version, oldstable_version = _openwrt_release_versions()
    installed_key = _version_key(installed_version)
    for version in (stable_version, oldstable_version):
        if not version or _version_key(version) <= installed_key:
            continue
        if _openwrt_target_available(version, board):
            return version, True
    return '', False


def get_router_information(router_information: RouterInformation):
    """
    Connect to the router, retrieve info, and store it in RouterInformation.
    """
    router = router_information.router
    field_max_length = 100
    success = False
    error_message = ''

    try:
        ssh = connect_to_ssh(router.address, router.port, router.username, router.password, router.ssh_key)
        json_data = {}

        if router.router_type in ('routeros', 'routeros-branded'):
            for cmd in ['/system resource print', '/system routerboard print']:
                stdin, stdout, stderr = ssh.exec_command(cmd)
                raw = stdout.read().decode('utf-8', errors='ignore')
                parsed = _parse_routeros_key_value_output(raw)
                json_data[cmd] = parsed

            rb = json_data['/system routerboard print']
            sr = json_data['/system resource print']

            if sr:
                router_information.model_name = sr.get('board-name', '')[:field_max_length]
                router_information.os_version = sr.get('version', '')[:field_max_length]
                router_information.architecture = sr.get('architecture-name', '')[:field_max_length]
                router_information.cpu = sr.get('cpu', '')[:field_max_length]
                success = True
            if rb:
                router_information.model_version = rb.get('model', '')[:field_max_length]
                router_information.serial_number = rb.get('serial-number', '')[:field_max_length]
                router_information.firmware_version = rb.get('current-firmware', '')[:field_max_length]
                success = True
            if not success:
                return False, 'Failed to retrieve router information'

            # A failed update check must not fail the information update itself
            try:
                update_info = _get_routeros_update_info(ssh)
                json_data['/system package update print'] = update_info
                available_version, update_available = _parse_routeros_update_info(update_info, sr)
            except Exception as e:
                logger.warning(f'Update check failed for {router.name}: {e}')
                available_version, update_available = '', False
            router_information.available_version = available_version[:field_max_length]
            router_information.update_available = update_available

        elif router.router_type == 'openwrt':
            stdin, stdout, stderr = ssh.exec_command('cat /etc/os-release')
            osrel = {}
            for line in stdout.read().decode('utf-8').splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    osrel[k] = v.strip().strip('"')
            json_data['cat /etc/os-release'] = osrel

            # hostname
            stdin, stdout, stderr = ssh.exec_command('uci get system.@system[0].hostname')
            hostname = stdout.read().decode('utf-8').strip()
            json_data['uci get system.@system[0].hostname'] = hostname

            # architecture
            stdin, stdout, stderr = ssh.exec_command('uname -m')
            arch = stdout.read().decode('utf-8').strip()
            json_data['uname -m'] = arch

            # fallback serial (MAC of eth0)
            stdin, stdout, stderr = ssh.exec_command('cat /sys/class/net/eth0/address')
            mac = stdout.read().decode('utf-8').strip()
            json_data['cat /sys/class/net/eth0/address'] = mac

            if osrel:
                router_information.model_name       = osrel.get('OPENWRT_DEVICE_MODEL', '')[:field_max_length]
                router_information.model_version    = osrel.get('VERSION_ID', '')[:field_max_length]
                router_information.serial_number    = mac[:field_max_length]
                router_information.os_version       = osrel.get('VERSION', '')[:field_max_length]
                router_information.firmware_version = osrel.get('OPENWRT_RELEASE', '')[:field_max_length]
                router_information.architecture     = arch[:field_max_length]
                success = True
            if not success:
                return False, 'Failed to retrieve router information'

            # A failed update check must not fail the information update itself
            try:
                available_version, update_available = get_openwrt_available_version(osrel)
            except Exception as e:
                logger.warning(f'Update check failed for {router.name}: {e}')
                available_version, update_available = '', False
            router_information.available_version = available_version[:field_max_length]
            router_information.update_available = update_available

        elif router.router_type == 'ubiquiti-airos':
            stdin, stdout, stderr = ssh.exec_command('cat /etc/version')
            version_raw = stdout.read().decode('utf-8', errors='ignore').strip()
            json_data['cat /etc/version'] = version_raw
            stdin, stdout, stderr = ssh.exec_command('cat /etc/board.info')
            board_raw = stdout.read().decode('utf-8', errors='ignore')
            json_data['cat /etc/board.info'] = board_raw
            board = {}
            for line in board_raw.splitlines():
                line = line.strip()
                if not line or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                board[k.strip()] = v.strip()
            if not board:
                return False, 'Failed to retrieve router information from /etc/board.info'

            stdin, stdout, stderr = ssh.exec_command('uname -r')
            kernel_version = stdout.read().decode('utf-8', errors='ignore').strip()
            json_data['uname -r'] = kernel_version

            stdin, stdout, stderr = ssh.exec_command('cat /proc/cpuinfo')
            cpuinfo_raw = stdout.read().decode('utf-8', errors='ignore')
            json_data['cat /proc/cpuinfo'] = cpuinfo_raw
            cpuinfo = {}

            for line in cpuinfo_raw.splitlines():
                if ':' not in line:
                    continue
                k, v = line.split(':', 1)
                cpuinfo[k.strip().lower()] = v.strip()

            router_information.model_name = board.get('board.name', '')[:field_max_length]
            router_information.model_version = (
                    board.get('board.model', '') or board.get('board.shortname', '')
            )[:field_max_length]

            router_information.serial_number = (
                    board.get('board.device_id', '') or board.get('board.hwaddr', '')
            )[:field_max_length]
            router_information.firmware_version = version_raw[:field_max_length]
            router_information.os_version = kernel_version[:field_max_length]
            cpu_model = cpuinfo.get('cpu model', '')
            router_information.architecture = (
                cpu_model.split()[0] if cpu_model else ''
            )[:field_max_length]
            router_information.cpu = (
                    cpuinfo.get('system type', '')
                    or cpu_model
                    or board.get('board.cpurevision', '')
            )[:field_max_length]
            # airOS has no reliable update check over SSH
            router_information.available_version = ''
            router_information.update_available = False
            success = True
        else:
            return False, f"Router type not supported: {router.get_router_type_display()}"

        if success:
            router_information.success        = True
            router_information.error          = False
            router_information.retry_count    = 0
            router_information.next_retry     = None
            router_information.error_message = ''
            router_information.last_retrieval = timezone.now()
            router_information.json_data = json.dumps(json_data)
            router_information.save()

    except Exception as e:
        success = False
        error_message = str(e)

    finally:
        try:
            ssh.close()
        except:
            pass

    return success, error_message


def update_router_information(router_information: RouterInformation):
    max_retry = 3
    retry_minutes = 5

    success = False
    error_message = ''

    if router_information.retry_count > max_retry:
        router_information.error = True
        router_information.success = False
        router_information.next_retry = None
        router_information.retry_count = 0
        router_information.last_retrieval = timezone.now()
        if router_information.error_message:
            router_information.error_message += f"\nMax retries reached for {router_information.router.name}"
        else:
            router_information.error_message = f"Max retries reached for {router_information.router.name}"
        router_information.save()
        return False, router_information.error_message
    try:
        success, error_message = get_router_information(router_information)
    except Exception as e:
        success = False
        error_message = f"Failed to update router information for {router_information.router.name}. Exception: {e}"

    if not success:
        router_information.error = True
        router_information.success = False
        router_information.next_retry = timezone.now() + datetime.timedelta(minutes=retry_minutes)
        router_information.retry_count += 1
        router_information.last_retrieval = timezone.now()
        if error_message:
            router_information.error_message = error_message
        else:
            router_information.error_message = f"Failed to update router information for {router_information.router.name}"
        router_information.save()

    return success, error_message
