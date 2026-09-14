import base64
import csv
import hashlib
import io
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

_fernet = None


def get_fernet():
    """Return a cached Fernet instance.

    The key comes from settings.ENCRYPTION_KEY, which the container entrypoint
    generates once and stores in the persistent /app_secrets volume (same
    pattern as MONITORING_KEY). Outside docker it falls back to a key derived
    from SECRET_KEY so development keeps working out of the box.
    """
    global _fernet
    if _fernet is None:
        encryption_key = getattr(settings, 'ENCRYPTION_KEY', None)
        if not encryption_key:
            encryption_key = base64.urlsafe_b64encode(
                hashlib.sha256(settings.SECRET_KEY.encode()).digest()
            ).decode()
        _fernet = Fernet(encryption_key.encode())
    return _fernet


def encrypt_value(value):
    """Encrypt a plaintext value. Empty values pass through untouched.

    Idempotent: values that are already valid Fernet tokens (e.g. during a data
    migration that assigns encrypted values through a model save) are returned
    unchanged instead of being encrypted twice.
    """
    if value in (None, ''):
        return value
    value = str(value)
    try:
        get_fernet().decrypt(value.encode())
        return value
    except InvalidToken:
        return get_fernet().encrypt(value.encode()).decode()


def decrypt_value(value):
    """Decrypt a value produced by encrypt_value. Empty values pass through."""
    if value in (None, ''):
        return value
    try:
        return get_fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        logger.warning('Could not decrypt stored value: the ENCRYPTION_KEY does not match this value')
        return value


def mask_passwords_in_csv(raw_csv_data):
    """Rewrite CSV text with every value in the 'password' column replaced by '********'.

    Used so that imported CSV data never keeps the password in cleartext at rest.
    Unparseable input is returned unchanged so no data is lost.
    """
    try:
        csv_file = io.StringIO(raw_csv_data)
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames or 'password' not in reader.fieldnames:
            return raw_csv_data
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=reader.fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in reader:
            if row.get('password'):
                row['password'] = '********'
            writer.writerow(row)
        return output.getvalue()
    except csv.Error:
        logger.warning('Could not parse CSV data to mask passwords')
        return raw_csv_data


class EncryptedTextField(models.TextField):
    """TextField that stores its value encrypted at rest.

    Reads return the plaintext, so existing code that uses the field value
    (e.g. connecting to a router over SSH) keeps working unchanged. Both
    instance save() and queryset.update() apply get_prep_value, so values are
    encrypted at rest either way.
    """

    def get_prep_value(self, value):
        if value is None:
            return None
        return encrypt_value(str(value))

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        if isinstance(value, (bytes, memoryview)):
            value = value.decode()
        return decrypt_value(value)
